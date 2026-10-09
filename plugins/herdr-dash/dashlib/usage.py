"""Agent spend for today, this week (Monday start) and this month, from ccusage.

All three are summed from ccusage's daily rows rather than its weekly/monthly
reports, so the week boundary is ours and not ccusage's convention.

A ccusage run takes from one to several seconds, and collect() runs on every
refresh tick alongside sections that take milliseconds. So collect() only reads
a cache file; a stale cache kicks off a detached `usage.py refresh`, which
outlives the popup that spawned it and is single-flighted by a lock file across
every open dash.
"""

import datetime
import json
import os
import subprocess
import sys
import time

CCUSAGE = "~/.local/share/pnpm/ccusage"
STATE = "~/.local/state/herdr-dash"
TTL_S = 120
LOCK_STALE_S = 90


def _path(name):
    return os.path.join(os.path.expanduser(STATE), name)


def _windows(today):
    monday = today - datetime.timedelta(days=today.weekday())
    return {"today": today, "week": monday, "month": today.replace(day=1)}


def fetch(today=None):
    today = today or datetime.date.today()
    windows = _windows(today)
    since = min(windows.values())
    command = [os.path.expanduser(CCUSAGE), "daily", "--json",
               "--since", since.strftime("%Y%m%d")]
    # Live pricing first: the embedded table lags new models, and a model it
    # lacks is costed at $0 with only a stderr warning, so offline is the fallback.
    proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if proc.returncode:
        proc = subprocess.run(command + ["--offline"], capture_output=True,
                              text=True, timeout=60)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1])
    rows = json.loads(proc.stdout).get("daily") or []
    warning = next((line.strip() for line in proc.stderr.splitlines()
                    if "pricing" in line.lower()), None)
    totals = {}
    for name, start in windows.items():
        picked = [row for row in rows
                  if start.isoformat() <= row["period"] <= today.isoformat()]
        totals[name] = {
            "cost": sum(row.get("totalCost") or 0 for row in picked),
            "tokens": sum(row.get("totalTokens") or 0 for row in picked),
            "days": len(picked),
        }
    models = next((row.get("modelsUsed") or [] for row in rows
                   if row["period"] == today.isoformat()), [])
    return {"date": today.isoformat(), "fetched_at": time.time(),
            "totals": totals, "models_today": models, "warning": warning}


def refresh():
    data = None
    try:
        data = fetch()
    except Exception as err:
        data = {"error": f"{type(err).__name__}: {err}",
                "fetched_at": time.time()}
    previous = _read()
    if data.get("error") and previous and not previous.get("error"):
        # Keep the last good figures; the renderer shows their age and the error.
        previous["error"] = data["error"]
        data = previous
    os.makedirs(os.path.expanduser(STATE), exist_ok=True)
    tmp = _path("usage.json.tmp")
    with open(tmp, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, _path("usage.json"))


def _read():
    try:
        with open(_path("usage.json")) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _spawn_refresh():
    lock = _path("usage.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    try:
        if time.time() - os.path.getmtime(lock) < LOCK_STALE_S:
            return
        os.unlink(lock)
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "refresh"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def collect():
    data = _read()
    fresh = (data and data.get("date") == datetime.date.today().isoformat()
             and time.time() - data.get("fetched_at", 0) < TTL_S)
    if not fresh:
        _spawn_refresh()
    return data or {"pending": True}


if "__main__" == __name__ and sys.argv[1:] == ["refresh"]:
    try:
        refresh()
    finally:
        try:
            os.unlink(_path("usage.lock"))
        except FileNotFoundError:
            pass
