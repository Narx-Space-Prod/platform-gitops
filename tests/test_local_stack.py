"""Opt-in integration test against the isolated Docker Compose preview.

Creates throwaway accounts and orders, promotes one test account using local SQL,
and exercises payment/refund simulation. Never run against a production URL.
"""

import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("NARX_TEST_URL", "http://localhost:18080").rstrip("/")
PROJECT = os.environ.get("NARX_COMPOSE_PROJECT", "")


@unittest.skipUnless(os.environ.get("NARX_LOCAL_TEST") == "1", "Set NARX_LOCAL_TEST=1 for the isolated local preview")
class CommerceIntegration(unittest.TestCase):
    def compose(self, *arguments, input=None):
        command = ["docker", "compose", "--project-name", PROJECT, "--env-file", str(ROOT / "local/.env"), "-f", str(ROOT / "local/compose.yaml")]
        return subprocess.run([*command, *arguments], input=input, text=True, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def sql(self, statement):
        return self.compose("exec", "-T", "postgres", "psql", "-U", "commerce", "-d", "commerce", "-At", "-v", "ON_ERROR_STOP=1", input=statement)

    def eventually(self, condition, timeout=120):
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except subprocess.CalledProcessError as error:
                # A queue can briefly refuse connections while its persisted state is restored.
                last_error = error.stderr[-1000:]
            time.sleep(1)
        self.fail("Asynchronous condition did not complete before deadline: " + last_error)

    def queue(self, action, **values):
        # ElasticMQ only, reached INSIDE the isolated Compose network. No AWS credentials.
        code = """
import json,sys,urllib.request
action,values=json.load(sys.stdin)
request=urllib.request.Request('http://elasticmq:9324', data=json.dumps(values).encode(),
    headers={'Content-Type':'application/x-amz-json-1.0','X-Amz-Target':'AmazonSQS.'+action})
with urllib.request.urlopen(request, timeout=15) as response:
    print(response.read().decode())
"""
        values.setdefault("QueueUrl", "http://elasticmq:9324/000000000000/order-paid")
        return json.loads(self.compose("exec", "-T", "razorpay-fixture", "python", "-c", code, input=json.dumps([action, values])) or "{}")

    def queue_depth(self, dead=False):
        url = "http://elasticmq:9324/000000000000/order-paid" + ("-dlq" if dead else "")
        attributes = self.queue("GetQueueAttributes", QueueUrl=url,
                                AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return sum(int(value) for value in attributes.values())

    def verify_delivery_recovery(self, order_id, request):
        # UUID was returned by our own isolated application, never arbitrary SQL input.
        self.assertEqual(str(uuid.UUID(order_id)), order_id)
        where = f"order_id='{order_id}'"
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM order_outbox WHERE {where}"), "1")
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM order_outbox WHERE {where} AND published_at IS NULL"), "1")
        self.eventually(lambda: int(self.sql(f"SELECT attempts FROM order_outbox WHERE {where}")) >= 1)
        request("/health/ready")
        print("Queue outage: payment committed, event retained, API healthy", flush=True)
        self.compose("start", "elasticmq")
        self.eventually(lambda: self.sql(f"SELECT COUNT(*) FROM payment_receipts WHERE {where}") == "1")
        self.eventually(lambda: self.sql(f"SELECT COUNT(*) FROM order_outbox WHERE {where} AND published_at IS NOT NULL") == "1")
        self.assertIsNotNone(request(f"/api/orders/{order_id}")["receipt"])
        self.eventually(lambda: self.queue_depth() == 0)

        # Simulate SendMessage success followed by publisher crash before DB commit.
        attempts = int(self.sql(f"SELECT attempts FROM order_outbox WHERE {where}"))
        self.sql(f"UPDATE order_outbox SET published_at=NULL,next_attempt_at=CURRENT_TIMESTAMP WHERE {where};")
        self.eventually(lambda: int(self.sql(f"SELECT attempts FROM order_outbox WHERE {where}")) > attempts)
        self.eventually(lambda: self.queue_depth() == 0)
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM payment_receipts WHERE {where}"), "1")
        body = self.sql(f"SELECT payload FROM order_outbox WHERE {where}")
        event = json.loads(body)
        event_id = event["eventId"]
        self.assertEqual(str(uuid.UUID(event_id)), event_id)
        print("Publisher replay: one receipt, one committed event marker", flush=True)

        # A DB failure after inserting the inbox marker must roll BOTH writes back.
        self.compose("stop", "receipt-worker")
        self.sql(f"BEGIN; DELETE FROM payment_receipts WHERE {where}; DELETE FROM processed_order_events WHERE event_id='{event_id}'; "
                 f"ALTER TABLE payment_receipts ADD CONSTRAINT local_receipt_failure CHECK(order_id <> '{order_id}'::uuid) NOT VALID; COMMIT;")
        self.queue("SendMessage", MessageBody=body)
        self.compose("restart", "elasticmq")
        self.eventually(lambda: self.queue_depth() == 1)
        self.compose("start", "receipt-worker")
        self.eventually(lambda: self.queue_depth() == 1)
        # Wait until the worker reports the injected processing failure, rather than just queue arrival.
        self.eventually(lambda: "receipt_processing_failed" in self.compose("logs", "--since", "30s", "receipt-worker"))
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM processed_order_events WHERE event_id='{event_id}'"), "0")
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM payment_receipts WHERE {where}"), "0")
        self.sql("ALTER TABLE payment_receipts DROP CONSTRAINT local_receipt_failure;")
        self.eventually(lambda: self.sql(f"SELECT COUNT(*) FROM payment_receipts WHERE {where}") == "1")
        self.eventually(lambda: self.queue_depth() == 0)
        print("Queue persistence and worker failure: restart retained message, transaction rolled back, retry recovered", flush=True)

        # A malformed event exhausts the configured five receives and reaches the DLQ.
        self.assertEqual(self.queue_depth(dead=True), 0, "Use a fresh disposable stack")
        poison = dict(event, version=999)
        self.queue("SendMessage", MessageBody=json.dumps(poison))
        self.eventually(lambda: self.queue_depth(dead=True) == 1, timeout=360)
        dead_url = "http://elasticmq:9324/000000000000/order-paid-dlq"
        message = self.queue("ReceiveMessage", QueueUrl=dead_url, MaxNumberOfMessages=1, WaitTimeSeconds=1)["Messages"][0]
        self.assertEqual(json.loads(message["Body"]), poison)
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM payment_receipts WHERE {where}"), "1")
        # Local operator replay: copy the trusted canonical event before deleting the poison message.
        self.queue("SendMessage", MessageBody=body)
        self.queue("DeleteMessage", QueueUrl=dead_url, ReceiptHandle=message["ReceiptHandle"])
        self.eventually(lambda: self.queue_depth() == 0 and self.queue_depth(dead=True) == 0)
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM payment_receipts WHERE {where}"), "1")
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM processed_order_events WHERE event_id='{event_id}'"), "1")
        print("DLQ: poison isolated after retries; corrected replay remained idempotent", flush=True)

    def test_customer_checkout_admin_and_refund(self):
        self.assertRegex(BASE, r"^http://(?:localhost|127\.0\.0\.1):[0-9]+$")
        self.assertRegex(PROJECT, r"^narx-[a-z0-9-]+$")
        if os.environ.get("NARX_QUEUE_TEST") == "1":
            self.assertRegex(PROJECT, r"^narx-[a-z0-9-]+-validation$")
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, method="GET", body=None, expected=200, headers=None, opener=client):
            data = json.dumps(body).encode() if body is not None else None
            values = {"Origin": BASE, "Content-Type": "application/json", **(headers or {})}
            call = urllib.request.Request(BASE + path, data=data, method=method, headers=values)
            try:
                response = opener.open(call, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                payload = response.read()
                self.assertEqual(response.status, expected, payload.decode())
                return json.loads(payload)

        request("/health/ready")
        delivery = {"name": "Integration Customer", "address": "1 Test Road", "city": "Test City", "postalCode": "560001", "country": "India"}
        request("/api/orders", "POST", delivery, expected=401, headers={"Idempotency-Key": "guest-checkout-rejected"})
        products = request("/api/products")
        self.assertTrue(products)
        self.assertTrue(all(product["currency"] == "INR" for product in products))
        product = products[0]
        email = f"integration-{uuid.uuid4().hex}@example.test"
        password = uuid.uuid4().hex + "-test"
        user = request("/api/auth/register", "POST", {"email": email, "password": password, "name": "Integration Customer"})
        self.assertEqual(user["user"]["role"], "customer")
        request("/api/admin/dashboard", expected=403)
        request(f"/api/cart/items/{product['id']}", "PUT", {"quantity": 2})
        key = "checkout-" + uuid.uuid4().hex
        request("/api/orders", "POST", {}, expected=400, headers={"Idempotency-Key": "delivery-required"})
        order = request("/api/orders", "POST", delivery, headers={"Idempotency-Key": key})
        replay = request("/api/orders", "POST", delivery, headers={"Idempotency-Key": key})
        self.assertEqual(replay["id"], order["id"])
        self.assertEqual(order["status"], "payment_pending")
        self.assertEqual(order["total"], product["price"] * 2)
        current = next(p for p in request("/api/products") if p["id"] == product["id"])
        self.assertEqual(current["stock"], product["stock"] - 2)

        payment = request(f"/api/orders/{order['id']}/payment", "POST", {})
        self.assertEqual(payment["mode"], "fixture")
        proof = request(f"/api/orders/{order['id']}/payment/fixture-capture", "POST", {})
        verify = f"/api/orders/{order['id']}/payment/verify"
        request(verify, "POST", dict(proof, razorpay_signature="0" * 64), expected=400)
        queue_test = os.environ.get("NARX_QUEUE_TEST") == "1"
        if queue_test:
            self.assertEqual(str(uuid.UUID(order["id"])), order["id"])
            self.compose("stop", "elasticmq")
            self.sql(f"ALTER TABLE order_outbox ADD CONSTRAINT local_outbox_failure CHECK(order_id <> '{order['id']}'::uuid) NOT VALID;")
            request(verify, "POST", proof, expected=500)
            self.assertEqual(request(f"/api/orders/{order['id']}")["payment_status"], "awaiting_payment")
            self.assertEqual(self.sql(f"SELECT COUNT(*) FROM order_outbox WHERE order_id='{order['id']}'"), "0")
            self.sql("ALTER TABLE order_outbox DROP CONSTRAINT local_outbox_failure;")
        request(verify, "POST", proof)
        request(verify, "POST", proof)
        paid = request(f"/api/orders/{order['id']}")
        self.assertEqual(paid["payment_status"], "paid")
        self.assertEqual(paid["status"], "confirmed")
        self.assertEqual(request("/api/orders")["orders"][0]["id"], order["id"])
        stranger = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request(f"/api/orders/{order['id']}", expected=404, opener=stranger)
        if queue_test:
            self.verify_delivery_recovery(order["id"], request)

        # Only the test's generated account in this named local stack is promoted.
        self.assertRegex(email, r"^integration-[a-f0-9]{32}@example\.test$")
        compose = ["docker", "compose", "--project-name", PROJECT, "--env-file", str(ROOT / "local/.env"), "-f", str(ROOT / "local/compose.yaml")]
        subprocess.run([*compose, "exec", "-T", "postgres", "psql", "-U", "commerce", "-d", "commerce", "-v", "ON_ERROR_STOP=1"],
            input=f"UPDATE customers SET role='admin' WHERE email='{email}';\n", text=True, check=True, stdout=subprocess.DEVNULL)
        request("/api/admin/dashboard")
        sku = "integration-" + uuid.uuid4().hex
        new_product = {"id": sku, "name": "Integration Product", "description": "Local test", "emoji": "◈", "price": 12345, "stock": 5, "active": True, "version": 0}
        saved = request(f"/api/admin/products/{sku}", "PUT", new_product)
        new_product.update(version=saved["version"], stock=7)
        request(f"/api/admin/products/{sku}", "PUT", new_product)
        request(f"/api/admin/products/{sku}", "PUT", new_product, expected=409)
        for state in ["processing", "shipped", "delivered"]:
            request(f"/api/admin/orders/{order['id']}/status", "POST", {"status": state})
        request(f"/api/admin/orders/{order['id']}/status", "POST", {"status": "confirmed"}, expected=409)
        refund = request(f"/api/admin/orders/{order['id']}/refund", "POST", {"reason": "Local integration test"})
        self.assertEqual(refund["status"], "processed")
        request(f"/api/admin/orders/{order['id']}/refund", "POST", {"reason": "Retry same refund"})
        self.assertEqual(request(f"/api/orders/{order['id']}")["payment_status"], "refunded")
        stock = next(p for p in request("/api/products") if p["id"] == product["id"])["stock"]
        self.assertEqual(stock, product["stock"] - 2, "Delivered items must not be restocked by a refund")
        dashboard = request("/api/admin/dashboard")
        self.assertTrue(any(row["currency"] == "INR" for row in dashboard["sales"]))
        request("/api/auth/logout", "POST", {})
        request("/api/admin/dashboard", expected=401)
        request("/api/auth/login", "POST", {"email": email, "password": password})
        self.assertEqual(request("/api/auth/session")["user"]["email"], email)


if __name__ == "__main__":
    unittest.main()
