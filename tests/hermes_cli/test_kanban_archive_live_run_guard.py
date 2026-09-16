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


def _foreign_running_task(conn, *, pid: int = 4242, expires_in: int = 900) -> str:
    """A ``running`` card claimed by ANOTHER host; ``expires_in`` sets its deadline."""
    tid = kb.create_task(conn, title="foreign", assignee="a")
    kb.claim_task(conn, tid, claimer="otherhost:worker")
    kbd._set_worker_pid(conn, tid, pid)
    conn.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) + expires_in, tid),
    )
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


# ---------------------------------------------------------------------------
# The refusal says WHICH evidence it read and whether the pid is ours
# (window tails: a live FOREIGN process, or a reused pid, read exactly like a
# real worker — nothing in the event or in the line let an operator tell them
# apart)
# ---------------------------------------------------------------------------


def test_refusal_names_host_local_and_heartbeat_age(kanban_home, monkeypatch, capsys):
    """The refusal carries the pid, its host-locality and the heartbeat age it read."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (int(time.time()) - 30, tid),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    assert _run_cli("archive", tid) != 0

    err = capsys.readouterr().err
    assert "alive on this host" in err, "the line must say the pid is host-local"
    assert "--force" in err, "the escape stays visible"
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_refused'", (tid,),
        ).fetchone()
    refusal = json.loads(row["payload"])
    assert refusal["host_local"] is True
    assert 25 <= refusal["heartbeat_age"] <= 90, refusal["heartbeat_age"]


def test_refusal_of_foreign_claim_records_host_local_false(kanban_home, monkeypatch):
    """A live pid under a FOREIGN claim is recorded as not ours — the reader's cue
    that the pid is this host's unrelated process and ``--force`` reclaims nothing
    of the other host's worker."""
    with kbc.connect() as conn:
        tid = _running_task(conn)
        conn.execute("UPDATE tasks SET claim_lock = 'otherhost:worker' WHERE id = ?", (tid,))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    with kbc.connect() as conn:
        assert kb.archive_task(conn, tid) is False
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_refused'", (tid,),
        ).fetchone()
    refusal = json.loads(row["payload"])
    assert refusal["host_local"] is False
    assert refusal["worker_pid"] == 54321
    assert refusal["reason"] == "pid_alive"


# ---------------------------------------------------------------------------
# The guard covers the claims the dispatcher would refuse to spare
# ---------------------------------------------------------------------------


def test_foreign_claim_with_live_local_pid_is_refused(kanban_home, monkeypatch):
    """The host-local gate must not swallow the guard: a LIVE process on this host is
    evidence of work in progress whatever the claim lock says (probe B: rc=0, archived
    without asking, process alive). The refusal records ``host_local: false`` so the
    operator sees the pid is not that claim's worker."""
    with kbc.connect() as conn:
        tid = _foreign_running_task(conn, expires_in=-60)
        conn.execute("UPDATE tasks SET last_heartbeat_at = NULL WHERE id = ?", (tid,))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    assert _run_cli("archive", tid) != 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'archive_refused'", (tid,),
        ).fetchone()
    refusal = json.loads(row["payload"])
    assert refusal["host_local"] is False
    assert refusal["reason"] == "pid_alive"


def test_foreign_claim_with_dead_worker_needs_no_force(kanban_home, monkeypatch):
    """The overreach net for the cell above: a claim from another host whose worker is gone
    is the ordinary stuck-card cleanup — the guard must not start refusing it."""
    with kbc.connect() as conn:
        tid = _foreign_running_task(conn, expires_in=900)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    assert _run_cli("archive", tid) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"


def test_foreign_claim_on_a_fresh_heartbeat_needs_no_force(kanban_home, monkeypatch):
    """The host-local part of the evidence, and the parity with ``release_stale_claims`` it
    comes from: a heartbeat is only readable as liveness by the host that writes it, so a
    foreign claim with a fresh heartbeat and a dead pid is not this host's live run. The card
    is the ordinary stuck-card cleanup — and the other host's worker, which nothing here can
    signal, is left to its own reclaim path rather than being provoked by an archive that
    could not terminate it anyway."""
    with kbc.connect() as conn:
        tid = _foreign_running_task(conn, expires_in=900)
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (int(time.time()), tid),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    assert _run_cli("archive", tid) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"


def test_spared_claim_matches_release_stale_claims(kanban_home, monkeypatch):
    """Read, don't restate, the predicate the docstring claims a superset of: every
    task ``release_stale_claims`` actually spares must be one the guard protects
    (otherwise a card gets archived the tick before the dispatcher extends it)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    with kbc.connect() as conn:
        spared_expired = _running_task(conn, pid=54321)          # local, expired claim
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, spared_expired),
        )
        spared_fresh = _running_task(conn, pid=54322)            # local, fresh claim
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) + 900, spared_fresh),
        )
        spared = {spared_expired, spared_fresh}
        assert kb.release_stale_claims(conn) == 0, "precondition: both claims are spared"
        for tid in spared:
            assert kb.live_run_info(conn, tid) is not None, tid


# ---------------------------------------------------------------------------
# --force is visible in the record
# ---------------------------------------------------------------------------


def test_batch_archives_nothing_when_one_card_is_live(kanban_home, monkeypatch, capsys):
    """A batch is ONE decision, not a sequence of half-done ones: the ids are asked about
    before the first card is archived (2026-09-16: a 128-card cleanup archived the first 126
    and killed the two running ones it never asked about)."""
    with kbc.connect() as conn:
        live = _running_task(conn, pid=54321)
        finished = kb.create_task(conn, title="done", assignee="a")
        assert kb.complete_task(conn, finished, result="ok")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    assert _run_cli("archive", finished, live) != 0

    err = capsys.readouterr().err
    assert live in err, "the refusal names the live card"
    with kbc.connect() as conn:
        assert kb.get_task(conn, finished).status == "done", "the batch archived nothing"
        assert kb.get_task(conn, live).status == "running"
        kinds = _kinds(conn, live)
        assert "archive_refused" in kinds, "the batch refusal is on the card's record too"


def test_forced_batch_archives_every_id(kanban_home, monkeypatch, capsys):
    """One ``--force`` is what archives the whole batch, live card included."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(kbd, "_kill_fn", lambda signal_fn: lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect() as conn:
        live = _running_task(conn, pid=54321)
        finished = kb.create_task(conn, title="done", assignee="a")
        assert kb.complete_task(conn, finished, result="ok")

    assert _run_cli("archive", finished, live, "--force") == 0

    assert capsys.readouterr().err == "", "a forced batch needs no refusal prose"
    assert signalled and signalled[0][0] == 54321
    with kbc.connect() as conn:
        assert kb.get_task(conn, finished).status == "archived"
        assert kb.get_task(conn, live).status == "archived"


def test_forced_archive_records_the_override(kanban_home, monkeypatch):
    """The ``archived`` event says the archive ran on someone's explicit --force, so a
    terminated worker is not read as a silent one."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kbc.connect() as conn:
        tid = _running_task(conn)

    assert _run_cli("archive", tid, "--force") == 0

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived'", (tid,),
        ).fetchone()
    assert json.loads(row["payload"])["force"] is True


def test_plain_archive_records_no_override(kanban_home):
    """A card that needed no override says so — the field is evidence, not decoration."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done", assignee="a")
        assert kb.complete_task(conn, tid, result="ok")

    assert _run_cli("archive", tid) == 0

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived'", (tid,),
        ).fetchone()
    assert json.loads(row["payload"]).get("force") is False


# ---------------------------------------------------------------------------
# --rm reads the same flags as the archive it purges
# ---------------------------------------------------------------------------


def test_purge_reads_force_and_says_so(kanban_home, capsys):
    """``archive --rm <ids>`` takes ``--force`` at the same verb level; the parser used to
    swallow it into the id list, so the operator's explicit override was read as a task
    id (or as a silent no-op) instead of being honoured or refused."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done", assignee="a")
        assert kb.complete_task(conn, tid, result="ok")
    assert _run_cli("archive", tid) == 0, "precondition: the card is archived"

    assert _run_cli("archive", "--rm", tid, "--force") == 0

    captured = capsys.readouterr()
    assert f"Deleted {tid}" in captured.out
    assert "--force" in captured.err, "the flag must be named in the refusal, not swallowed"
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid) is None, "the purge removed the card"


def test_purge_of_a_live_card_names_the_force_flag(kanban_home, capsys):
    """``--force`` never turns a purge into a delete-anything: the card must already be
    archived, and the flag is reported as inert rather than accepted in silence."""
    with kbc.connect() as conn:
        tid = _running_task(conn)

    assert _run_cli("archive", "--rm", tid, "--force") == 1

    err = capsys.readouterr().err
    assert "--force" in err
    assert "must already be archived" in err
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running", "the card was not touched"
