"""RED-first contract for the native Hermes Kanban night operator."""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_constants import get_scratch_dir


class Spy:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> str:
        self.writes.append(kwargs)
        return kwargs.get("idempotency_key", "generated")

    def notify(self, **payload: Any) -> None:
        self.notices.append(payload)


def _plugin():
    return importlib.import_module("plugins.night_operator")


class NightOperatorContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="night-operator-", dir=get_scratch_dir(prune=False),
        )
        self.home = Path(self.tmp.name)
        self.db = self.home / "kanban.db"
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(self.home),
                "HERMES_KANBAN_HOME": str(self.home),
                "HERMES_KANBAN_DB": str(self.db),
                "HERMES_DELEGATED_CHILD_CONTEXT": "",
                "HERMES_KANBAN_TASK": "",
                "HERMES_KANBAN_RUN_ID": "",
                "HERMES_KANBAN_CLAIM_LOCK": "",
            },
            clear=False,
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        kanban_db.init_db(db_path=self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _completed(self, conn, *, batch: str | None, expected: list[str] | None = None) -> str:
        created = kanban_db.create_task(
            conn, title=f"child-{batch}", body="handoff", assignee="worker-a",
            initial_status="running", board="default",
        )
        claimed = kanban_db.claim_task(conn, created, claimer="worker-a")
        self.assertIsNotNone(claimed)
        self.assertTrue(kanban_db.complete_task(
            conn, created, summary="handoff", metadata={
                "operator_batch": batch,
                "operator_expected_ids": expected,
                "operator_group_ready": True,
            },
        ))
        return created

    def _logical_attachment(self, conn, task_id: str, filename: str) -> int:
        """Create a durable attachment row without claiming that its blob exists."""
        stored_path = self.home / "logical-attachments" / task_id / filename
        return kanban_db.add_attachment(
            conn, task_id, filename=filename, stored_path=str(stored_path),
            content_type="application/octet-stream", size=17, uploaded_by="worker-a",
        )

    def test_defaults_are_fail_closed(self) -> None:
        module = _plugin()
        self.assertFalse(module.DEFAULTS["enabled"])
        self.assertTrue(module.DEFAULTS["dry_run"])
        self.assertIsNone(module.DEFAULTS["escalation_channel"])

    def test_completed_without_explicit_config_is_disabled(self) -> None:
        module = _plugin()
        spy = Spy()
        result = module.handle_completed(
            task_id="t-default", board="default", batch="batch-default",
            expected_ids=["t-default"], adapter=spy,
        )
        self.assertEqual(result.terminal_state, "disabled")
        self.assertEqual(spy.writes, [])

    def test_completed_batch_creates_one_verification_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            child_a = self._completed(conn, batch="batch-7")
            child_b = self._completed(conn, batch="batch-7", expected=[child_a])
            spy = Spy()
            result = module.handle_completed(
                task_id=child_b, board="default", batch="batch-7",
                expected_ids=[child_a, child_b], adapter=spy, home=self.home,
                config=module.Config(enabled=True, dry_run=False),
            )
            self.assertEqual(result.outcome, "verification_created")
            self.assertEqual(len(spy.writes), 1)
            self.assertEqual(spy.writes[0]["idempotency_key"], module.review_key(
                "default", "batch-7", [child_a, child_b],
            ))
            self.assertEqual(spy.writes[0]["assignee"], "night-operator")

    def test_repeated_creation_is_idempotent(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            child_a = self._completed(conn, batch="batch-8")
            child_b = self._completed(conn, batch="batch-8", expected=[child_a])
            expected = [child_a, child_b]
            first = module.create_or_get_verification(
                conn, board="default", batch="batch-8", expected_ids=expected, home=self.home,
            )
            second = module.create_or_get_verification(
                conn, board="default", batch="batch-8", expected_ids=expected, home=self.home,
            )
            self.assertEqual(first, second)
            count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
                (module.review_key("default", "batch-8", expected),),
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_missing_batch_creates_no_guessed_review(self) -> None:
        module = _plugin()
        result = module.handle_completed(
            task_id="t-no-batch", board="default", batch=None,
            expected_ids=["t-no-batch"], adapter=Spy(),
            config=module.Config(enabled=True, dry_run=False),
        )
        self.assertEqual(result.outcome, "decision_required")
        self.assertTrue(result.requires_human)

    def test_incomplete_group_is_not_dispatched(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            done = self._completed(conn, batch="batch-9")
            pending = kanban_db.create_task(
                conn, title="still-running", assignee="worker-a", initial_status="running",
            )
            result = module.handle_completed(
                task_id=done, board="default", batch="batch-9",
                expected_ids=[done, pending], adapter=Spy(), home=self.home,
                config=module.Config(enabled=True, dry_run=False),
            )
            self.assertEqual(result.outcome, "waiting_for_batch")

    def test_decide_preserves_redaction_invariant_exact_artifacts(self) -> None:
        module = _plugin()
        long_identity = "C:/evidence/" + ("x" * 900) + ".bin"
        card = module.ReviewEvidence(
            verification_task_id="v-exact", operator_profile="night-operator",
            implementation_profiles=["worker-a"], verified_parent_ids=["p1"],
            evidence=["event:reviewed"], artifacts=[long_identity], residual_risk=[],
        )

        outcome = module.decide(
            card, "pass", implementation_profile="worker-a",
            evidence=["event:reviewed"], artifacts=[long_identity, long_identity],
        )
        self.assertEqual(outcome.terminal_state, "complete")
        self.assertEqual(outcome.metadata["artifacts"], [long_identity, long_identity])

        secret_like = "C:/evidence/api_key=" + ("H" * 32) + ".bin"
        with self.assertRaisesRegex(ValueError, "exact artifacts"):
            module.decide(
                card, "pass", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=[secret_like],
            )

        padded_parent_card = module.ReviewEvidence(
            verification_task_id="v-padded-parent", operator_profile="night-operator",
            implementation_profiles=["worker-a"],
            verified_parent_ids=[" p1 "], evidence=["event:reviewed"],
            artifacts=["C:/workspace/proof.json"], residual_risk=[],
        )
        with self.assertRaisesRegex(ValueError, "exact verified parents"):
            module.decide(
                padded_parent_card, "pass", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=["C:/workspace/proof.json"],
            )

    def test_outcome_requires_evidence_and_never_approves_implementation(self) -> None:
        module = _plugin()
        card = module.ReviewEvidence(
            verification_task_id="v1", operator_profile="night-operator",
            implementation_profiles=["worker-a"], verified_parent_ids=["p1", "p2"],
            evidence=["artifact:p1.py", "artifact:p2.json"],
            artifacts=["C:/workspace/p1.py", "C:/workspace/p2.json"], residual_risk=[],
        )
        with self.assertRaisesRegex(ValueError, "evidence"):
            module.decide(card, "pass", implementation_profile="worker-a", evidence=[])
        with self.assertRaisesRegex(ValueError, "self"):
            module.decide(
                card, "pass", implementation_profile="night-operator", evidence=card.evidence,
                artifacts=card.artifacts,
            )
        outcome = module.decide(
            card, "pass", implementation_profile="worker-a", evidence=card.evidence,
            artifacts=card.artifacts,
        )
        self.assertEqual(outcome.terminal_state, "complete")

    def test_public_outcome_redacts_secret_like_metadata_before_return(self) -> None:
        module = _plugin()
        secret = "api_key=" + "F" * 32
        card = module.ReviewEvidence(
            verification_task_id="v-redacted", operator_profile="night-operator",
            implementation_profiles=["worker-a"], verified_parent_ids=["p1"],
            evidence=[secret], artifacts=["C:/workspace/proof.json"], residual_risk=[secret],
        )
        outcome = module.decide(
            card, "pass", implementation_profile="worker-a",
            evidence=card.evidence, artifacts=card.artifacts,
        )
        rendered = repr(outcome.__dict__)
        self.assertNotIn(secret, rendered)
        self.assertIn("api_key=", rendered)
        self.assertIn("***", rendered)

    def test_changes_for_review_parent_use_same_card_lifecycle(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="candidate", assignee="worker-a", initial_status="running",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            claimed = kanban_db.claim_review_task(conn, task_id, claimer="night-operator")
            self.assertIsNotNone(claimed)
            result = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="changes",
                reason="1. Restore the missing artifact. 2. Attach its hash.",
                implementation_profile="worker-a",
            )
            self.assertEqual(result.terminal_state, "changes")
            self.assertEqual(module.get_task(conn, task_id).status, "ready")

    def test_done_parent_is_never_reopened_and_gets_remediation(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._completed(conn, batch="batch-10")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-10", expected_ids=[parent],
                operator_profile="night-operator",
            )
            result = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="changes",
                reason="1. Re-run the release check. 2. Publish the evidence hash.",
                implementation_profile="worker-a", source_parent_id=parent,
                source_handoff="handoff from parent",
            )
            self.assertEqual(result.terminal_state, "remediation")
            self.assertEqual(module.get_task(conn, parent).status, "done")
            rows = conn.execute(
                "SELECT status FROM tasks WHERE title LIKE 'Remediation:%'"
            ).fetchall()
            self.assertEqual(len(rows), 1)

    def test_review_pass_writes_structured_evidence_to_durable_run(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent_a = self._completed(conn, batch="batch-review-parents")
            parent_b = self._completed(conn, batch="batch-review-parents", expected=[parent_a])
            task_id = kanban_db.create_task(
                conn, title="review candidate", assignee="worker-a", initial_status="running",
                parents=(parent_a, parent_b), board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            attachment_id = self._logical_attachment(conn, task_id, "proof.json")
            self.assertGreater(attachment_id, 0)
            result = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="reviewed handoff", implementation_profile="worker-a",
                evidence=["event:reviewed", "artifact:proof.json"],
                artifacts=["proof.json"], verified_parent_ids=[parent_a, parent_b],
                residual_risk=["manual smoke test remains"],
            )
            self.assertEqual(result.terminal_state, "complete")
            run = kanban_db.latest_run(conn, task_id)
            self.assertCountEqual(run.metadata["verified_parent_ids"], [parent_a, parent_b])
            self.assertEqual(run.metadata["verification_evidence"], ["event:reviewed", "artifact:proof.json"])
            self.assertEqual(run.metadata["artifacts"], ["proof.json"])
            self.assertEqual(run.metadata["artifact_record_presence"], "logical_only")
            self.assertEqual(run.metadata["artifact_physical_presence"], "unverified")
            self.assertEqual(module.get_task(conn, task_id).status, "done")

    def test_pass_without_evidence_is_blocked_and_does_not_close(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="review candidate", assignee="worker-a", initial_status="running",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            result = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="reviewed handoff", implementation_profile="worker-a", evidence=[], artifacts=[],
            )
            self.assertEqual(result.terminal_state, "blocked")
            self.assertTrue(result.requires_human)
            self.assertNotEqual(module.get_task(conn, task_id).status, "done")

    def test_apply_outcome_rejects_operator_self_approval(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="self-approval candidate", assignee="night-operator",
                initial_status="running",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="night-operator"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            result = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="self approval", implementation_profile="night-operator",
                evidence=["event:self"], artifacts=["proof.json"],
            )
            self.assertEqual(result.terminal_state, "blocked")
            self.assertTrue(result.requires_human)
            self.assertEqual(module.get_task(conn, task_id).status, "running")

    def test_ambiguity_blocks_review_and_renders_minimal_notice(self) -> None:
        module = _plugin()
        card = module.ReviewEvidence(
            verification_task_id="v2", operator_profile="night-operator",
            implementation_profiles=["worker-a"], verified_parent_ids=["p1", "p2"],
            evidence=[], artifacts=[], residual_risk=["unclear"],
        )
        result = module.decide(
            card, "needs_input", implementation_profile="worker-a", evidence=[],
            next_steps=[], question="Choose A or B", options=["A", "B"],
        )
        self.assertEqual(result.terminal_state, "blocked")
        notice = module.render_notice("default", card, result)
        self.assertEqual(set(notice), {
            "board", "task_id", "batch_id", "summary", "reason", "question", "options", "link",
        })

    def test_handle_completed_redacts_caller_facing_dry_run_metadata(self) -> None:
        module = _plugin()
        secret = "api_key=" + "H" * 32
        outcome = module.handle_completed(
            task_id=secret, board="default", batch=secret,
            expected_ids=[secret], config=module.Config(enabled=True, dry_run=True),
        )
        rendered = repr(outcome.__dict__)
        self.assertNotIn(secret, rendered)
        self.assertIn("api_key=", rendered)
        self.assertIn("***", rendered)

    def test_dry_run_never_writes_and_reconciliation_is_bounded(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            self._completed(conn, batch="batch-11")
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcomes = module.reconcile(
                conn, config=module.Config(enabled=True, dry_run=True, board="default", max_items=3),
                home=self.home,
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(before, after)
        self.assertLessEqual(len(outcomes), 3)

    def test_forbidden_tools_are_denied_by_policy(self) -> None:
        self.assertEqual(_plugin().tool_policy(), {
            "allowed": ["kanban_read", "kanban_review", "kanban_followup"],
            "denied": ["terminal", "browser", "credentials", "deployment", "arbitrary_external_communication"],
            "approvals": {
                "cron_mode": "deny", "single_query_mode": "deny", "unattended_mode": "deny",
            },
        })


if __name__ == "__main__":
    unittest.main()
