"""Named pipeline stages preserve the existing release and deployment trust policy."""

import copy
import json
import os
from pathlib import Path
import sys
import unittest
import yaml
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delivery as d


class DeliveryStages(unittest.TestCase):
    def setUp(self):
        self.facts = {
            name: {
                "component": name,
                "repository": "Narx-Space-Prod/" + repo,
                "image": "ghcr.io/narx-space-prod/commerce-" + name,
                "digest": "sha256:" + "a" * 64,
                "commit": "b" * 40,
                "runId": 12,
            }
            for name, repo in d.COMPONENTS.items()
        }
        self.bundle = d.bundle(self.facts)
        self.old = copy.deepcopy(self.facts)
        for fact in self.old.values():
            fact["commit"] = "e" * 40
        self.states = {
            "dev": self.bundle,
            "qa": d.bundle(self.old),
            "production": d.bundle(self.old),
        }

    def command(self, *args, **kwargs):
        if args[:3] == ("git", "rev-parse", "origin/main"):
            return "c" * 40
        if args[:2] == ("git", "show"):
            env = args[2].split("environments/")[1].split("/")[0]
            return json.dumps(self.states[env])
        if args[:3] == ("gh", "pr", "list"):
            return json.dumps([{"state": "OPEN"}])
        return ""

    def state(self, path):
        return self.states[Path(path).parent.name]

    def test_qa_stage_verifies_the_exact_upstream_and_preserves_previous_state(self):
        with (
            patch.object(d, "run", side_effect=self.command),
            patch.object(d, "read", side_effect=self.state),
            patch.object(d, "healthy", return_value=True),
            patch.object(d, "successful_run", return_value=True),
            patch.object(d, "verify_image") as verify,
            patch.object(d, "open_pr") as propose,
            patch.object(d, "stage_output"),
        ):
            d.advance_stage("qa", self.bundle["release"]["id"])
            self.assertEqual(verify.call_count, 4)
            env, value, reason = propose.call_args.args
            self.assertEqual((env, reason), ("qa", "promote"))
            self.assertEqual(value["release"]["components"], self.facts)
            self.assertEqual(
                value["release"]["previous"],
                {"components": self.old, "gitopsCommit": "c" * 40, "environment": "qa"},
            )

    def test_superseded_or_unhealthy_upstream_cannot_be_promoted(self):
        with (
            patch.object(d, "run", side_effect=self.command),
            patch.object(d, "read", side_effect=self.state),
            patch.object(d, "healthy", return_value=False),
            patch.object(d, "open_pr") as propose,
        ):
            for wanted in ["f" * 64, self.bundle["release"]["id"]]:
                with self.assertRaises(ValueError):
                    d.advance_stage("qa", wanted)
            propose.assert_not_called()

    def test_wait_requires_matching_bundle_and_trusted_success(self):
        self.states["qa"] = self.bundle
        with (
            patch.object(d, "run", side_effect=self.command),
            patch.object(
                d,
                "healthy",
                side_effect=lambda revision, env, state="success": state == "success",
            ),
            patch.object(d, "stage_output") as output,
        ):
            d.wait_for_deployment("dev", self.bundle["release"]["id"])
            output.assert_called_once()
        with (
            patch.object(d, "run", side_effect=self.command),
            patch.object(d, "healthy", return_value=True),
            patch.object(d, "stage_output") as output,
        ):
            with self.assertRaisesRegex(ValueError, "failure"):
                d.wait_for_deployment("qa", self.bundle["release"]["id"])
            output.assert_not_called()

    def test_wait_timeout_is_not_reported_as_deployment_success(self):
        with (
            patch.object(d.time, "monotonic", return_value=1),
            patch.object(d, "stage_output") as output,
        ):
            with self.assertRaisesRegex(ValueError, "timed out"):
                d.wait_for_deployment("qa", self.bundle["release"]["id"], timeout=0)
            output.assert_not_called()

    def test_dispatch_waits_for_source_ci_and_rejects_failed_or_untrusted_runs(self):
        source = {
            "repository": "Narx-Space-Prod/commerce-orders",
            "commit": "b" * 40,
            "runId": 12,
        }
        record = {
            "head_sha": "b" * 40,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "head_repository": {"full_name": source["repository"]},
        }
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "repository_dispatch",
                    "GITHUB_EVENT_PATH": "fixture",
                },
            ),
            patch.object(d, "read", return_value={"client_payload": source}),
            patch.object(d.time, "sleep"),
        ):
            with patch.object(
                d,
                "api",
                side_effect=[
                    {**record, "status": "in_progress"},
                    {**record, "status": "completed", "conclusion": "success"},
                ],
            ) as api:
                d.wait_for_source()
                self.assertEqual(api.call_count, 2)
            for change in [
                {"status": "completed", "conclusion": "failure"},
                {"head_branch": "feature"},
            ]:
                with patch.object(d, "api", return_value={**record, **change}):
                    with self.assertRaises(ValueError):
                        d.wait_for_source()

    def test_production_continuation_rejects_unmerged_and_fork_events(self):
        for event in [
            {"pull_request": {"merged": False}},
            {
                "pull_request": {
                    "merged": True,
                    "base": {"ref": "main"},
                    "head": {"repo": {"full_name": "attacker/fork"}},
                }
            },
        ]:
            with (
                patch.dict(os.environ, {"GITHUB_EVENT_PATH": "fixture"}),
                patch.object(d, "read", return_value=event),
                patch.object(d, "run") as run,
            ):
                with self.assertRaises(ValueError):
                    d.observe_production()
                run.assert_not_called()


class DeliveryGraph(unittest.TestCase):
    def test_graph_orders_real_smoke_gates_and_keeps_wait_jobs_read_only(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/commerce.yml").read_text())
        jobs = workflow["jobs"]
        self.assertEqual(jobs["verify-dev"]["needs"], "dev")
        self.assertEqual(jobs["qa"]["needs"], ["dev", "verify-dev", "business"])
        self.assertEqual(jobs["verify-qa"]["needs"], ["dev", "qa"])
        self.assertEqual(jobs["production-pr"]["needs"], ["dev", "verify-qa"])
        for name in ["verify-dev", "verify-qa", "production"]:
            self.assertNotIn("secrets", jobs[name])
            self.assertNotIn("environment", jobs[name])
            self.assertEqual(jobs[name]["permissions"]["statuses"], "read")
            self.assertEqual(jobs[name]["permissions"]["contents"], "read")
        observer = yaml.safe_load(
            (root / ".github/workflows/reusable-verify.yml").read_text()
        )["jobs"]["observe"]
        self.assertNotIn("environment", observer)
        self.assertNotIn("secrets", observer)
        self.assertEqual(
            observer["permissions"],
            {"contents": "read", "statuses": "read", "pull-requests": "read"},
        )
        self.assertEqual(
            jobs["production"]["needs"], ["dev", "production-pr", "approval"]
        )


class ReleaseSecretContract(unittest.TestCase):
    def test_only_release_stages_receive_named_secret(self):
        import yaml

        root = Path(__file__).resolve().parents[1]
        for filename in ("commerce.yml", "recovery.yml"):
            workflow = yaml.safe_load(
                (root / ".github/workflows" / filename).read_text()
            )
            for job in workflow["jobs"].values():
                if job.get("uses", "").endswith("/reusable-release-stage.yml"):
                    self.assertEqual(
                        set(job["secrets"]), {"NARX_RELEASE_APP_PRIVATE_KEY"}
                    )
                else:
                    self.assertNotIn("secrets", job)


class MasterSourceGates(unittest.TestCase):
    def setUp(self):
        import source_gate

        self.g = source_gate
        self.repo = "Narx-Space-Prod/commerce-orders"
        self.event = {
            "client_payload": {"repository": self.repo, "commit": "b" * 40, "runId": 12}
        }
        self.record = {
            "head_sha": "b" * 40,
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
            "head_repository": {"full_name": self.repo},
        }

    def test_payload_cannot_select_forks_or_inject_commands(self):
        for changes in [
            {"repository": "attacker/fork"},
            {"commit": "$(bad)"},
            {"runId": True},
        ]:
            with self.assertRaises(ValueError):
                self.g.identity(
                    {"client_payload": {**self.event["client_payload"], **changes}}
                )
        for changes in [
            {"head_sha": "c" * 40},
            {"event": "pull_request"},
            {"path": ".github/workflows/other.yml"},
        ]:
            with self.assertRaises(ValueError):
                self.g.validate({**self.record, **changes}, self.repo, "b" * 40)

    def test_master_has_ci_through_production_in_one_dependency_chain(self):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / ".github/workflows/commerce.yml").read_text())[
            "jobs"
        ]
        self.assertEqual(jobs["quality"]["needs"], "tests")
        self.assertEqual(jobs["publication"]["needs"], "quality")
        self.assertEqual(jobs["dev"]["needs"], "publication")
        self.assertEqual(
            jobs["production"]["needs"], ["dev", "production-pr", "approval"]
        )
        self.assertEqual(
            jobs["production"]["with"]["release-id"],
            "${{ needs.dev.outputs.release-id }}",
        )
        self.assertEqual(jobs["production"]["with"]["timeout-seconds"], 3600)
        self.assertNotIn("secrets", jobs["production"])


class OrchestrationOwnership(unittest.TestCase):
    def test_only_mutations_hold_the_shared_queued_lock(self):
        root = Path(__file__).resolve().parents[1]
        load = lambda name: yaml.safe_load(
            (root / ".github/workflows" / name).read_text()
        )
        self.assertFalse((root / ".github/workflows/delivery.yml").exists())
        self.assertNotIn("concurrency", load("commerce.yml"))
        mutation = load("reusable-release-stage.yml")["jobs"]["advance"]
        self.assertEqual(
            mutation["concurrency"],
            {
                "group": "release-state-mutation",
                "cancel-in-progress": False,
                "queue": "max",
            },
        )
        self.assertNotIn("concurrency", load("recovery.yml")["jobs"]["recovery"])
        self.assertIn("production-observation", load("recovery.yml")["jobs"])
        for name in ("reusable-release-stage.yml", "reusable-verify.yml"):
            jobs = load(name)["jobs"]
            for job in jobs.values():
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/checkout@"):
                        self.assertNotIn("ref", step.get("with", {}))

    def test_read_transport_retries_but_authorization_and_mutation_fail_closed(self):
        import subprocess

        transient = subprocess.CalledProcessError(1, ["gh"], output="HTTP 503")
        denied = subprocess.CalledProcessError(1, ["gh"], output="HTTP 403")
        with (
            patch.object(d.subprocess, "check_output", side_effect=[transient, "{}"]),
            patch.object(d.time, "sleep") as sleep,
        ):
            self.assertEqual(d.run("gh", "api", "repos/example"), "{}")
            sleep.assert_called_once()
        for args, error in [
            (("gh", "api", "repos/example"), denied),
            (("gh", "api", "--method", "POST", "repos/example"), transient),
        ]:
            with (
                patch.object(d.subprocess, "check_output", side_effect=error),
                patch.object(d.time, "sleep") as sleep,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    d.run(*args)
                sleep.assert_not_called()


class VersionedEvidence(unittest.TestCase):
    def setUp(self):
        import source_gate

        self.g = source_gate
        self.repo = "Narx-Space-Prod/commerce-orders"
        self.record = {"run_attempt": 2, "status": "in_progress"}
        self.value = {
            "schemaVersion": 1,
            "gate": "tests",
            "repository": self.repo,
            "commit": "b" * 40,
            "runId": 12,
            "runAttempt": 2,
            "result": "success",
            "releases": [],
        }

    def archive(self, value, filename="gate.json"):
        import io, zipfile

        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr(filename, json.dumps(value))
        return raw.getvalue()

    def artifact(self):
        return {
            "name": "narx-ci-v1-tests-2",
            "id": 44,
            "expired": False,
            "size_in_bytes": 1024,
            "workflow_run": {"id": 12},
        }

    def test_valid_versioned_gate_has_no_job_display_name_dependency(self):
        pages = json.dumps([{"artifacts": [self.artifact()]}])
        with patch.object(self.g, "run", side_effect=[pages, self.archive(self.value)]):
            self.assertEqual(
                self.g.evidence("tests", self.record, self.repo, "b" * 40, 12),
                self.value,
            )

    def test_stale_attempt_forged_commit_and_archive_paths_are_rejected(self):
        for change in [
            {"runAttempt": 1},
            {"commit": "c" * 40},
            {"runId": 13},
            {"schemaVersion": True},
        ]:
            with patch.object(
                self.g,
                "run",
                side_effect=[
                    json.dumps([{"artifacts": [self.artifact()]}]),
                    self.archive({**self.value, **change}),
                ],
            ):
                with self.assertRaises(ValueError):
                    self.g.evidence("tests", self.record, self.repo, "b" * 40, 12)
        with patch.object(
            self.g,
            "run",
            side_effect=[
                json.dumps([{"artifacts": [self.artifact()]}]),
                self.archive(self.value, "../gate.json"),
            ],
        ):
            with self.assertRaises(ValueError):
                self.g.evidence("tests", self.record, self.repo, "b" * 40, 12)

    def test_missing_expired_or_duplicate_artifacts_fail_closed(self):
        for artifacts in [
            [],
            [{**self.artifact(), "expired": True}],
            [self.artifact(), self.artifact()],
        ]:
            with patch.object(
                self.g, "run", return_value=json.dumps([{"artifacts": artifacts}])
            ):
                with self.assertRaises(ValueError):
                    self.g.evidence(
                        "tests",
                        {**self.record, "status": "completed"},
                        self.repo,
                        "b" * 40,
                        12,
                    )

    def test_version_one_cannot_silently_fall_back_to_job_names(self):
        event = {
            "client_payload": {
                "repository": self.repo,
                "commit": "b" * 40,
                "runId": 12,
                "evidenceVersion": 1,
            }
        }
        with (
            patch.object(
                self.g, "observe_versioned", side_effect=ValueError("missing evidence")
            ),
            patch.object(self.g, "api") as legacy,
        ):
            with self.assertRaises(ValueError):
                self.g.observe("tests", event)
            legacy.assert_not_called()


class OverlappingReleaseSafety(unittest.TestCase):
    setUp = DeliveryStages.setUp
    command = DeliveryStages.command

    def test_old_promotion_candidate_cannot_replace_a_newer_environment_release(self):
        candidate = d.bundle(
            self.facts,
            {"components": self.old, "gitopsCommit": "c" * 40, "environment": "qa"},
        )
        # Another master has advanced QA since this PR was prepared.
        states = copy.deepcopy(self.states)
        states["qa"] = d.bundle(self.facts)
        with (
            patch.object(d, "healthy", return_value=True),
            patch.object(d, "verify_image") as verify,
        ):
            with self.assertRaises(ValueError):
                d.verify_candidate(candidate, "qa", "d" * 40, states)
            verify.assert_not_called()

    def test_delayed_failure_event_does_not_rollback_current_healthy_release(self):
        with (
            patch.object(d, "run", side_effect=self.command),
            patch.object(d, "healthy", return_value=False),
            patch.object(d, "open_pr") as propose,
        ):
            d.recover_stage("qa")
            propose.assert_not_called()


class BusinessGateSafety(unittest.TestCase):
    def test_business_test_blocks_qa_uses_exact_images_and_cleans_only_its_project(
        self,
    ):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / ".github/workflows/commerce.yml").read_text())[
            "jobs"
        ]
        gate = jobs["business"]
        steps = yaml.safe_load(
            (root / ".github/actions/business/action.yml").read_text()
        )["runs"]["steps"]
        text = "\n".join(s.get("run", "") for s in steps)
        self.assertEqual(gate["needs"], ["dev", "verify-dev"])
        self.assertIn("business", jobs["qa"]["needs"])
        self.assertEqual(gate["env"]["NARX_TEST_URL"], "http://127.0.0.1:18080")
        self.assertNotIn("secrets", gate)
        self.assertIn("--no-build", text)
        self.assertIn("DELIVERY_VISIBILITY_SECONDS=15", text)
        self.assertNotIn("DELIVERY_VISIBILITY_SECONDS=5", text)
        self.assertIn("logs --tail 100 receipt-worker outbox-publisher", text)
        self.assertIn("facts[component]['digest']", text)
        self.assertIn("config['services'][service].pop('build',None)", text)
        self.assertIn("--volumes --remove-orphans", text)
        self.assertNotIn("docker system prune", text)
        self.assertNotIn("kubectl", text)
        self.assertEqual(steps[-1]["if"], "always()")
        fixture = next(
            s for s in steps if s.get("with", {}).get("path") == "fixture-source"
        )
        self.assertEqual(
            fixture["with"]["ref"], "${{ steps.bundle.outputs.orders-commit }}"
        )


class RetiredPathCleanup(unittest.TestCase):
    def test_old_or_unspecified_evidence_is_rejected_without_api_access(self):
        import source_gate

        for version in (None, True, 0, 2, "1"):
            with patch.object(source_gate, "api") as api:
                with self.assertRaises(ValueError):
                    source_gate.observe(
                        "tests", {"client_payload": {"evidenceVersion": version}}
                    )
                api.assert_not_called()

    def test_manual_rollback_reuses_verified_previous_release_without_failure_trigger(
        self,
    ):
        sample = DeliveryStages()
        sample.setUp()
        current = d.bundle(
            sample.facts,
            {"components": sample.old, "gitopsCommit": "c" * 40, "environment": "qa"},
        )

        def command(*args, **kwargs):
            if args[:2] == ("git", "show"):
                return json.dumps(current)
            return sample.command(*args, **kwargs)

        with (
            patch.object(d, "run", side_effect=command),
            patch.object(
                d,
                "healthy",
                side_effect=lambda revision, env, state="success": state == "success",
            ),
            patch.object(d, "verify_image") as verify,
            patch.object(d, "open_pr") as propose,
            patch.object(d, "stage_output"),
        ):
            d.recover_stage("qa")
            propose.assert_not_called()
            d.recover_stage("qa", manual=True)
            self.assertEqual(
                propose.call_args.args[1]["release"]["components"], sample.old
            )
            self.assertEqual(verify.call_count, 4)


class CompactWorkflowStructure(unittest.TestCase):
    def test_master_reuses_one_read_only_verifier_and_business_implementation(self):
        root = Path(__file__).resolve().parents[1]
        load = lambda name: yaml.safe_load(
            (root / ".github/workflows" / name).read_text()
        )
        jobs = load("commerce.yml")["jobs"]
        for name, operation in [
            ("tests", "tests"),
            ("quality", "quality"),
            ("publication", "publication"),
            ("verify-dev", "wait"),
            ("verify-qa", "wait"),
            ("production", "wait"),
        ]:
            self.assertEqual(
                jobs[name]["uses"], "./.github/workflows/reusable-verify.yml"
            )
            self.assertEqual(
                jobs[name].get("with", {}).get("operation", "wait"), operation
            )
        steps = jobs["business"]["steps"]
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[-1]["uses"], "./.github/actions/business")
        action = yaml.safe_load(
            (root / ".github/actions/business/action.yml").read_text()
        )
        self.assertEqual(action["runs"]["using"], "composite")
        for step in action["runs"]["steps"]:
            if "run" in step:
                self.assertEqual(step["shell"], "bash")
        verifier = load("reusable-verify.yml")["jobs"]["observe"]
        self.assertNotIn("environment", verifier)
        self.assertNotIn("secrets", verifier)
        self.assertTrue(all(v == "read" for v in verifier["permissions"].values()))
        self.assertFalse((root / ".github/workflows/source-gate.yml").exists())
        self.assertFalse((root / ".github/workflows/delivery-observe.yml").exists())


class ProductionApprovalVisibility(unittest.TestCase):
    def setUp(self):
        sample = DeliveryStages()
        sample.setUp()
        self.bundle = sample.bundle
        self.release = self.bundle["release"]["id"]
        self.pr = {
            "state": "open",
            "merged": False,
            "base": {"ref": "main"},
            "head": {
                "repo": {"full_name": "Narx-Space-Prod/platform-gitops"},
                "ref": "release/production-" + self.release[:16],
            },
        }

    def test_waits_for_merge_not_just_an_open_pr_then_checks_exact_bundle(self):
        merged = {
            **self.pr,
            "merged": True,
            "state": "closed",
            "merge_commit_sha": "c" * 40,
        }
        files = [
            {"filename": "environments/production/release.json", "status": "modified"}
        ]
        with (
            patch.object(d, "api", side_effect=[self.pr, merged, files]),
            patch.object(d, "run", return_value=json.dumps(self.bundle)),
            patch.object(d.time, "sleep") as sleep,
        ):
            d.wait_for_approval(12, self.release)
            sleep.assert_called_once()

    def test_declined_fork_wrong_bundle_and_extra_files_are_rejected(self):
        bads = [
            {**self.pr, "state": "closed"},
            {**self.pr, "head": {"repo": {"full_name": "attacker/fork"}}},
        ]
        for pr in bads:
            with (
                patch.object(d, "api", return_value=pr),
                patch.object(d.time, "sleep") as sleep,
            ):
                with self.assertRaises(ValueError):
                    d.wait_for_approval(12, self.release)
                sleep.assert_not_called()
        merged = {**self.pr, "merged": True, "merge_commit_sha": "c" * 40}
        for files, value in [
            ([{"filename": "README.md", "status": "modified"}], self.bundle),
            (
                [
                    {
                        "filename": "environments/production/release.json",
                        "status": "modified",
                    }
                ],
                {"release": {"ready": False}},
            ),
        ]:
            with (
                patch.object(d, "api", side_effect=[merged, files]),
                patch.object(d, "run", return_value=json.dumps(value)),
            ):
                with self.assertRaises(ValueError):
                    d.wait_for_approval(12, self.release)

    def test_approval_timeout_fails_and_has_no_write_capability(self):
        with (
            patch.object(d.time, "monotonic", side_effect=[0, 4000]),
            patch.object(d, "api") as api,
        ):
            with self.assertRaisesRegex(ValueError, "timed out"):
                d.wait_for_approval(12, self.release)
            api.assert_not_called()

    def test_master_has_distinct_approval_and_rollout_nodes(self):
        root = Path(__file__).resolve().parents[1]
        jobs = yaml.safe_load((root / ".github/workflows/commerce.yml").read_text())[
            "jobs"
        ]
        self.assertIn("Await Production PR", jobs["approval"]["name"])
        self.assertEqual(jobs["approval"]["with"]["operation"], "wait-approval")
        self.assertIn("approval", jobs["production"]["needs"])
        self.assertIn("needs.approval.result == 'success'", jobs["production"]["if"])
        self.assertNotIn("secrets", jobs["approval"])
        self.assertTrue(
            all(value == "read" for value in jobs["approval"]["permissions"].values())
        )
