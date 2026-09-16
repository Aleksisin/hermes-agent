"""A card running under a live claim may only be closed by its owner.

Observed class (IBF board, 2026-09-16): a ``delegate_task`` child closed card
``t_d1e09157`` while worker run 386 was still working. The child was refused
once by name (the cooperative ``HERMES_DELEGATED_CHILD_CONTEXT`` marker), then
dropped that one env var and the CLI closed the card with rc 0 — issue #69e6a49e.
The ``completed`` event carried the *owner's* run id, so the record could not
tell an owner's close from a stranger's, and the card read ``done`` 11.5 minutes
before the owner actually finished (the owner's own commit landed at 18:07).

The invariants pinned here are relations, not snapshots:

* a close that carries no proof of ownership (``expected_run_id``) is refused
  while the card's claim is live — and the card keeps the holder's state;
* the same close passes with an explicit operator override (``force``), and
  *never* silently: it is recorded as a non-owner completion (own event, run
  metadata and the ``completed`` payload), next to the unmerged-branch verdict;
* owner paths — the run pin, the scoped CLI/tool env, a lapsed claim, a card
  with no claim at all — stay untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

NON_OWNER_EVENT = "completion_non_owner"
NON_OWNER_KEY = "non_owner_completion"
FOREIGN_LOCK = "testhost:4242"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with its own board DB (never the live board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                 "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _claimed_card(conn, title: str = "claimed card", *, ttl_seconds: int = 900) -> tuple[str, int]:
    """A card in ``running`` under a live claim held by somebody else."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    claim = kb.claim_task(conn, tid, ttl_seconds=ttl_seconds, claimer=FOREIGN_LOCK)
    assert claim is not None and claim.current_run_id is not None
    return tid, int(claim.current_run_id)


def _row(conn, tid: str) -> dict:
    return dict(conn.execute(
        "SELECT status, result, claim_lock, current_run_id FROM tasks WHERE id = ?", (tid,),
    ).fetchone())


def _run_metadata(conn, tid: str) -> dict:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    return json.loads(row["metadata"]) if row and row["metadata"] else {}


def _events(conn, tid: str, kind: str) -> list[dict]:
    out = []
    for event in kb.list_events(conn, tid):
        if event.kind != kind:
            continue
        payload = event.payload
        out.append(json.loads(payload) if isinstance(payload, str) else payload)
    return out


def _completed_payload(conn, tid: str) -> dict:
    payloads = _events(conn, tid, "completed")
    assert payloads, "the card must record a completed event"
    return payloads[-1]


# ---------------------------------------------------------------------------
# A — the ownership boundary: an unowned close of a live claim is refused
# ---------------------------------------------------------------------------


def test_unowned_close_of_a_live_claim_is_refused(board: Path) -> None:
    """The observed bypass: no run proof, no claim, no override -> nothing happens."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)
        assert kb.complete_task(conn, tid, result="child verdict") is False
        after = _row(conn, tid)
        assert after["status"] == "running", "a refused close must not touch the card"
        assert after["result"] is None
        assert after["claim_lock"] == FOREIGN_LOCK, "the holder keeps its claim"
        assert after["current_run_id"] == owner_run
        assert _events(conn, tid, "completed") == []


def test_the_refusal_is_named_and_names_the_holder(board: Path) -> None:
    """A bare ``cannot complete`` hides an ownership clash (cf. the CLI's exit line)."""
    with kbc.connect_closing() as conn:
        tid, _ = _claimed_card(conn)
        reason = kb.close_refusal_reason(conn, tid)
        assert reason, "a live claim must produce a refusal reason"
        assert FOREIGN_LOCK in reason
        assert "force" in reason


def test_the_owner_closing_through_its_run_proof_is_untouched(board: Path) -> None:
    """The owner path every worker already uses: the run pin, no override."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)
        assert kb.complete_task(conn, tid, summary="owner report", expected_run_id=owner_run) is True
        assert _row(conn, tid)["status"] == "done"
        assert _events(conn, tid, NON_OWNER_EVENT) == []


def test_a_card_with_no_claim_is_closed_as_before(board: Path) -> None:
    """Manual CLI closes of a ready/blocked card carry no live claim to invade."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="unclaimed", assignee="worker")
        assert kb.complete_task(conn, tid, result="closed by hand") is True
        assert _row(conn, tid)["status"] == "done"
        assert _events(conn, tid, NON_OWNER_EVENT) == []
        assert NON_OWNER_KEY not in _run_metadata(conn, tid)


def test_a_lapsed_claim_does_not_block_a_close(board: Path) -> None:
    """An expired lease is the reclaim lane's business, not an ownership wall."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn, ttl_seconds=1)
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (0, tid))
        conn.commit()
        assert kb.complete_task(conn, tid, result="closed after the lease lapsed") is True
        assert _row(conn, tid)["status"] == "done"
        assert _events(conn, tid, NON_OWNER_EVENT) == []


def test_a_blank_close_does_not_reach_another_run(board: Path) -> None:
    """Ownership is a fact about the holder, not about what the caller wrote."""
    with kbc.connect_closing() as conn:
        tid, _ = _claimed_card(conn)
        assert kb.complete_task(conn, tid, summary="forged handoff",
                               metadata={"close_without_merge": "unrelated declaration"}) is False
        assert _row(conn, tid)["status"] == "running"


# ---------------------------------------------------------------------------
# B — the distinguishable record: an explicit override closes, and says so
# ---------------------------------------------------------------------------


def test_an_explicit_override_closes_and_records_the_non_owner_close(board: Path) -> None:
    """``force`` is the operator's band (the sibling of request_review's) — and it stamps."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)
        assert kb.complete_task(
            conn, tid, result="operator verdict", metadata={"operator": "shogu"}, force=True,
        ) is True
        assert _row(conn, tid)["status"] == "done"

        events = _events(conn, tid, NON_OWNER_EVENT)
        assert events, "a close without run proof must be distinguishable on the board"
        record = events[-1]
        assert record["claim_lock"] == FOREIGN_LOCK
        assert record["run_id"] == owner_run
        assert record["closed_by"] == kb._claimer_id()

        run_meta = _run_metadata(conn, tid)
        assert run_meta[NON_OWNER_KEY]["claim_lock"] == FOREIGN_LOCK
        assert run_meta[NON_OWNER_KEY]["run_id"] == owner_run
        # the closing party's own metadata is enriched, never clobbered
        assert run_meta["operator"] == "shogu"
        assert NON_OWNER_KEY in _completed_payload(conn, tid)


def test_the_record_names_the_override_it_was_closed_under(board: Path) -> None:
    """The record must separate "the holder closed it" from "somebody overrode it"."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)
        assert kb.complete_task(conn, tid, summary="override", force=True) is True
        record = _events(conn, tid, NON_OWNER_EVENT)[-1]
        assert record["holder"] == FOREIGN_LOCK
        assert record["closed_by"] == kb._claimer_id()
        assert record["caller_pid"] != ""
        assert str(owner_run) in record["warning"] or record["run_id"] == owner_run


# ---------------------------------------------------------------------------
# The live path — the worker's own tool, through the real handler
# ---------------------------------------------------------------------------


def test_the_worker_tool_completes_its_own_claimed_card(board: Path, monkeypatch) -> None:
    """The tool path pins the worker's run: owner green, no non-owner record."""
    from hermes_cli import kanban_db as kb_mod

    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(owner_run))
    kb_mod._INITIALIZED_PATHS.clear()

    from tools import kanban_tools as kt

    assert json.loads(kt._handle_complete({"summary": "owner finished"}))["ok"] is True
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["status"] == "done"
        assert _events(conn, tid, NON_OWNER_EVENT) == []


# ---------------------------------------------------------------------------
# The observed bypass, end to end through the real CLI
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def _hermes_cli(board: Path, *args: str, env_extra: dict | None = None):
    """Run ``hermes kanban …`` in its own process against the temp board."""
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["HERMES_HOME"] = str(board)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for name in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
                 "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        env.pop(name, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, check=False, timeout=120,
    )


def test_the_cli_names_the_live_claim_instead_of_closing(board: Path) -> None:
    """The child's own line, stepped one further than the marker guard reached:
    the cooperative marker is gone, so the refusal has to come from ownership."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)

    refused = _hermes_cli(board, "complete", tid, "--result", "child verdict")
    assert refused.returncode == 1, refused.stdout
    assert FOREIGN_LOCK in refused.stderr, refused.stderr
    assert "--force" in refused.stderr, refused.stderr

    with kbc.connect_closing() as conn:
        after = _row(conn, tid)
        assert after["status"] == "running"
        assert after["claim_lock"] == FOREIGN_LOCK
        assert after["current_run_id"] == owner_run

    # The operator's band: the same command with the explicit override.
    forced = _hermes_cli(board, "complete", tid, "--result", "operator verdict", "--force")
    assert forced.returncode == 0, forced.stderr
    assert f"Completed {tid}" in forced.stdout
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["status"] == "done"
        closed = _events(conn, tid, NON_OWNER_EVENT)
        assert closed and closed[-1]["holder"] == FOREIGN_LOCK
        # The closing party ran in its own process, so its ``host:pid`` is that
        # process's, never this test process's — assert the lock's shape, not a pid.
        assert closed[-1]["closed_by"].split(":", 1)[1].isdigit()


def test_the_scoped_worker_cli_still_closes_its_own_card(board: Path) -> None:
    """The other owner path: the dispatcher's env pins the worker's run — no ``--force``,
    no non-owner record (the scoped CLI is untouched by the gate)."""
    with kbc.connect_closing() as conn:
        tid, owner_run = _claimed_card(conn)

    done = _hermes_cli(
        board, "complete", tid, "--result", "owner report",
        env_extra={"HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(owner_run)},
    )
    assert done.returncode == 0, done.stderr
    with kbc.connect_closing() as conn:
        assert _row(conn, tid)["status"] == "done"
        assert _events(conn, tid, NON_OWNER_EVENT) == []
