"""Machine memory, attributed to the pane that is spending it.

Three bands, and the plugin labels each one, because they are not the same
measurement:

  cgroup   memory.current for the two slices and for named services. The only
           honest figure for processes this user does not own.
  PSS      smaps_rollup, own processes only -- shared pages divided among the
           sharers, so a sum over siblings is meaningful.
  RSS      fallback for a process whose smaps_rollup is EPERM. Never summed
           across processes: the 27 postgres backends share most of their
           pages, and adding their RSS counts that memory 27 times.

The pane join is the point: herdr puts HERDR_PANE_ID in every pane's
environment, so /proc/<pid>/environ attributes a process to a pane.
"""

import os
import time

PROC = "/proc"
CGROUP = "/sys/fs/cgroup"
KIB = 1024


def meminfo():
    out = {}
    try:
        with open(f"{PROC}/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                fields = rest.split()
                if fields:
                    out[key] = int(fields[0]) * KIB
    except OSError:
        return {}
    return out


def _read_int(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def slices():
    """cgroup v2 memory.current for the top-level slices, plus the top services."""
    out = {"slices": {}, "services": []}
    for name in ("user.slice", "system.slice", "init.scope"):
        value = _read_int(f"{CGROUP}/{name}/memory.current")
        if value is not None:
            out["slices"][name] = value
    services = []
    root = f"{CGROUP}/system.slice"
    try:
        entries = os.listdir(root)
    except OSError:
        entries = []
    for entry in entries:
        current = _read_int(f"{root}/{entry}/memory.current")
        if current is None:
            # A nested slice -- postgres lives in system-postgresql.slice, which
            # has no memory.current of its own on this box; sum its children.
            child_root = f"{root}/{entry}"
            total = 0
            try:
                for child in os.listdir(child_root):
                    value = _read_int(f"{child_root}/{child}/memory.current")
                    if value:
                        total += value
            except OSError:
                total = 0
            current = total
        if current:
            services.append((entry, current))
    services.sort(key=lambda item: -item[1])
    out["services"] = services
    return out


def _pss(pid):
    try:
        with open(f"{PROC}/{pid}/smaps_rollup", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * KIB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _rss(pid):
    try:
        with open(f"{PROC}/{pid}/statm", encoding="utf-8") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _environ_pane(pid):
    try:
        with open(f"{PROC}/{pid}/environ", "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    for item in blob.split(b"\0"):
        if item.startswith(b"HERDR_PANE_ID="):
            return item[14:].decode("utf-8", "replace")
    return None


def _comm(pid):
    try:
        with open(f"{PROC}/{pid}/comm", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return "?"


def _cmdline(pid, limit=60):
    try:
        with open(f"{PROC}/{pid}/cmdline", "rb") as handle:
            parts = [p.decode("utf-8", "replace")
                     for p in handle.read().split(b"\0") if p]
    except OSError:
        parts = []
    if not parts:
        return _comm(pid)
    text = " ".join(parts)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def disks(paths=("/", "/tmp", os.path.expanduser("~"))):
    """One row per distinct filesystem behind the paths that fill up here."""
    seen, out = set(), []
    for path in paths:
        try:
            stat = os.statvfs(path)
        except OSError:
            continue
        key = (stat.f_blocks, stat.f_frsize)
        if not stat.f_blocks or key in seen:
            continue
        seen.add(key)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        out.append({"path": path, "total": total, "free": free,
                    "used_fraction": 1 - (free / total)})
    return out


def sweep(uid=None):
    """One pass over /proc. Returns own-PSS totals, the pane join, and timing."""
    started = time.monotonic()
    uid = os.getuid() if uid is None else uid
    own, foreign, panes = [], [], {}
    for entry in os.listdir(PROC):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            owner = os.stat(f"{PROC}/{entry}").st_uid
        except OSError:
            continue
        if owner != uid:
            rss = _rss(pid)
            if rss:
                foreign.append({"pid": pid, "rss": rss, "comm": _comm(pid),
                                "uid": owner})
            continue
        size = _pss(pid)
        source = "pss"
        if size is None:
            size = _rss(pid) or 0
            source = "rss"
        record = {"pid": pid, "bytes": size, "source": source,
                  "cmd": _cmdline(pid), "pane": _environ_pane(pid)}
        own.append(record)
        if record["pane"]:
            bucket = panes.setdefault(record["pane"], {"bytes": 0, "procs": []})
            bucket["bytes"] += size
            bucket["procs"].append(record)
    own.sort(key=lambda record: -record["bytes"])
    foreign.sort(key=lambda record: -record["rss"])
    for bucket in panes.values():
        bucket["procs"].sort(key=lambda record: -record["bytes"])
    return {
        "own": own,
        "foreign": foreign,
        "panes": panes,
        "own_total": sum(record["bytes"] for record in own),
        "pane_total": sum(bucket["bytes"] for bucket in panes.values()),
        "own_count": len(own),
        "foreign_count": len(foreign),
        "degraded": sum(1 for record in own if record["source"] == "rss"),
        "elapsed_s": time.monotonic() - started,
    }


def collect():
    return {"meminfo": meminfo(), "cgroup": slices(), "procs": sweep(),
            "disks": disks(), "at": time.time()}
