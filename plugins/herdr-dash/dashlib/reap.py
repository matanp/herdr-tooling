"""Kill the processes of panes herdr no longer has.

The rendered orphan list is up to one refresh interval old, so nothing here
trusts it. Each kill re-takes the herdr snapshot and re-reads every pid's
owner and HERDR_PANE_ID first: a pane can come back, and a pid can be reused
by an unrelated process in between.
"""

import os
import signal
import time

from . import herdr as herdr_mod
from . import procmem


def _stat(pid):
    """(ppid, starttime), or None once the pid is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _ancestors():
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        stat = _stat(pid)
        if not stat:
            break
        pid = stat[0]
    return out


def kill(pane_ids, grace=2.0):
    """SIGTERM, then SIGKILL whatever outlives the grace period.

    Raises HerdrError when the snapshot fails: without it every live pane
    looks orphaned, so the kill must fail closed.
    """
    panes, _ = herdr_mod.pane_index(herdr_mod.snapshot())
    targets = set(pane_ids) - set(panes)
    if not targets:
        return {"killed": 0, "forced": 0, "skipped": sorted(pane_ids)}
    spared = _ancestors()
    started = {}
    for record in procmem.sweep()["own"]:
        stat = _stat(record["pid"])
        if record["pane"] in targets and record["pid"] not in spared and stat:
            started[record["pid"]] = stat[1]
    sent = []
    for pid in started:
        try:
            os.kill(pid, signal.SIGTERM)
            sent.append(pid)
        except OSError:
            pass

    # A pid whose start time changed was reused during the grace period.
    def alive(pid):
        stat = _stat(pid)
        return bool(stat) and stat[1] == started[pid]

    deadline = time.monotonic() + grace
    survivors = sent
    while survivors and time.monotonic() < deadline:
        time.sleep(0.1)
        survivors = [pid for pid in survivors if alive(pid)]
    forced = 0
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
            forced += 1
        except OSError:
            pass
    return {"killed": len(sent), "forced": forced,
            "skipped": sorted(set(pane_ids) - targets)}
