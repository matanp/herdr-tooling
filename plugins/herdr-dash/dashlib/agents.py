"""Agent rows, from herdr-sessions.

That tool owns the columns -- in particular HELD, whose `>=Nm` idle bound comes
from a seq stamp it persists across runs, because herdr's API carries no
wall-clock timestamp for an agent. Re-deriving any of this
here would produce a second, disagreeing answer.

terminal_id is the one thing it does not carry, and notes key on it, so the
join comes from this plugin's own snapshot.
"""

import json
import subprocess

STATUS_ORDER = {"blocked": 0, "working": 1, "idle": 2, "done": 3, "unknown": 4}
GLYPH = {"blocked": "◆", "working": "●", "idle": "○",
         "done": "✓", "unknown": "?"}


def rows(terminals=None, timeout=30):
    proc = subprocess.run(["herdr-sessions", "--json", "--no-recap"], capture_output=True,
                          text=True, timeout=timeout)
    if 0 != proc.returncode:
        raise RuntimeError(
            f"herdr-sessions --json failed: {proc.stderr.strip() or proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError("herdr-sessions --json returned unreadable JSON")
    for row in data:
        row["terminal_id"] = (terminals or {}).get(row.get("pane"))
    return data


def by_workspace(data):
    groups = {}
    for row in data:
        groups.setdefault(row.get("workspace") or "—", []).append(row)
    for group in groups.values():
        group.sort(key=lambda row: (STATUS_ORDER.get(row.get("status"), 9),
                                    row.get("pane") or ""))
    # Workspaces holding a blocked agent first: that is the one wanting a human.
    return sorted(groups.items(),
                  key=lambda item: (min(STATUS_ORDER.get(row.get("status"), 9)
                                        for row in item[1]), item[0]))


def tally(data):
    counts = {}
    for row in data:
        status = row.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts
