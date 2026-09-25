"""Integration/negative contract for the native Night Operator plugin."""
from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_constants import get_scratch_dir
from hermes_cli import plugins


class Spy:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> str:
        self.writes.append(kwargs)
        return str(kwargs.get("idempotency_key", "generated"))

    def notify(self, **payload: Any) -> None:
        self.notices.append(payload)


def _plugin():
    return importlib.import_module("plugins.night_operator")


def _create_verification_in_process(
    db_path: str,
    expected_ids: list[str],
    barrier: Any,
    results: Any,
) -> None:
    try:
        module = _plugin()
        with kbc.connect_closing(db_path=Path(db_path)) as conn:
            barrier.wait(timeout=20)
            task_id = module.create_or_get_verification(
                conn,
                board="default",
                batch="batch-multiprocess",
                expected_ids=expected_ids,
            )
        results.put(("ok", task_id))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


class NightOperatorIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="night-operator-integration-", dir=get_scratch_dir(prune=False),
        )
        self.home = Path(self.tmp.name)
        self.db = self.home / "kanban.db"
        # Every contract must use this test's own board even when the invoking
        # shell already exports an acceptance or live HERMES_HOME.
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

    def _done(self, conn, *, title: str, batch: str, expected: list[str] | None = None) -> str:
        task_id = kanban_db.create_task(
            conn, title=title, body="worker handoff", assignee="worker-a",
            initial_status="running", board="default",
        )
        self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
        self.assertTrue(kanban_db.complete_task(
            conn, task_id, summary="worker handoff", metadata={
                "operator_batch": batch,
                "operator_expected_ids": expected,
                "operator_group_ready": True,
            },
        ))
        return task_id

    def _logical_attachment(self, conn, task_id: str, filename: str) -> int:
        """Create durable attachment metadata without asserting physical blob presence."""
        stored_path = self.home / "logical-attachments" / task_id / filename
        return kanban_db.add_attachment(
            conn, task_id, filename=filename, stored_path=str(stored_path),
            content_type="application/octet-stream", size=17, uploaded_by="worker-a",
        )

    def test_real_registration_registers_hooks_and_cli_without_tools(self) -> None:
        module = _plugin()
        class Ctx:
            def __init__(self) -> None:
                self.hooks: list[tuple[str, Any]] = []
                self.cli: list[dict[str, Any]] = []
            def register_hook(self, name: str, callback: Any) -> None:
                self.hooks.append((name, callback))
            def register_cli_command(self, **entry: Any) -> None:
                self.cli.append(entry)
        ctx = Ctx()
        module.register(ctx)
        self.assertEqual({name for name, _ in ctx.hooks}, {
            "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked",
            "on_kanban_dispatch_tick", "pre_tool_call",
        })
        self.assertEqual([entry["name"] for entry in ctx.cli], ["night-operator"])
        parser = __import__("argparse").ArgumentParser()
        ctx.cli[0]["setup_fn"](parser)
        args = parser.parse_args(["sweep", "--board", "default", "--json"])
        self.assertEqual(args.night_operator_action, "sweep")
        self.assertTrue(args.json)
        self.assertFalse(args.write)

    def test_cli_sweep_uses_config_defaults_and_never_writes_without_write_flag(self) -> None:
        module = _plugin()
        import argparse
        parser = argparse.ArgumentParser()
        module.register_cli(parser)
        args = parser.parse_args(["sweep", "--json"])
        self.assertFalse(args.write)
        self.assertTrue(args.json)
        self.assertEqual(args.board, "default")
        self.assertEqual(args.max_items, 100)

    def test_cli_sweep_dry_run_does_not_mutate_temp_board(self) -> None:
        module = _plugin()
        import argparse
        parser = argparse.ArgumentParser()
        module.register_cli(parser)
        with kbc.connect_closing(db_path=self.db) as conn:
            a = self._done(conn, title="parent", batch="batch-cli")
            b = self._done(conn, title="child", batch="batch-cli", expected=[a])
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        args = parser.parse_args(["sweep", "--board", "default", "--home", str(self.home), "--json"])
        before_stdout = __import__("contextlib").redirect_stdout(__import__("io").StringIO())
        with before_stdout:
            result = module.night_operator_command(args)
        with kbc.connect_closing(db_path=self.db) as conn:
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(result, 0)
        self.assertEqual(after, before)

    def test_cli_json_redacts_outcome_metadata_at_the_public_boundary(self) -> None:
        module = _plugin()
        secret = "api_key=" + "G" * 32
        parser = argparse.ArgumentParser()
        module.register_cli(parser)
        args = parser.parse_args(["sweep", "--home", str(self.home), "--json"])
        outcome = module.Outcome(
            "dry_run", reason=secret,
            metadata={"batch_id": secret, "parent_ids": [secret]},
        )
        stream = __import__("io").StringIO()
        with mock.patch.object(module, "reconcile", return_value=[outcome]), \
                __import__("contextlib").redirect_stdout(stream):
            result = module.night_operator_command(args)
        rendered = stream.getvalue()
        self.assertEqual(result, 0)
        self.assertNotIn(secret, rendered)
        self.assertIn("api_key=", rendered)
        self.assertIn("***", rendered)

    def test_dry_run_missing_batch_never_creates_decision_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="done without batch", assignee="worker-a", initial_status="running",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.complete_task(conn, task_id, summary="handoff"))
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcome = module.handle_completed(
                task_id=task_id, board="default", batch=None,
                expected_ids=[task_id], conn=conn,
                config=module.Config(enabled=True, dry_run=True),
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(outcome.terminal_state, "decision_required")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(before, after)

    def test_dry_run_never_writes_for_unproven_batch_membership(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = self._done(conn, title="done", batch="batch-provenance")
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcome = module.handle_completed(
                task_id=task_id, board="default", batch="batch-provenance",
                expected_ids=["t_not_this_task"], conn=conn,
                config=module.Config(enabled=True, dry_run=True),
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(outcome.terminal_state, "decision_required")
        self.assertTrue(outcome.requires_human)
        self.assertIsNone(outcome.metadata["decision_task_id"])
        self.assertEqual(before, after)

    def test_done_follow_up_creates_card_without_reopening_parent(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="done follow-up parent", batch="batch-done-follow-up")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-done-follow-up", expected_ids=[parent],
                operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="follow_up",
                reason="Run the follow-up acceptance check", source_parent_id=parent,
                source_handoff="preserve the completed handoff", implementation_profile="worker-a",
            )
            self.assertEqual(outcome.terminal_state, "follow_up")
            self.assertEqual(module.get_task(conn, parent).status, "done")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'Remediation:%'").fetchone()[0],
                1,
            )
            self.assertTrue(outcome.metadata.get("followup_task_id"))

    def test_done_pass_requires_evidence_and_closes_only_verification(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-pass-done")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-pass-done", expected_ids=[parent],
                operator_profile="night-operator",
            )
            claimed = kanban_db.claim_task(conn, verification, claimer="night-operator")
            self.assertIsNotNone(claimed)
            attachment_id = self._logical_attachment(conn, parent, "proof.json")
            self.assertGreater(attachment_id, 0)
            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="reviewed handoff", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["proof.json"], verified_parent_ids=[parent],
            )
            self.assertEqual(outcome.terminal_state, "complete")
            self.assertEqual(module.get_task(conn, parent).status, "done")
            self.assertEqual(module.get_task(conn, verification).status, "done")
            run = kanban_db.latest_run(conn, verification)
            self.assertEqual(run.metadata["verification_evidence"], ["event:reviewed"])
            self.assertEqual(run.metadata["artifacts"], ["proof.json"])

    def test_pass_accepts_filename_matched_to_durable_attachment_record_without_stat(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-attachment-present")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-attachment-present",
                expected_ids=[parent], operator_profile="night-operator",
            )
            claimed = kanban_db.claim_task(conn, verification, claimer="night-operator")
            self.assertIsNotNone(claimed)
            missing_blob = self.home / "logical-only" / "proof.json"
            self.assertFalse(missing_blob.exists())
            attachment_id = kanban_db.add_attachment(
                conn, parent, filename="proof.json", stored_path=str(missing_blob),
                content_type="application/json", size=17, uploaded_by="worker-a",
            )
            self.assertGreater(attachment_id, 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="logical attachment record verified", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["proof.json"], verified_parent_ids=[parent],
            )
            verification_run = kanban_db.latest_run(conn, verification)
        self.assertEqual(outcome.terminal_state, "complete")
        self.assertEqual(verification_run.metadata["artifact_record_presence"], "logical_only")
        self.assertEqual(verification_run.metadata["artifact_record_count"], 1)
        self.assertEqual(verification_run.metadata["artifact_physical_presence"], "unverified")
        self.assertFalse(missing_blob.exists())

    def test_pass_requires_native_attachment_root_regular_file_with_exact_size_and_sha256(self) -> None:
        module = _plugin()
        payload = b'{"proof": true}\n'
        expected_sha = __import__("hashlib").sha256(payload).hexdigest()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="physical parent", batch="batch-physical")
            task_id = kanban_db.create_task(
                conn, title="physical review", assignee="worker-a", initial_status="running",
                parents=(parent,), board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            blob = kanban_db.task_attachments_dir(task_id, board="default") / "proof.json"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(payload)
            kanban_db.add_attachment(
                conn, task_id, filename="proof.json", stored_path=str(blob.resolve()),
                content_type="application/json", size=len(payload), uploaded_by="worker-a",
            )
            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="physical evidence verified", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=[str(blob.resolve())],
                verified_parent_ids=[parent], physical_sha256={str(blob.resolve()): expected_sha},
            )
            run = kanban_db.latest_run(conn, task_id)
            status = module.get_task(conn, task_id).status
        self.assertEqual(outcome.terminal_state, "complete")
        self.assertEqual(status, "done")
        self.assertEqual(run.metadata["artifact_physical_presence"], "verified")
        self.assertEqual(run.metadata["artifact_sha256"], {str(blob.resolve()): expected_sha})
        self.assertEqual(run.metadata["artifact_record_presence"], "logical_and_physical")
        self.assertEqual(run.metadata["artifact_record_count"], 1)

    def test_physical_artifact_outside_native_root_is_blocked(self) -> None:
        module = _plugin()
        payload = b"outside-root\n"
        expected_sha = __import__("hashlib").sha256(payload).hexdigest()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="escape parent", batch="batch-escape")
            task_id = kanban_db.create_task(
                conn, title="escape review", assignee="worker-a", initial_status="running",
                parents=(parent,), board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            outside = self.home / "outside-native-root" / "proof.json"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_bytes(payload)
            kanban_db.add_attachment(
                conn, task_id, filename="proof.json", stored_path=str(outside.resolve()),
                content_type="application/octet-stream", size=len(payload), uploaded_by="worker-a",
            )
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="attempted traversal", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=[str(outside.resolve())],
                verified_parent_ids=[parent], physical_sha256={str(outside.resolve()): expected_sha},
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            status = module.get_task(conn, task_id).status
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertIn("SHA-256", outcome.reason)
        self.assertEqual(status, "running")
        self.assertEqual(after, before)

    def test_physical_artifact_with_mismatched_sha_is_blocked(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="mismatch parent", batch="batch-mismatch")
            task_id = kanban_db.create_task(
                conn, title="mismatch review", assignee="worker-a", initial_status="running",
                parents=(parent,), board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            blob = kanban_db.task_attachments_dir(task_id, board="default") / "proof.json"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(b"actual bytes\n")
            kanban_db.add_attachment(
                conn, task_id, filename="proof.json", stored_path=str(blob.resolve()),
                content_type="application/octet-stream", size=len(b"actual bytes\n"),
                uploaded_by="worker-a",
            )
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="declared digest does not match", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=[str(blob.resolve())],
                verified_parent_ids=[parent],
                physical_sha256={str(blob.resolve()): "0" * 64},
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            status = module.get_task(conn, task_id).status
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(status, "running")
        self.assertEqual(after, before)

    def test_named_bounded_verification_check_passes_and_shell_command_is_blocked(self) -> None:
        module = _plugin()
        payload = b'{"ok": 1}'
        digest = __import__("hashlib").sha256(payload).hexdigest()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="checks parent", batch="batch-checks")
            task_id = kanban_db.create_task(
                conn, title="checks review", assignee="worker-a", initial_status="running",
                parents=(parent,), board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            blob = kanban_db.task_attachments_dir(task_id, board="default") / "proof.json"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(payload)
            kanban_db.add_attachment(
                conn, task_id, filename="proof.json", stored_path=str(blob.resolve()),
                content_type="application/json", size=len(payload), uploaded_by="worker-a",
            )
            good = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="named checks only", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=[str(blob.resolve())],
                verified_parent_ids=[parent], physical_sha256={str(blob.resolve()): digest},
                verification_checks=["json", "sha256"],
            )
            run = kanban_db.latest_run(conn, task_id)
            self.assertEqual(good.terminal_state, "complete")
            self.assertEqual(run.metadata["verification_check_names"], ["json", "sha256"])
            self.assertEqual(
                run.metadata["verification_checks"],
                [{"artifact": str(blob.resolve()), "checks": {"json": True, "sha256": True}}],
            )

        with kbc.connect_closing(db_path=self.db) as conn:
            parent2 = self._done(conn, title="command parent", batch="batch-command")
            task2 = kanban_db.create_task(
                conn, title="command review", assignee="worker-a", initial_status="running",
                parents=(parent2,), board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task2, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task2, summary="ready for review", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task2, claimer="night-operator"))
            blob2 = kanban_db.task_attachments_dir(task2, board="default") / "proof.json"
            blob2.parent.mkdir(parents=True, exist_ok=True)
            blob2.write_bytes(payload)
            kanban_db.add_attachment(
                conn, task2, filename="proof.json", stored_path=str(blob2.resolve()),
                content_type="application/json", size=len(payload), uploaded_by="worker-a",
            )
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            refused = module.apply_outcome(
                conn, verification_task_id=task2, source_status="review", decision="pass",
                reason="shell attempt", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=[str(blob2.resolve())],
                verified_parent_ids=[parent2],
                physical_sha256={str(blob2.resolve()): digest},
                verification_checks=["bash -c 'curl example.com'"],
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            status2 = module.get_task(conn, task2).status
        self.assertEqual(refused.terminal_state, "blocked")
        self.assertTrue(refused.requires_human)
        self.assertIn("closed allowlist", refused.reason)
        self.assertEqual(status2, "running")
        self.assertEqual(after, before)

    def test_pre_tool_call_blocks_denied_tools_only_inside_the_operator_profile(self) -> None:
        module = _plugin()
        class Ctx:
            plugin_id = "night-operator"
            def __init__(self) -> None:
                self.hooks: list[tuple[str, Any]] = []
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "enforce_tools": True,
                        "operator_profile": "night-operator"}.get(key, default)
            def register_hook(self, name: str, callback: Any) -> None:
                self.hooks.append((name, callback))
            def register_cli_command(self, **entry: Any) -> None:
                pass
        ctx = Ctx()
        module.register(ctx)
        self.assertIn("pre_tool_call", {name for name, _ in ctx.hooks})

        with mock.patch.object(module, "_operator_profile_active", return_value="night-operator"):
            denied = module._on_pre_tool_call(
                ctx, tool_name="terminal", args={"command": "rm -rf /"}, session_id="s1",
            )
            self.assertIsNotNone(denied)
            self.assertEqual(denied["action"], "block")
            self.assertIn("terminal", denied["message"])

            for tool in ("browser_use", "vault_list", "web_search", "cronjob_manage", "execute_code"):
                directive = module._on_pre_tool_call(
                    ctx, tool_name=tool, args={}, session_id="s1",
                )
                self.assertIsNotNone(directive, tool)
                self.assertEqual(directive["action"], "block", tool)

            for tool in ("kanban_show", "kanban_comment", "kanban_attachments", "read_file"):
                self.assertIsNone(
                    module._on_pre_tool_call(ctx, tool_name=tool, args={}, session_id="s1"), tool,
                )

    def test_pre_tool_call_stays_silent_in_a_foreign_profile(self) -> None:
        module = _plugin()
        class Ctx:
            plugin_id = "night-operator"
            def __init__(self) -> None:
                self.hooks: list[tuple[str, Any]] = []
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "enforce_tools": True,
                        "operator_profile": "night-operator"}.get(key, default)
            def register_hook(self, name: str, callback: Any) -> None:
                self.hooks.append((name, callback))
            def register_cli_command(self, **entry: Any) -> None:
                pass
        ctx = Ctx()
        module.register(ctx)
        with mock.patch.object(module, "_operator_profile_active", return_value="ibf-operator"):
            self.assertIsNone(module._on_pre_tool_call(
                ctx, tool_name="terminal", args={"command": "echo hi"}, session_id="s1",
            ))

        class Disabled(Ctx):
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "enforce_tools": False}.get(key, default)
        with mock.patch.object(module, "_operator_profile_active", return_value="night-operator"):
            self.assertIsNone(module._on_pre_tool_call(
                Disabled(), tool_name="terminal", args={"command": "echo hi"}, session_id="s1",
            ))

        class Off(Ctx):
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": False}.get(key, default)
        with mock.patch.object(module, "_operator_profile_active", return_value="night-operator"):
            self.assertIsNone(module._on_pre_tool_call(
                Off(), tool_name="terminal", args={"command": "echo hi"}, session_id="s1",
            ))

    def test_artifact_record_count_counts_unique_declared_identities(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="duplicate artifact records", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            first = self._logical_attachment(conn, task_id, "first.json")
            duplicate_first = self._logical_attachment(conn, task_id, "first.json")
            second = self._logical_attachment(conn, task_id, "second.json")
            self.assertEqual(len({first, duplicate_first, second}), 3)

            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review",
                decision="pass", reason="duplicate logical records",
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["first.json", "second.json"],
            )

        self.assertEqual(outcome.terminal_state, "complete")
        self.assertEqual(outcome.metadata["artifact_record_count"], 2)
        self.assertEqual(outcome.metadata["artifact_record_presence"], "logical_only")
        self.assertEqual(outcome.metadata["artifact_physical_presence"], "unverified")

    def test_pass_preserves_exact_artifact_identities_in_durable_output(self) -> None:
        module = _plugin()
        long_identity = "C:/evidence/" + ("x" * 900) + ".bin"
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="exact durable artifact round trip", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            self.assertGreater(
                self._logical_attachment(conn, task_id, long_identity),
                0,
            )

            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review",
                decision="pass", reason="exact durable round trip",
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=[long_identity, long_identity],
            )
            run = kanban_db.latest_run(conn, task_id)

        self.assertEqual(outcome.terminal_state, "complete")
        self.assertEqual(outcome.metadata["artifacts"], [long_identity, long_identity])
        self.assertEqual(run.metadata["artifacts"], [long_identity, long_identity])
        self.assertEqual(outcome.metadata["artifact_record_count"], 1)
        self.assertEqual(outcome.metadata["artifact_physical_presence"], "unverified")

    def test_pass_rejects_redacted_artifact_identity_without_closing(self) -> None:
        module = _plugin()
        secret_like = "C:/evidence/api_key=" + ("H" * 32) + ".bin"
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="redacted exact artifact", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            self.assertGreater(
                self._logical_attachment(conn, task_id, secret_like),
                0,
            )

            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review",
                decision="pass", reason="secret-like artifact",
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=[secret_like],
            )
            after = module.get_task(conn, task_id)

        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertNotIn(secret_like, repr(outcome.metadata))

    def test_pass_requires_exact_raw_artifact_identity(self) -> None:
        module = _plugin()
        cases = (
            ("proof.json", str(self.home / "missing" / "proof.json"), " proof.json "),
            ("x" * 1_004, str(self.home / "missing" / ("x" * 1_004)), "x" * 1_000),
            (
                "proof.json",
                "C:/evidence/" + ("x" * 4_000) + ".bin",
                ("C:/evidence/" + ("x" * 4_000) + ".bin")[:4_000],
            ),
        )
        for index, (stored_name, stored_path, declared_name) in enumerate(cases):
            with self.subTest(index=index):
                with kbc.connect_closing(db_path=self.db) as conn:
                    parent = self._done(
                        conn, title="parent", batch=f"batch-exact-{index}",
                    )
                    verification = module.create_or_get_verification(
                        conn, board="default", batch=f"batch-exact-{index}",
                        expected_ids=[parent], operator_profile="night-operator",
                    )
                    self.assertIsNotNone(
                        kanban_db.claim_task(conn, verification, claimer="night-operator")
                    )
                    attachment_id = kanban_db.add_attachment(
                        conn, parent, filename=stored_name, stored_path=stored_path,
                        content_type="application/octet-stream", size=17,
                        uploaded_by="worker-a",
                    )
                    self.assertGreater(attachment_id, 0)

                    outcome = module.apply_outcome(
                        conn, verification_task_id=verification,
                        source_status="done", decision="pass",
                        reason="artifact identity must be exact",
                        source_parent_id=parent, implementation_profile="worker-a",
                        evidence=["event:reviewed"], artifacts=[declared_name],
                        verified_parent_ids=[parent],
                    )
                    after = module.get_task(conn, verification)
                self.assertEqual(outcome.terminal_state, "blocked")
                self.assertTrue(outcome.requires_human)
                self.assertEqual(after.status, "running")
                if index == 0:
                    self.assertIn("redaction-invariant", outcome.reason)
                else:
                    self.assertIn("attachment record", outcome.reason)

    def test_pass_blocks_when_artifact_has_no_matching_durable_attachment_record(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-attachment-missing")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-attachment-missing",
                expected_ids=[parent], operator_profile="night-operator",
            )
            claimed = kanban_db.claim_task(conn, verification, claimer="night-operator")
            self.assertIsNotNone(claimed)

            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="unproven artifact", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["missing.json"], verified_parent_ids=[parent],
            )
            after = module.get_task(conn, verification)
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIsNotNone(after.current_run_id)
        self.assertIn("attachment record", outcome.reason)

    def test_pass_requires_every_declared_artifact_to_have_a_record(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-artifact-partial")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-artifact-partial",
                expected_ids=[parent], operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            attachment_id = self._logical_attachment(conn, parent, "proof.json")
            self.assertGreater(attachment_id, 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="one declared artifact is unproven", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["proof.json", "missing.json"], verified_parent_ids=[parent],
            )
            after = module.get_task(conn, verification)
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIn("attachment record", outcome.reason)

    def test_pass_rejects_matching_attachment_record_on_unrelated_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-artachment-scope")
            unrelated = self._done(conn, title="unrelated", batch="batch-unrelated")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-artachment-scope",
                expected_ids=[parent], operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            attachment_id = self._logical_attachment(conn, unrelated, "proof.json")
            self.assertGreater(attachment_id, 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="unrelated attachment record is not authoritative", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["proof.json"], verified_parent_ids=[parent],
            )
            after = module.get_task(conn, verification)
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIn("verification card or its native parents", outcome.reason)

    def test_pass_rejects_done_source_that_is_not_a_native_parent(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-parent-binding")
            unrelated = self._done(conn, title="unrelated done", batch="batch-unrelated-parent")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-parent-binding",
                expected_ids=[parent], operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            attachment_id = self._logical_attachment(conn, unrelated, "proof.json")
            self.assertGreater(attachment_id, 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="done source must be a native parent", source_parent_id=unrelated,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["proof.json"], verified_parent_ids=[parent],
            )
            after = module.get_task(conn, verification)
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIn("native parent", outcome.reason)

    def test_done_non_pass_rejects_source_that_is_not_a_native_parent(self) -> None:
        module = _plugin()
        decisions = ("changes", "follow_up", "needs_input", "blocked")
        for index, decision in enumerate(decisions):
            with self.subTest(decision=decision):
                with kbc.connect_closing(db_path=self.db) as conn:
                    batch = f"batch-non-pass-parent-{index}"
                    parent = self._done(conn, title="parent", batch=batch)
                    unrelated = self._done(conn, title="unrelated", batch=f"{batch}-other")
                    verification = module.create_or_get_verification(
                        conn, board="default", batch=batch, expected_ids=[parent],
                        operator_profile="night-operator",
                    )
                    adapter = Spy()

                    outcome = module.apply_outcome(
                        conn, verification_task_id=verification,
                        source_status="done", decision=decision,
                        reason="human decision required",
                        source_parent_id=unrelated,
                        implementation_profile="worker-a",
                        adapter=adapter,
                    )

                self.assertEqual(outcome.terminal_state, "blocked")
                self.assertTrue(outcome.requires_human)
                self.assertEqual(adapter.writes, [])

    def test_apply_outcome_rejects_spoofed_implementation_profile(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            review_task = kanban_db.create_task(
                conn, title="spoofed review implementer", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, review_task, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, review_task, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(
                kanban_db.claim_review_task(conn, review_task, claimer="night-operator")
            )
            self.assertGreater(
                self._logical_attachment(conn, review_task, "review-proof.json"), 0,
            )
            review_outcome = module.apply_outcome(
                conn, verification_task_id=review_task, source_status="review",
                decision="pass", reason="spoofed review implementer",
                implementation_profile="fake-other", evidence=["event:reviewed"],
                artifacts=["review-proof.json"], verified_parent_ids=[],
            )
            review_after = module.get_task(conn, review_task)

        self.assertEqual(review_outcome.terminal_state, "blocked")
        self.assertTrue(review_outcome.requires_human)
        self.assertEqual(review_after.status, "running")

        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="spoofed done parent", batch="batch-spoof-profile")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-spoof-profile", expected_ids=[parent],
                operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            self.assertGreater(self._logical_attachment(conn, parent, "done-proof.json"), 0)
            done_outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done",
                decision="pass", reason="spoofed done implementer",
                source_parent_id=parent, implementation_profile="fake-other",
                evidence=["event:reviewed"], artifacts=["done-proof.json"],
                verified_parent_ids=[parent],
            )
            done_after = module.get_task(conn, verification)

        self.assertEqual(done_outcome.terminal_state, "blocked")
        self.assertTrue(done_outcome.requires_human)
        self.assertEqual(done_after.status, "running")

    def test_implementation_provenance_read_is_bounded(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            review_task = kanban_db.create_task(
                conn, title="bounded review provenance", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, review_task, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, review_task, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(
                kanban_db.claim_review_task(conn, review_task, claimer="night-operator")
            )
            self.assertGreater(self._logical_attachment(conn, review_task, "review-bound.json"), 0)

            parent = self._done(conn, title="bounded done provenance", batch="batch-provenance-bound")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-provenance-bound",
                expected_ids=[parent], operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            self.assertGreater(self._logical_attachment(conn, parent, "done-bound.json"), 0)

            run_selects: list[str] = []
            conn.set_trace_callback(
                lambda sql: run_selects.append(sql)
                if "SELECT profile FROM task_runs" in sql
                else None
            )
            with mock.patch.object(
                kanban_db, "list_runs", wraps=kanban_db.list_runs,
            ) as unbounded_api:
                review_outcome = module.apply_outcome(
                    conn, verification_task_id=review_task, source_status="review",
                    decision="pass", reason="bounded review provenance",
                    implementation_profile="worker-a", evidence=["event:reviewed"],
                    artifacts=["review-bound.json"], verified_parent_ids=[],
                )
                done_outcome = module.apply_outcome(
                    conn, verification_task_id=verification, source_status="done",
                    decision="pass", reason="bounded done provenance",
                    source_parent_id=parent, implementation_profile="worker-a",
                    evidence=["event:reviewed"], artifacts=["done-bound.json"],
                    verified_parent_ids=[parent],
                )
            conn.set_trace_callback(None)

        self.assertEqual(review_outcome.terminal_state, "complete")
        self.assertEqual(done_outcome.terminal_state, "complete")
        self.assertEqual(unbounded_api.call_count, 0)
        self.assertEqual(len(run_selects), 2)
        self.assertTrue(all(" LIMIT 1" in sql for sql in run_selects))

    def test_pass_rejects_padded_claimed_parent_ids(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="exact parent source", batch="batch-exact-parent")
            task_id = kanban_db.create_task(
                conn, title="exact parent claim", assignee="worker-a",
                initial_status="running", parents=[parent], board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            self.assertGreater(self._logical_attachment(conn, task_id, "parent-proof.json"), 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review",
                decision="pass", reason="padded parent claim",
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["parent-proof.json"], verified_parent_ids=[f" {parent} "],
            )
            after = module.get_task(conn, task_id)

        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertNotIn(f" {parent} ", repr(outcome.metadata))

    def test_pass_rejects_claimed_parent_ids_that_differ_from_native_links(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="claimed parent ids candidate", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            self.assertGreater(self._logical_attachment(conn, task_id, "parent-proof.json"), 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="claimed parents are not native", implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=["parent-proof.json"],
                verified_parent_ids=["fake", "unrelated"],
            )
            after = module.get_task(conn, task_id)

        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIn("native parent", outcome.reason)

    def test_native_parent_bound_is_a_real_read_limit(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parents = [
                self._done(conn, title=f"parent-{index}", batch=f"batch-parent-bound-{index}")
                for index in range(101)
            ]
            task_id = module.create_or_get_verification(
                conn, board="default", batch="batch-parent-bound",
                expected_ids=parents, operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="night-operator"))
            self.assertIsNotNone(
                kanban_db.request_review(
                    conn, task_id, summary="ready", reviewer="reviewer-a",
                    expected_run_id=task_id and kanban_db.get_task(conn, task_id).current_run_id,
                )
            )
            self.assertIsNotNone(
                kanban_db.claim_review_task(conn, task_id, claimer="reviewer-a")
            )
            self.assertGreater(self._logical_attachment(conn, task_id, "parent-bound.json"), 0)
            link_selects: list[str] = []
            conn.set_trace_callback(
                lambda sql: link_selects.append(sql)
                if "task_links" in sql and sql.lstrip().upper().startswith("SELECT")
                else None
            )
            with mock.patch.object(
                kanban_db, "parent_ids", wraps=kanban_db.parent_ids,
            ) as unbounded_api:
                review_outcome = module.apply_outcome(
                    conn, verification_task_id=task_id, source_status="review",
                    decision="pass", reason="bounded parent read",
                    implementation_profile="night-operator", evidence=["event:reviewed"],
                    artifacts=["parent-bound.json"], verified_parent_ids=parents,
                )
                link_selects.clear()
                unbounded_api.reset_mock()
                done_adapter = Spy()
                done_outcome = module.apply_outcome(
                    conn, verification_task_id=task_id, source_status="done",
                    decision="changes", reason="bounded done parent read",
                    source_parent_id=parents[0], implementation_profile="worker-a",
                    adapter=done_adapter,
                )
            conn.set_trace_callback(None)
            after = module.get_task(conn, task_id)

        self.assertEqual(review_outcome.terminal_state, "blocked")
        self.assertTrue(review_outcome.requires_human)
        self.assertEqual(done_outcome.terminal_state, "blocked")
        self.assertTrue(done_outcome.requires_human)
        self.assertEqual(done_adapter.writes, [])
        self.assertEqual(after.status, "running")
        self.assertIn("source-card bound", review_outcome.reason)
        self.assertIn("source-card bound", done_outcome.reason)
        self.assertEqual(unbounded_api.call_count, 0)
        self.assertTrue(link_selects)
        self.assertTrue(all(" LIMIT " in sql.upper() for sql in link_selects))

    def test_artifact_record_bound_is_a_real_read_limit(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="bounded attachment reads", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="night-operator"))
            conn.executemany(
                "INSERT INTO task_attachments "
                "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        task_id,
                        "proof.json" if index == 0 else f"extra-{index}.json",
                        f"C:/missing/row-{index}.json",
                        "application/octet-stream",
                        1,
                        "worker-a",
                        1_000 + index,
                    )
                    for index in range(1_001)
                ],
            )
            attachment_selects: list[str] = []
            conn.set_trace_callback(
                lambda sql: attachment_selects.append(sql)
                if "task_attachments" in sql and sql.lstrip().upper().startswith("SELECT")
                else None
            )
            with mock.patch.object(
                kanban_db, "list_attachments", wraps=kanban_db.list_attachments,
            ) as unbounded_api:
                outcome = module.apply_outcome(
                    conn, verification_task_id=task_id, source_status="review",
                    decision="pass", reason="bounded attachment read",
                    implementation_profile="worker-a", evidence=["event:reviewed"],
                    artifacts=["proof.json"], verified_parent_ids=[],
                )
            conn.set_trace_callback(None)
            after = module.get_task(conn, task_id)

        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIn("record bound", outcome.reason)
        self.assertEqual(unbounded_api.call_count, 0)
        self.assertTrue(attachment_selects)
        self.assertTrue(all(" LIMIT " in sql.upper() for sql in attachment_selects))

    def test_done_pass_rejects_claimed_parent_ids_that_differ_from_native_links(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-done-parent-metadata")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-done-parent-metadata",
                expected_ids=[parent], operator_profile="night-operator",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, verification, claimer="night-operator"))
            self.assertGreater(self._logical_attachment(conn, parent, "done-parent-proof.json"), 0)

            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="claimed done parents are not native", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["done-parent-proof.json"], verified_parent_ids=["fake", "unrelated"],
            )
            after = module.get_task(conn, verification)

        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIn("native parent", outcome.reason)

    def test_review_pass_redacts_reason_before_native_completion(self) -> None:
        module = _plugin()
        secret_like = "api_key=" + "C" * 32
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="review candidate", assignee="worker-a", initial_status="running",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="reviewer-a",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="reviewer-a"))
            attachment_id = self._logical_attachment(conn, task_id, "proof.json")
            self.assertGreater(attachment_id, 0)
            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason=secret_like, implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=["proof.json"],
            )
            run = kanban_db.latest_run(conn, task_id)
        self.assertEqual(outcome.terminal_state, "complete")
        self.assertNotIn(secret_like, run.summary or "")
        self.assertIn("api_key=", run.summary or "")

    def test_review_pass_redacts_durable_metadata_identifiers(self) -> None:
        module = _plugin()
        secret_like = "api_key=" + "E" * 32
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="review metadata candidate", assignee="worker-a",
                initial_status="running",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="reviewer-a",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="reviewer-a"))
            attachment_id = self._logical_attachment(conn, task_id, "proof.json")
            self.assertGreater(attachment_id, 0)
            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="reviewed", implementation_profile="worker-a",
                evidence=[secret_like], artifacts=["proof.json"], residual_risk=[secret_like],
            )
            metadata = kanban_db.latest_run(conn, task_id).metadata
        self.assertEqual(outcome.terminal_state, "complete")
        self.assertNotIn(secret_like, repr(metadata))
        self.assertIn("api_key=", repr(metadata))

    def test_followup_body_redacts_source_handoff(self) -> None:
        module = _plugin()
        secret_like = "api_key=" + "D" * 32
        with kbc.connect_closing(db_path=self.db) as conn:
            source = self._done(conn, title="parent", batch="batch-handoff-redaction")
            followup = module._create_followup(
                conn, None, board="default", source_id=source, verification_id="v1",
                implementation_profile="worker-a", reason="fix it", handoff=secret_like,
            )
            task = module.get_task(conn, followup)
        self.assertIsNotNone(task)
        self.assertNotIn(secret_like, task.body or "")
        self.assertIn("api_key=", task.body or "")

    def test_review_pass_rejects_unclaimed_review_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="unclaimed review candidate", assignee="reviewer-a",
                initial_status="running",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="reviewer-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="reviewer-a",
            ))
            before = module.get_task(conn, task_id)
            result = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="reviewed", implementation_profile="reviewer-a",
                evidence=["event:reviewed"], artifacts=["proof.json"],
            )
            after = module.get_task(conn, task_id)
        self.assertEqual(result.terminal_state, "blocked")
        self.assertTrue(result.requires_human)
        self.assertEqual(before.status, "review")
        self.assertEqual(after.status, "review")
        self.assertIsNone(after.current_run_id)

    def test_custom_operator_profile_cannot_self_approve_review(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="custom reviewer candidate", assignee="reviewer-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, task_id, claimer="reviewer-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready for review", reviewer="reviewer-a",
            ))
            self.assertIsNotNone(kanban_db.claim_review_task(conn, task_id, claimer="reviewer-a"))
            result = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="pass",
                reason="self approval via configured profile",
                implementation_profile="reviewer-a",
                evidence=["event:reviewed"], artifacts=["proof.json"],
            )
            status = module.get_task(conn, task_id).status
        self.assertEqual(result.terminal_state, "blocked")
        self.assertTrue(result.requires_human)
        self.assertEqual(status, "running")

    def test_done_pass_rejects_run_owned_by_a_different_profile(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-pass-owner")
            verification = module.create_or_get_verification(
                conn, board="default", batch="batch-pass-owner",
                expected_ids=[parent], operator_profile="night-operator",
            )
            self.assertTrue(kanban_db.assign_task(conn, verification, "attacker"))
            claimed = kanban_db.claim_task(
                conn, verification, claimer="attacker",
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(module._active_run_profile(conn, verification), "attacker")
            outcome = module.apply_outcome(
                conn, verification_task_id=verification,
                source_status="done", decision="pass",
                reason="reviewed handoff", source_parent_id=parent,
                implementation_profile="worker-a",
                evidence=["event:reviewed"], artifacts=["proof.json"],
                verified_parent_ids=[parent],
            )
            after = module.get_task(conn, verification)
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(after.status, "running")
        self.assertIsNotNone(after.current_run_id)

    def test_done_pass_rejects_evidence_when_verification_has_no_operator_run(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-pass-no-run")
            verification = kanban_db.create_task(
                conn, title="Night review: batch-pass-no-run", assignee="night-operator",
                initial_status="running", board="default",
            )
            outcome = module.apply_outcome(
                conn, verification_task_id=verification, source_status="done", decision="pass",
                reason="reviewed handoff", source_parent_id=parent,
                implementation_profile="worker-a", evidence=["event:reviewed"],
                artifacts=["proof.json"], verified_parent_ids=[parent],
            )
            self.assertEqual(outcome.terminal_state, "blocked")
            self.assertTrue(outcome.requires_human)
            self.assertEqual(module.get_task(conn, verification).status, "ready")
            self.assertIsNone(module.get_task(conn, verification).current_run_id)

    def test_blocked_hook_is_observer_only_and_defers_decision_to_sweep(self) -> None:
        module = _plugin()
        class Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "board": "default"}.get(key, default)
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="blocked source", assignee="worker-a", initial_status="running", board="default",
            )
            claimed = kanban_db.claim_task(conn, task_id, claimer="worker-a")
            self.assertIsNotNone(claimed)
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            self.assertIsNone(module._on_blocked(
                Ctx(), task_id=task_id, board="default", run_id=task_id,
                assignee="worker-a", profile_name="worker-a", reason="waiting",
            ))
            after_hook = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(after_hook, before)

    def test_sweep_defers_blocked_decision_after_transaction(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="blocked source", assignee="worker-a", initial_status="running", board="default",
            )
            claimed = kanban_db.claim_task(conn, task_id, claimer="worker-a")
            self.assertIsNotNone(claimed)
            self.assertTrue(kanban_db.block_task(
                conn, task_id, reason="waiting for an input", kind="needs_input",
                expected_run_id=claimed.current_run_id,
            ))
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcomes = module.reconcile(
                conn, config=module.Config(enabled=True, dry_run=False, board="default"),
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].terminal_state, "decision_required")
        self.assertTrue(outcomes[0].requires_human)
        self.assertEqual(after, before + 1)

    def test_reconcile_creates_one_blocked_decision_and_restart_is_idempotent(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="blocked source", assignee="worker-a", initial_status="running", board="default",
            )
            claimed = kanban_db.claim_task(conn, task_id, claimer="worker-a")
            self.assertIsNotNone(claimed)
            self.assertTrue(kanban_db.block_task(
                conn, task_id, reason="waiting for an input", kind="needs_input",
                expected_run_id=claimed.current_run_id,
            ))
            config = module.Config(enabled=True, dry_run=False, board="default")
            first = module.reconcile(conn, config=config)
            second = module.reconcile(conn, config=config)
            decisions = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE title LIKE 'Operator decision required:%'"
            ).fetchone()[0]
            # A second sweep sees the same source card but must not create a second
            # human-only decision card; its idempotency key is source-stable.
            with_dry = module.reconcile(conn, config=module.Config(
                enabled=True, dry_run=True, board="default",
            ))
        self.assertTrue(first and any(item.requires_human for item in first))
        self.assertEqual(decisions, 1)
        self.assertTrue(second and any(item.terminal_state == "decision_required" for item in second))
        self.assertTrue(with_dry and any(item.requires_human for item in with_dry))

    def test_dispatch_tick_respects_native_dry_run_payload(self) -> None:
        module = _plugin()
        class Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "board": "default"}.get(key, default)
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-native-dry")
            child = self._done(conn, title="child", batch="batch-native-dry", expected=[parent])
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            module._on_dispatch_tick(Ctx(), board="default", dry_run=True, outcome="idle")
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(before, after)

    def test_dispatch_tick_throttles_live_sweep_using_plugin_state(self) -> None:
        module = _plugin()
        class State:
            def __init__(self) -> None:
                self.values: dict[str, Any] = {}
            def get(self, key: str, default: Any = None) -> Any:
                return self.values.get(key, default)
            def set(self, key: str, value: Any) -> None:
                self.values[key] = value
        class Ctx:
            def __init__(self) -> None:
                self.state = State()
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "board": "default", "reconciliation_interval_seconds": 300}.get(key, default)
        ctx = Ctx()
        with (
            mock.patch.object(module, "_open_board") as open_board,
            mock.patch.object(module, "reconcile", return_value=[]) as sweep,
        ):
            open_board.return_value.__enter__.return_value = object()
            module._on_dispatch_tick(ctx, board="default", dry_run=False, outcome="idle")
            module._on_dispatch_tick(ctx, board="default", dry_run=False, outcome="idle")
        self.assertEqual(sweep.call_count, 1)

    def test_dispatch_tick_ignores_legacy_monotonic_state_after_restart(self) -> None:
        module = _plugin()
        class State:
            def __init__(self) -> None:
                self.values: dict[str, Any] = {"reconcile:last": 10**12}
            def get(self, key: str, default: Any = None) -> Any:
                return self.values.get(key, default)
            def set(self, key: str, value: Any) -> None:
                self.values[key] = value
        class Ctx:
            def __init__(self) -> None:
                self.state = State()
            def get_config(self, key: str, default: Any = None) -> Any:
                return {
                    "enabled": True, "dry_run": False, "board": "default",
                    "reconciliation_interval_seconds": 300,
                }.get(key, default)
        ctx = Ctx()
        with (
            mock.patch.object(module, "_open_board") as open_board,
            mock.patch.object(module, "reconcile", return_value=[]) as sweep,
            mock.patch.object(module.time, "monotonic", return_value=1000.0),
            mock.patch.object(module.time, "time", return_value=2000.0),
        ):
            open_board.return_value.__enter__.return_value = object()
            module._on_dispatch_tick(ctx, board="default", dry_run=False, outcome="idle")
        self.assertEqual(sweep.call_count, 1)
        self.assertEqual(ctx.state.values["reconcile:last_unix"], 2000.0)

    def test_review_round_limit_escalates_instead_of_looping(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="review candidate", assignee="worker-a", initial_status="running", board="default",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.request_review(
                conn, task_id, summary="ready", reviewer="night-operator",
            ))
            for round_no in range(1, 4):
                claimed = kanban_db.claim_review_task(conn, task_id, claimer="night-operator")
                if round_no > 1:
                    self.assertIsNotNone(claimed)
                outcome = module.apply_outcome(
                    conn, verification_task_id=task_id, source_status="review", decision="changes",
                    reason=f"{round_no}. Re-run the bounded check.", implementation_profile="worker-a",
                    max_review_rounds=2,
                )
                if round_no < 3:
                    self.assertEqual(outcome.terminal_state, "changes")
                    self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
                    self.assertTrue(kanban_db.request_review(
                        conn, task_id, summary="ready again", reviewer="night-operator",
                    ))
            escalations = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE title LIKE 'Escalation:%'"
            ).fetchone()[0]
        self.assertEqual(outcome.terminal_state, "exhausted")
        self.assertTrue(outcome.requires_human)
        self.assertEqual(escalations, 1)

    def test_cli_config_uses_registered_plugin_id(self) -> None:
        module = _plugin()
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    category/night_operator:\n      settings:\n        enabled: true\n        dry_run: false\n        board: default\n",
            encoding="utf-8",
        )
        config = module._cli_config(self.home, plugin_id="category/night_operator")
        self.assertTrue(config.enabled)
        self.assertFalse(config.dry_run)

    def test_cli_malformed_yaml_fails_closed(self) -> None:
        module = _plugin()
        (self.home / "config.yaml").write_text("plugins: [unterminated\n", encoding="utf-8")
        parser = argparse.ArgumentParser()
        module.register_cli(parser)
        args = parser.parse_args(["sweep", "--home", str(self.home), "--json"])
        with mock.patch.object(module, "reconcile", return_value=[]):
            result = module.night_operator_command(args)
        write_args = parser.parse_args(["sweep", "--home", str(self.home), "--write", "--json"])
        write_result = module.night_operator_command(write_args)
        self.assertEqual(result, 0)
        self.assertEqual(write_result, 2)

    def test_hook_payload_contract_is_observed_through_lifecycle(self) -> None:
        module = _plugin()
        seen: list[dict[str, Any]] = []
        class Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": True, "board": "default"}.get(key, default)
            def register_hook(self, name: str, callback: Any) -> None:
                if name == "kanban_task_completed":
                    callback(task_id="t-contract", board="default", profile_name="worker-a",
                             assignee="worker-a", run_id=7, summary="handoff")
            def register_cli_command(self, **_: Any) -> None:
                pass
        with mock.patch.object(module, "_on_completed", side_effect=lambda ctx, **kwargs: seen.append(kwargs)):
            module.register(Ctx())
        self.assertEqual(seen[0]["task_id"], "t-contract")
        self.assertEqual(seen[0]["board"], "default")
        self.assertEqual(seen[0]["run_id"], 7)
        self.assertEqual(seen[0]["summary"], "handoff")

    def test_batch_and_review_keys_are_redacted_consistently(self) -> None:
        module = _plugin()
        secret = "api_key=" + "B" * 32
        key = module.review_key("default", secret, [secret])
        self.assertNotIn(secret, key)
        self.assertIn("operator-review:default:", key)

    def test_review_key_preserves_component_boundaries(self) -> None:
        module = _plugin()
        left = module.review_key("default", "x:y", ["z"])
        right = module.review_key("default", "x", ["y:z"])
        self.assertNotEqual(left, right)
        self.assertEqual(
            module.review_key("default", "batch-1", ["a", "b"]),
            module.review_key("default", "batch-1", ["b", "a"]),
        )

    def test_apply_outcome_reports_failed_block_without_audit_comment(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="not active", assignee="night-operator", initial_status="running", board="default",
            )
            outcome = module.apply_outcome(
                conn, verification_task_id=task_id, source_status="review", decision="blocked",
                reason="human input required", implementation_profile="worker-a",
            )
            comments = [c.body for c in kanban_db.list_comments(conn, task_id)]
            status = module.get_task(conn, task_id).status
        self.assertEqual(outcome.terminal_state, "blocked")
        self.assertTrue(outcome.requires_human)
        self.assertNotIn("[night-operator:blocked]", comments)
        self.assertEqual(status, "ready")

    def test_notice_redacts_secret_like_values(self) -> None:
        module = _plugin()
        secret_like = "api_key=" + "A" * 32
        card = module.ReviewEvidence(
            verification_task_id="v-secret", operator_profile="night-operator",
            implementation_profiles=["worker-a"], verified_parent_ids=["p1"],
            evidence=[], artifacts=[], batch_id="batch-secret",
        )
        notice = module.render_notice(
            "default", card,
            module.Outcome("blocked", requires_human=True,
                          reason="provider returned " + secret_like,
                          question="choose one", options=["a", "b"]),
        )
        rendered = repr(notice)
        self.assertNotIn(secret_like, rendered)
        self.assertIn("api_key=", rendered)
        self.assertIn("***", rendered)

    def test_plugin_registers_dispatch_tick_hook(self) -> None:
        module = _plugin()
        class Ctx:
            def __init__(self) -> None:
                self.hooks: list[str] = []
            def register_hook(self, name: str, _: Any) -> None:
                self.hooks.append(name)
            def register_cli_command(self, **_: Any) -> None:
                pass
        ctx = Ctx()
        module.register(ctx)
        self.assertIn("on_kanban_dispatch_tick", ctx.hooks)
    def test_strict_boolean_config_fails_closed(self) -> None:
        module = _plugin()
        class Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": "false", "dry_run": "false"}.get(key, default)
        config = module._config_from_context(Ctx())
        self.assertFalse(config.enabled)
        self.assertTrue(config.dry_run)
        with module._open_board(self.home, "default") as conn:
            self.assertEqual(module.reconcile(conn, config=config), [])

    def test_hook_and_reconcile_use_the_same_batch_key(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            first = self._done(conn, title="first", batch="batch-key")
            second = self._done(conn, title="second", batch="batch-key", expected=[first])
            expected = [first, second]
            class Ctx:
                def get_config(self, key: str, default: Any = None) -> Any:
                    return {
                        "enabled": True,
                        "dry_run": True,
                        "board": "default",
                        "review_group_key": "operator_batch",
                    }.get(key, default)
            self.assertIsNone(module._on_completed(
                Ctx(), task_id=second, board="default", run_id="run-1",
                assignee="worker-a", profile_name="worker-a", summary="handoff",
            ))
            outcomes = module.reconcile(
                conn, config=module._config_from_context(Ctx()), home=self.home,
            )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].metadata["batch_id"], "batch-key")
        self.assertEqual(
            module.review_key("default", "batch-key", expected),
            module.review_key("default", "batch-key", expected),
        )

    def test_cli_rejects_board_slug_outside_the_isolated_home(self) -> None:
        module = _plugin()
        parser = __import__("argparse").ArgumentParser()
        module.register_cli(parser)
        args = parser.parse_args([
            "sweep", "--home", str(self.home), "--board", "../escape", "--json",
        ])
        result = module.night_operator_command(args)
        self.assertEqual(result, 2)
        self.assertFalse((self.home.parent / "escape" / "kanban.db").exists())

    def test_hook_fails_closed_when_board_is_unavailable(self) -> None:
        module = _plugin()
        class Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "board": "missing"}.get(key, default)
        # A real callback must not raise or fabricate a task when its board cannot be opened.
        self.assertIsNone(module._on_completed(
            Ctx(), task_id="t-missing", board="missing",
        ))

    def test_reconcile_skips_malformed_and_missing_handoff(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            task_id = kanban_db.create_task(
                conn, title="done without handoff", assignee="worker-a", initial_status="running",
            )
            self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
            self.assertTrue(kanban_db.complete_task(conn, task_id, summary="bad handoff"))
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcomes = module.reconcile(
                conn, config=module.Config(enabled=True, dry_run=False, max_items=10),
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].terminal_state, "decision_required")
        self.assertTrue(outcomes[0].requires_human)
        self.assertEqual(after, before + 1)

    def test_dispatch_tick_honors_native_dry_run_before_any_reconcile(self) -> None:
        module = _plugin()
        class Ctx:
            def get_config(self, key: str, default: Any = None) -> Any:
                return {"enabled": True, "dry_run": False, "board": "default"}.get(key, default)
        with mock.patch.object(module, "reconcile", return_value=[]) as sweep:
            module._on_dispatch_tick(Ctx(), board="default", dry_run=True, outcome="idle")
        sweep.assert_not_called()

    def test_registered_cli_handler_uses_effective_plugin_id(self) -> None:
        module = _plugin()
        class Ctx:
            plugin_id = "category/night_operator"
            def __init__(self) -> None:
                self.cli: list[dict[str, Any]] = []
            def register_hook(self, _: str, __: Any) -> None:
                pass
            def register_cli_command(self, **entry: Any) -> None:
                self.cli.append(entry)
        ctx = Ctx()
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    category/night_operator:\n      settings:\n        enabled: true\n        dry_run: false\n        board: default\n",
            encoding="utf-8",
        )
        module.register(ctx)
        parser = argparse.ArgumentParser()
        ctx.cli[0]["setup_fn"](parser)
        args = parser.parse_args(["sweep", "--home", str(self.home), "--json"])
        with mock.patch.object(module, "reconcile", return_value=[]):
            result = ctx.cli[0]["handler_fn"](args)
        self.assertEqual(result, 0)

    def test_final_review_round_is_escalated_by_decide(self) -> None:
        module = _plugin()
        card = module.ReviewEvidence(
            verification_task_id="v-round", operator_profile="night-operator",
            implementation_profiles=["worker-a"], verified_parent_ids=["p1"],
            evidence=["event:e"], artifacts=["proof.json"], review_round=2, max_review_rounds=2,
        )
        result = module.decide(
            card, "changes", implementation_profile="worker-a",
            evidence=card.evidence, artifacts=card.artifacts,
            next_steps=["1. Fix the remaining issue."],
        )
        self.assertEqual(result.terminal_state, "exhausted")
        self.assertTrue(result.requires_human)

    def test_reconcile_reserves_capacity_for_completed_batches(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            done_id = kanban_db.create_task(
                conn, title="done parent", body="worker handoff", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, done_id, claimer="worker-a"))
            self.assertTrue(kanban_db.complete_task(
                conn, done_id, summary="worker handoff", metadata={
                    "operator_batch": "batch-done-priority",
                    "operator_expected_ids": [done_id],
                    "operator_group_ready": True,
                },
            ))
            self.assertEqual(module.get_task(conn, done_id).status, "done")
            blocked_id = kanban_db.create_task(
                conn, title="blocked candidate", assignee="worker-a", initial_status="running",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, blocked_id, claimer="worker-a"))
            self.assertTrue(kanban_db.block_task(conn, blocked_id, reason="human review needed"))
            outcomes = module.reconcile(
                conn, config=module.Config(enabled=True, dry_run=True, max_items=1),
            )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].terminal_state, "dry_run")
        self.assertEqual(outcomes[0].metadata.get("batch_id"), "batch-done-priority")
        self.assertIn(done_id, outcomes[0].metadata.get("parent_ids", []))

    def test_internal_done_card_does_not_consume_bounded_reconcile_slot(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="parent", batch="batch-internal-slot")
            ordinary = kanban_db.create_task(
                conn, title="ordinary handoff", body="worker handoff", assignee="worker-a",
                initial_status="running", board="default",
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, ordinary, claimer="worker-a"))
            self.assertTrue(kanban_db.complete_task(
                conn, ordinary, summary="worker handoff", metadata={
                    "operator_batch": "batch-ordinary-slot",
                    "operator_expected_ids": [ordinary],
                    "operator_group_ready": True,
                },
            ))
            review = module.create_or_get_verification(
                conn, board="default", batch="batch-internal-slot", expected_ids=[parent],
            )
            self.assertIsNotNone(kanban_db.claim_task(conn, review, claimer="night-operator"))
            self.assertTrue(kanban_db.complete_task(
                conn, review, summary="verification complete", metadata={
                    "operator_batch": "batch-internal-slot",
                    "operator_expected_ids": [parent],
                },
            ))
            conn.execute("UPDATE tasks SET completed_at = 50 WHERE id = ?", (parent,))
            conn.execute("UPDATE tasks SET completed_at = 100 WHERE id = ?", (ordinary,))
            conn.execute("UPDATE tasks SET completed_at = 200 WHERE id = ?", (review,))
            self.assertGreater(
                module.get_task(conn, review).completed_at,
                module.get_task(conn, ordinary).completed_at,
            )
            legacy = kanban_db.list_tasks(
                conn, status="done", limit=1, order_by="completed-desc",
            )
            external = module._external_candidates(conn, status="done", limit=1)
            self.assertEqual([task.id for task in legacy], [review])
            self.assertEqual([task.id for task in external], [ordinary])
            outcomes = module.reconcile(
                conn, config=module.Config(enabled=True, dry_run=True, max_items=1),
            )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].terminal_state, "dry_run")
        self.assertEqual(outcomes[0].metadata.get("batch_id"), "batch-ordinary-slot")
        self.assertIn(ordinary, outcomes[0].metadata.get("parent_ids", []))

    def test_internal_task_requires_reserved_key_and_machine_marker(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            parent = self._done(conn, title="ordinary parent", batch="batch-spoof")
            ordinary_id = kanban_db.create_task(
                conn, title="Night review: user-authored title", body="ordinary worker handoff",
                assignee="worker-a", initial_status="running", board="default",
            )
            ordinary = module.get_task(conn, ordinary_id)
            fake_key_id = kanban_db.create_task(
                conn, title="ordinary title", body="ordinary worker handoff",
                assignee="worker-a", initial_status="running", board="default",
                idempotency_key="operator-review:default:spoofed",
            )
            fake_key = module.get_task(conn, fake_key_id)
            review_id = module.create_or_get_verification(
                conn, board="default", batch="batch-spoof", expected_ids=[parent],
            )
            genuine_review = module.get_task(conn, review_id)
        self.assertFalse(module._is_internal_task(ordinary))
        self.assertFalse(module._is_internal_task(fake_key))
        self.assertTrue(module._is_internal_task(genuine_review))

    def test_reconcile_never_processes_a_verification_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as conn:
            a = kanban_db.create_task(
                conn, title="parent", body="worker handoff", assignee="worker-a", initial_status="running",
            )
            b = kanban_db.create_task(
                conn, title="child", body="worker handoff", assignee="worker-a", initial_status="running",
            )
            expected = [a, b]
            for task_id in expected:
                self.assertTrue(kanban_db.claim_task(conn, task_id, claimer="worker-a"))
                self.assertTrue(kanban_db.complete_task(
                    conn, task_id, summary="worker handoff", metadata={
                        "operator_batch": "batch-race",
                        "operator_expected_ids": expected,
                        "operator_group_ready": True,
                    },
                ))
            review = module.create_or_get_verification(
                conn, board="default", batch="batch-race", expected_ids=expected,
            )
            kanban_db.claim_task(conn, review, claimer="night-operator")
            kanban_db.complete_task(
                conn, review, summary="verification done",
                metadata={"operator_batch": "batch-race", "operator_expected_ids": expected},
            )
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            outcomes = module.reconcile(
                conn, config=module.Config(enabled=True, dry_run=True, max_items=20),
            )
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].terminal_state, "dry_run")
        self.assertEqual(outcomes[0].metadata["batch_id"], "batch-race")

    def test_two_connections_do_not_duplicate_idempotency_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as setup:
            a = self._done(setup, title="parent", batch="batch-concurrent")
            b = self._done(setup, title="child", batch="batch-concurrent", expected=[a])
            expected = [a, b]
        barrier = threading.Barrier(2)
        ids: list[str | None] = [None, None]
        errors: list[BaseException] = []
        def create(index: int) -> None:
            try:
                # A sqlite connection is thread-affine. Each concurrent worker must
                # open its own connection in its own thread; sharing the setup
                # connection would test Python's sqlite guard, not idempotency.
                with kbc.connect_closing(db_path=self.db) as conn:
                    barrier.wait()
                    ids[index] = module.create_or_get_verification(
                        conn, board="default", batch="batch-concurrent", expected_ids=expected,
                    )
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create, args=(0,)), threading.Thread(target=create, args=(1,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(ids[0], ids[1])
        with kbc.connect_closing(db_path=self.db) as check:
            count = check.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (
                module.review_key("default", "batch-concurrent", expected),
            )).fetchone()[0]
        self.assertEqual(count, 1)

    def test_two_processes_create_one_verification_card(self) -> None:
        module = _plugin()
        with kbc.connect_closing(db_path=self.db) as setup:
            a = self._done(setup, title="parent", batch="batch-multiprocess")
            b = self._done(setup, title="child", batch="batch-multiprocess", expected=[a])
            expected = [a, b]
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_create_verification_in_process,
                args=(str(self.db), expected, barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        messages = [results.get(timeout=2) for _ in processes]
        self.assertTrue(all(status == "ok" for status, _ in messages), messages)
        self.assertEqual(messages[0][1], messages[1][1])
        with kbc.connect_closing(db_path=self.db) as check:
            count = check.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (
                module.review_key("default", "batch-multiprocess", expected),
            )).fetchone()[0]
        self.assertEqual(count, 1)

    def test_native_manifest_declares_settings_and_hooks(self) -> None:
        module = _plugin()
        manifest_path = Path(module.__file__).with_name("plugin.yaml")
        text = manifest_path.read_text(encoding="utf-8")
        self.assertIn("config_schema:", text)
        self.assertIn("kanban_task_completed", text)
        self.assertIn("kanban_task_blocked", text)
        self.assertIn("enabled: {type: bool, default: false", text)
        self.assertIn("dry_run: {type: bool, default: true", text)
        self.assertNotIn("night_operator:\n  enabled: true", text)

    def test_operator_markers_redact_secret_like_values(self) -> None:
        module = _plugin()
        body = module._json_marker(
            "review", {"summary": "token=sk-night-operator-secret", "safe": "batch-12"},
        )
        self.assertNotIn("sk-night-operator-secret", body)
        self.assertIn("batch-12", body)


if __name__ == "__main__":
    unittest.main()
