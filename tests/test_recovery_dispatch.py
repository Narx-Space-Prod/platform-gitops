"""Failure dispatch stays bound to the current trusted deployment evidence."""

import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delivery as d


class RecoveryDispatch(unittest.TestCase):
    def setUp(self):
        self.revision = "a" * 40
        self.event = {
            "action": "recovery-requested",
            "sender": {"login": "narx-local-commerce-release[bot]", "id": 352511280},
            "client_payload": {
                "environment": "qa",
                "revision": self.revision,
                "statusId": 42,
            },
        }
        self.status = {
            "id": 42,
            "state": "failure",
            "context": "narx/deploy-qa",
            "creator": {"login": "narx-local-commerce-observer[bot]", "id": 352518969},
        }

    def check(self, event=None, statuses=None, revisions=None):
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_PATH": "test-event",
                    "NARX_DEPLOYMENT_BOT_LOGIN": self.status["creator"]["login"],
                },
            ),
            patch.object(d, "read", return_value=event or self.event),
            patch.object(
                d,
                "deployment_revisions",
                return_value=revisions if revisions is not None else [self.revision],
            ),
            patch.object(
                d,
                "api",
                return_value=statuses if statuses is not None else [self.status],
            ),
        ):
            return d.current_recovery_event("qa", "b" * 40)

    def test_current_trusted_failure_is_accepted(self):
        self.assertTrue(self.check())

    def test_new_revision_or_new_status_invalidates_event(self):
        self.assertFalse(self.check(revisions=["c" * 40]))
        for field, value in [("id", 43), ("state", "success")]:
            status = copy.deepcopy(self.status)
            status[field] = value
            self.assertFalse(self.check(statuses=[status, self.status]))

    def test_forged_dispatcher_or_payload_is_rejected(self):
        for field, value in [("id", 1), ("login", "other[bot]")]:
            event = copy.deepcopy(self.event)
            event["sender"][field] = value
            with self.assertRaises(ValueError):
                self.check(event)
        event = copy.deepcopy(self.event)
        event["client_payload"]["statusId"] = True
        with self.assertRaises(ValueError):
            self.check(event)

    def test_foreign_observer_is_rejected(self):
        status = copy.deepcopy(self.status)
        status["creator"]["id"] = 1
        self.assertFalse(self.check(statuses=[status]))

    def test_workflow_has_no_status_firehose_and_manual_recovery_remains(self):
        workflow = yaml.safe_load(
            (
                Path(__file__).resolve().parents[1] / ".github/workflows/recovery.yml"
            ).read_text()
        )
        events = workflow.get("on", workflow.get(True))
        self.assertNotIn("status", events)
        self.assertEqual(events["repository_dispatch"]["types"], ["recovery-requested"])
        self.assertIn("workflow_dispatch", events)
        self.assertIn("production-observation", workflow["jobs"])
        guard = workflow["jobs"]["recovery"]["if"]
        self.assertIn("352511280", guard)
        self.assertNotIn("github.event.state", guard)

    def test_stale_dispatch_cannot_reach_image_verification_or_pr_write(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "repository_dispatch"}),
            patch.object(d, "run", side_effect=["", "b" * 40]),
            patch.object(d, "current_recovery_event", return_value=False),
            patch.object(d, "verify_image") as verify,
            patch.object(d, "open_pr") as write,
        ):
            d.recover_stage("qa")
            verify.assert_not_called()
            write.assert_not_called()
