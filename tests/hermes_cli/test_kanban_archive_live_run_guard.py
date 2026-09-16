"""``hermes kanban archive`` must not silently kill a live worker.

Regression for the 2026-09-16 ibf board cleanup: one batch archived 128 cards and
swept up two RUNNING ones (``t_f99e6223`` ibf-coder, ``t_d7cc669a`` ibf-reviewer).
Both got an ``archived`` event *and* an ``archive_worker_termination``
(``{"prev_pid": 19900, "host_local": true, "terminated": true, ...}``), their runs
were closed as ``reclaimed``, and the CLI asked nothing and returned 0 — an archive
of a live card was indistinguishable from an archive of a finished one. The second
half of the hole was the audit trail: every ``archived`` event carried
``payload = NULL`` and ``task_events`` has no actor column, so afterwards nobody
could tell who had taken the board down.

Invariants asserted here (seam: the CLI command, i.e. ``kanban_command`` with a real
argparse tree — the same boundary an operator and a batch script use):

* a live run refuses the archive without ``--force``, naming the card and the pid;
* ``--force`` keeps the pre-fix behaviour (terminate + ``archive_worker_termination``);
* a finished card and a card whose worker is gone archive with no flag at all;
* the ``archived`` event records the initiator (profile, session, calling surface).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _run_cli(*argv: str) -> int:
    """Drive ``hermes kanban …`` through the real argparse tree."""
    return kc.kanban_command(_parser().parse_args(["kanban", *argv]))


def _running_task(conn, *, pid: int = 54321) -> str:
    """A claimed (``running``) task with a recorded worker pid — a live run."""
    tid = kb.create_task(conn, title="live", assignee="a")
    kb.claim_task(conn, tid, claimer=f"{kb._host_prefix()}worker")
    kbd._set_worker_pid(conn, tid, pid)
    return tid


def _kinds(conn, tid: str) -> list[str]:
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# The live run is protected
# ---------------------------------------------------------------------------


def test_archive_refuses_card_with_live_run(kanban_home, monkeypatch, capsys):
    """No ``--force`` -> refusal naming the card and the pid; the card is untouched."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    rc = _run_cli("archive", tid)

    err = capsys.readouterr().err
    assert rc != 0, "archiving a card with a live run must not report success"
    assert tid in err, "the refusal must name the card"
    assert "54321" in err, "the refusal must name the worker pid"
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        kinds = _kinds(conn, tid)
        assert "archived" not in kinds
        assert "archive_worker_termination" not in kinds
        assert "archive_refused" in kinds, "the refusal itself must be on the card's record"


def test_archive_refuses_on_fresh_heartbeat(kanban_home, monkeypatch):
    """The dispatcher spares a claim whose heartbeat is fresh even when the pid probe
    cannot see the process; the archive must not be more optimistic than that."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (int(time.time()), tid),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    assert _run_cli("archive", tid) != 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        assert "archived" not in _kinds(conn, tid)


def test_db_archive_refuses_live_run_itself(kanban_home, monkeypatch):
    """The guard is the DB layer's, not a CLI decoration: the panel's bulk cleanup and any
    other profile reach ``archive_task`` directly, so every path refuses alike."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    with kbc.connect() as conn:
        assert kb.archive_task(conn, tid) is False
        assert kb.get_task(conn, tid).status == "running"
        assert "archived" not in _kinds(conn, tid)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_refused'", (tid,),
        ).fetchone()
    refusal = json.loads(row["payload"])
    assert refusal["worker_pid"] == 54321
    assert refusal["reason"] == "pid_alive"
    assert refusal["source"]


# ---------------------------------------------------------------------------
# --force keeps the old behaviour
# ---------------------------------------------------------------------------


def test_archive_force_terminates_live_run(kanban_home, monkeypatch):
    """``--force`` archives anyway and still terminates the host-local worker."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(kbd, "_kill_fn", lambda signal_fn: lambda pid, sig: signalled.append((pid, sig)))
    with kbc.connect() as conn:
        tid = _running_task(conn)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    assert _run_cli("archive", tid, "--force") == 0

    assert signalled and signalled[0][0] == 54321
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_worker_termination'", (tid,),
        ).fetchone()
        assert row is not None
        assert json.loads(row["payload"])["prev_pid"] == 54321


# ---------------------------------------------------------------------------
# Nothing else starts asking for a flag
# ---------------------------------------------------------------------------


def test_archive_completed_card_needs_no_force(kanban_home):
    """A finished card archives exactly as before — the guard is about live runs only."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done", assignee="a")
        assert kb.complete_task(conn, tid, result="ok")

    assert _run_cli("archive", tid) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"


def test_archive_dead_worker_run_needs_no_force(kanban_home, monkeypatch):
    """A ``running`` card whose worker is gone and whose heartbeat is not fresh is the
    ordinary stuck-card cleanup: no flag, no refusal."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
        conn.execute("UPDATE tasks SET last_heartbeat_at = NULL WHERE id = ?", (tid,))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    assert _run_cli("archive", tid) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"


# ---------------------------------------------------------------------------
# Traceability: who archived the card
# ---------------------------------------------------------------------------


def test_archived_event_records_initiator(kanban_home, monkeypatch):
    """The ``archived`` event carries profile + session + calling surface, so a batch
    that takes live work down can be attributed afterwards."""
    monkeypatch.setenv("HERMES_PROFILE_NAME", "ibf-operator")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-20260916")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done", assignee="a")
        assert kb.complete_task(conn, tid, result="ok")

    assert _run_cli("archive", tid) == 0

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived'", (tid,),
        ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["actor"] == "ibf-operator"
    assert payload["session_id"] == "sess-20260916"
    assert payload["source"] == "cli"
