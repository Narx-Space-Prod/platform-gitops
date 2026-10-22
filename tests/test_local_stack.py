"""Opt-in integration test against the isolated Docker Compose preview.

Creates throwaway accounts and orders, promotes one test account using local SQL,
and exercises payment/refund simulation. Never run against a production URL.
"""

import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import unittest
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("NARX_TEST_URL", "http://localhost:18080").rstrip("/")
PROJECT = os.environ.get("NARX_COMPOSE_PROJECT", "")


@unittest.skipUnless(os.environ.get("NARX_LOCAL_TEST") == "1", "Set NARX_LOCAL_TEST=1 for the isolated local preview")
class CommerceIntegration(unittest.TestCase):
    def test_customer_checkout_admin_and_refund(self):
        self.assertRegex(BASE, r"^http://(?:localhost|127\.0\.0\.1):[0-9]+$")
        self.assertRegex(PROJECT, r"^narx-[a-z0-9-]+$")
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
        request(verify, "POST", proof)
        request(verify, "POST", proof)
        paid = request(f"/api/orders/{order['id']}")
        self.assertEqual(paid["payment_status"], "paid")
        self.assertEqual(paid["status"], "confirmed")
        self.assertEqual(request("/api/orders")["orders"][0]["id"], order["id"])
        stranger = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request(f"/api/orders/{order['id']}", expected=404, opener=stranger)

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
