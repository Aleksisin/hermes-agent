"""``delete_task`` must not take a card out from under a live run.

Measured 2026-10-10 on the ibf board: ``t_422bbcde`` (status ``running``, run 1118, worker
pid 19096) was hard-deleted from the DB while its worker was still working. ``sqlite_sequence``
then read ``task_runs = 1118`` with no row ``id = 1118`` and no event carrying ``run_id =
1118`` — the rows were inserted and afterwards deleted (a file rolled back to the previous
snapshot would read 1115). All five of the worker's remaining board calls refused
(``task t_422bbcde not found``, ``could not heartbeat …``, ``unknown task …``,
``could not complete …``, ``could not block …``), so a running agent was orphaned with no row
to report to and no way to close.

``delete_task`` is the single path that deletes a task row — the dashboard's
``DELETE /api/plugins/kanban/tasks/{id}`` route (its single-task and bulk "Delete" buttons)
calls it directly — and it had no live-run guard at all, while its sibling
``delete_archived_task`` requires an explicit archive first. Refusal is the fix: a live card
is deleted only after ``hermes kanban reclaim <id>`` (or once its worker is provably gone).

Invariants asserted here (seam: the DB layer, so every caller shares them):

* a live card is refused, and the refusal is recorded ON the card (``delete_refused``), so the
  orphaned-worker incident leaves a trace instead of a silent gap;
* "provably gone" is what licenses the delete: a gone pid still deletes in one step, while an
  UNVERIFIED fingerprint refuses — that liveness can never be proven;
* a card whose claim was archived with its worker still alive is refused too: deleting its
  rows would throw away the pid/fingerprint evidence ``reap_terminal_workers`` needs to end
  that worker (issue #111791);
* a refusal writes nothing else — runs, comments and events of that card are intact and other
  cards are untouched.
"""

from __future__ import annotations

import argparse
import json
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


def _running_task(conn, *, pid: int = 54321, fingerprint=None) -> str:
    """A claimed (``running``) card carrying ``worker_pid`` — a live run.

    ``fingerprint=None`` leaves ``worker_started_at`` NULL: the legacy-row shape, whose
    liveness is the bare pid probe (the shape the 2026-10-10 incident left behind).
    """
    tid = kb.create_task(conn, title="live", assignee="a")
    assert kb.claim_task(conn, tid, claimer=f"{kb._host_prefix()}worker")
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
        (pid, fingerprint, tid),
    )
    conn.commit()
    return tid


def _kinds(conn, tid: str) -> list[str]:
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
    ).fetchall()]


def _refusals(conn, tid: str) -> list[dict]:
    return [
        json.loads(r["payload"] or "{}")
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'delete_refused'",
            (tid,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# A live run is protected
# ---------------------------------------------------------------------------


def test_delete_task_refuses_card_with_live_run(kanban_home, monkeypatch):
    """A live host-local worker's card is refused; the refusal is on the card's record."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
        other = kb.create_task(conn, title="bystander", assignee="a")
        kb.add_comment(conn, tid, "user", "work in progress")
        runs_before = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    with kbc.connect() as conn:
        assert kb.delete_task(conn, tid) is False

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None, "a live card must survive the delete attempt"
        assert task.status == "running"
        assert "deleted" not in _kinds(conn, tid)
        refusals = _refusals(conn, tid)
        assert refusals, "the refusal itself must be recorded on the card"
        assert refusals[-1]["reason"] == "live_worker"
        assert int(refusals[-1]["worker_pid"]) == 54321
        # Nothing else was written: the card's history is exactly as it was.
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == runs_before
        assert len(kb.list_comments(conn, tid)) == 1
        # And the bystander card is untouched, refusal event included.
        assert kb.get_task(conn, other) is not None
        assert _refusals(conn, other) == []


def test_delete_task_refuses_worker_with_unverifiable_liveness(kanban_home, monkeypatch):
    """An UNVERIFIED spawn fingerprint refuses even with a live pid: whether that process is
    our worker can never be proven, and the delete cannot be undone."""
    with kbc.connect() as conn:
        tid = _running_task(conn, fingerprint=kbd.UNVERIFIED_WORKER_FINGERPRINT)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    with kbc.connect() as conn:
        assert kb.delete_task(conn, tid) is False
        assert kb.get_task(conn, tid) is not None
        assert _refusals(conn, tid)[-1]["reason"] == "live_worker"


def test_delete_task_refuses_archived_claim_with_live_worker(kanban_home, monkeypatch, capsys):
    """Archived + live = the worker outlived its archive: the closed run still carries the pid
    and the spawn fingerprint ``reap_terminal_workers`` needs to signal it, so those rows stay.

    Built the way production builds it: claim the card, record the worker pid through
    ``_set_worker_pid`` (which stamps pid + fingerprint on the task AND the run), then archive.
    ``archive_task`` clears the pid from the task row but keeps it on the run — the last
    evidence of that process once the card leaves ``running``.

    Also drives the operator surface (``hermes kanban archive --rm <id>``), because that CLI
    purge is a real caller of the fence: it must fail with a line naming the pid rather than
    removing the run.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="live", assignee="a")
        assert kb.claim_task(conn, tid, claimer=f"{kb._host_prefix()}worker")
        kbd._set_worker_pid(conn, tid, 54321)
        assert kb.archive_task(conn, tid)
        row = conn.execute(
            "SELECT status, worker_pid FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "archived" and row["worker_pid"] is None
        run = conn.execute(
            "SELECT worker_pid, worker_started_at, claim_lock FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert run["worker_pid"] == 54321
        assert (run["claim_lock"] or "").startswith(kb._host_prefix())
    monkeypatch.setattr(kb, "_worker_alive", lambda pid, started_at: True)

    with kbc.connect() as conn:
        assert kb.delete_task(conn, tid) is False
        assert kb.get_task(conn, tid) is not None
        assert _refusals(conn, tid)[-1]["reason"] == "archived_live_worker"

    assert _run_cli("archive", "--rm", tid) != 0
    err = capsys.readouterr().err
    assert tid in err and "54321" in err, f"the refusal must name the card and the pid: {err!r}"
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid) is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# "Provably gone" still deletes in one step (the pre-fix behaviour elsewhere)
# ---------------------------------------------------------------------------


def test_delete_task_allows_card_whose_worker_is_gone(kanban_home, monkeypatch):
    """Green control: a card whose worker died is deleted in one step, no archive needed."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
        kb.add_comment(conn, tid, "user", "comment")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    with kbc.connect() as conn:
        assert kb.delete_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_task_allows_clean_card(kanban_home):
    """Green control: an unclaimed card keeps the plain one-step delete (no refusal row)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="never-run", assignee="a")
        assert kb.delete_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
