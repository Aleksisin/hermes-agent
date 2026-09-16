"""Live probe: does ``hermes kanban archive`` ask before archiving a card whose run is live?

Seeds an isolated board (temp HERMES_HOME + temp DB — the parent session exports
HERMES_KANBAN_DB pointing at the LIVE ibf board, so both are overridden here) with cards that
have real live processes as their ``worker_pid``, then runs the real CLI against them.

Three probes:
  [1] single card claimed by another host, live local pid, expired claim (red-team probe B);
  [2] the same card after the process dies — the ordinary stuck-card cleanup;
  [3] a batch of a finished card + a live card — a batch is ONE decision.

``HERMES_PROBE_REPO`` points the probe at another checkout (the unpatched baseline).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="archive-probe-")) / ".hermes"
HOME.mkdir(parents=True)
os.environ["HERMES_HOME"] = str(HOME)
PROBE_DB = HOME / "probe-kanban.db"
os.environ["HERMES_KANBAN_DB"] = str(PROBE_DB)
os.environ.pop("HERMES_KANBAN_BOARD", None)
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

REPO = Path(os.environ.get("HERMES_PROBE_REPO", r"C:\Users\shogu\AppData\Local\hermes\hermes-agent"))
sys.path.insert(0, str(REPO))
HERMES = str(Path(os.environ.get(
    "HERMES_PROBE_EXE", r"C:\Users\shogu\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe")))

from hermes_cli import kanban_db as kb            # noqa: E402
from hermes_cli import kanban_db_connect as kbc   # noqa: E402
from hermes_cli import kanban_db_dispatch as kbd  # noqa: E402

print(f"probe sources: {REPO}\nprobe db:      {PROBE_DB}")
kb.init_db()


def run_cli(*argv: str) -> tuple[int, str]:
    """Drive the real `hermes kanban` from the tree under probe."""
    proc = subprocess.run(
        [HERMES, "kanban", *argv], capture_output=True, text=True, cwd=str(REPO),
        env={
            **os.environ,
            "HERMES_HOME": str(HOME),
            "HERMES_KANBAN_DB": str(PROBE_DB),
            "PYTHONPATH": str(REPO),
            "PYTHONNOUSERSITE": "1",
        },
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def events(tid: str) -> list[tuple[str, str]]:
    with kbc.connect() as conn:
        return [
            (r["kind"], r["payload"] or "")
            for r in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
            ).fetchall()
        ]


def show(tid: str) -> None:
    print("    events:", *[f"\n      {k}: {p}" for k, p in events(tid)], sep="")


sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(f"live process on this host: pid {sleeper.pid}\n")
try:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="probe: foreign claim, live local pid", assignee="ibf-coder")
        kb.claim_task(conn, tid, claimer="otherhost:worker")            # NOT this host
        kbd._set_worker_pid(conn, tid, sleeper.pid)                     # a real live process
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, tid),                               # claim already expired
        )
        print(f"card {tid}: status=running claim_lock=otherhost:worker claim_expires=expired")
        print(f"archive guard says: {kb.live_run_info(conn, tid)}")

    rc, out = run_cli("archive", tid)
    print(f"\n[1] hermes kanban archive {tid}  (process ALIVE) -> rc={rc}")
    print(f"    {out}")
    with kbc.connect() as conn:
        print(f"    status now: {kb.get_task(conn, tid).status}")
    show(tid)

    sleeper.terminate()
    sleeper.wait(timeout=10)
    time.sleep(0.5)

    rc, out = run_cli("archive", tid)
    print(f"\n[2] hermes kanban archive {tid}  (process dead) -> rc={rc}")
    print(f"    {out}")
    with kbc.connect() as conn:
        print(f"    status now: {kb.get_task(conn, tid).status}")
    show(tid)

    batch_sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        with kbc.connect() as conn:
            live = kb.create_task(conn, title="probe: batch, live card", assignee="ibf-coder")
            kb.claim_task(conn, live, claimer=f"{kb._host_prefix()}worker")
            kbd._set_worker_pid(conn, live, batch_sleeper.pid)
            finished = kb.create_task(conn, title="probe: batch, finished card", assignee="ibf-coder")
            kb.complete_task(conn, finished, result="ok")

        rc, out = run_cli("archive", finished, live)
        print(f"\n[3] hermes kanban archive {finished} {live}  (one card live) -> rc={rc}")
        print(f"    {out}")
        with kbc.connect() as conn:
            print(f"    live card:     {kb.get_task(conn, live).status} (kept running)")
            print(f"    finished card: {kb.get_task(conn, finished).status} (untouched)")
    finally:
        batch_sleeper.kill()
finally:
    if sleeper.poll() is None:
        sleeper.kill()
