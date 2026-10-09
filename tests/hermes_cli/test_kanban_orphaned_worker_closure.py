"""An orphaned worker must be told to stop, not nudged forever.

Measured 2026-10-10 on the ibf board: ``t_422bbcde`` (``running``, run 1118, worker pid 19096)
was hard-deleted while its worker was still working it. Every remaining board call of that
worker refused (``task t_422bbcde not found``, ``could not heartbeat …``, ``could not complete
…``, ``could not block …``) — and the worker-side paths cannot recover either:
``agent/kanban_turn_recovery.worker_claim_is_live`` reads ``task_runs`` through
``tasks.current_run_id``, rows that the delete removed.

So a worker whose card row is GONE has no terminal move available at all. The stop-nudge
(``agent/kanban_stop.py``) must not keep asking it to call ``kanban_complete`` — that is an
infinite "please close" loop against a board that no longer knows the card, which is exactly
how the incident looked from the worker's side. And the refusal it gets from the board tools
must name the situation instead of a generic "not found".

Invariants asserted here:

* nudge still fires while the card exists (no behaviour change for healthy workers);
* nudge is suppressed when the board answers authoritatively that the card is gone;
* an unreadable DB never suppresses the nudge (fail open — a broken probe must not let a
  healthy worker exit without a handoff);
* the worker's own tool call on a gone card rejects with an instruction to stop and end the
  turn, rather than a bare "not found".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _worker_env(monkeypatch, tid: str, run_id: int) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)


def test_nudge_still_fires_while_the_card_exists(kanban_home, monkeypatch):
    """Green control: a healthy worker keeps the plain-text-is-not-terminal nudge."""
    from agent import kanban_stop

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="alive", assignee="w")
        assert kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    _worker_env(monkeypatch, tid, run_id)

    assert kanban_stop.kanban_card_gone() is False
    assert kanban_stop.build_kanban_stop_nudge(messages=[]) is not None


def test_nudge_suppressed_when_the_card_was_deleted(kanban_home, monkeypatch):
    """The orphaned worker: the card row is gone, so no board call can close it — stop nudging."""
    from agent import kanban_stop

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="doomed", assignee="w")
        assert kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    _worker_env(monkeypatch, tid, run_id)
    with kbc.connect() as conn:
        assert kb.delete_task(conn, tid) is True

    assert kanban_stop.kanban_card_gone() is True
    assert kanban_stop.build_kanban_stop_nudge(messages=[]) is None


def test_unreadable_db_never_suppresses_the_nudge(kanban_home, monkeypatch):
    """Fail open: a probe that cannot answer must not license a silent exit."""
    from agent import kanban_stop

    _worker_env(monkeypatch, "t_deadbeef", 7)

    def boom(*_a, **_k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(kanban_stop, "_unused_probe_hook", boom, raising=False)
    # Patch the connection factory the probe actually uses.
    from hermes_cli import kanban_db_connect as _kbc

    monkeypatch.setattr(_kbc, "connect", boom)
    assert kanban_stop.kanban_card_gone() is False
    assert kanban_stop.build_kanban_stop_nudge(messages=[]) is not None


def test_worker_tool_call_names_the_orphaned_card(kanban_home, monkeypatch):
    """The board tool tells the worker to stop instead of a bare 'not found'."""
    from tools import kanban_tools as kt

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="doomed", assignee="w")
        assert kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    _worker_env(monkeypatch, tid, run_id)
    with kbc.connect() as conn:
        assert kb.delete_task(conn, tid) is True

    out = kt._handle_complete({"summary": "done"})
    assert "stop" in out.lower() or "end the turn" in out.lower(), out
    assert "no longer" in out or "gone" in out or "removed" in out, out
