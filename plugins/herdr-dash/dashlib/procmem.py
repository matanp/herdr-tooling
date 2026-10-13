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

Without /proc (macOS) there is no PSS, no cgroup and no environ to read, so the
sweep falls back to one `ps` call: RSS for every process, and the pane join by
walking each pane's process tree down from the shell pid that
pane.process_info reports. Processes that outlived their pane are not found
that way, so the orphaned band stays empty there.
"""

import os
import re
import subprocess
import time

from . import herdr as herdr_mod

PROC = "/proc"
CGROUP = "/sys/fs/cgroup"
KIB = 1024


def meminfo():
    if not os.path.isdir(PROC):
        return _meminfo_darwin()
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


def _run(*argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout


_SWAP = re.compile(r"(total|free) = ([\d.]+)M")


def _meminfo_darwin():
    """The /proc/meminfo keys the renderer reads, from sysctl and vm_stat.

    Available is free + inactive + speculative + purgeable pages, which is what
    the kernel can hand out without paging -- close to Activity Monitor's figure.
    """
    out = {}
    try:
        out["MemTotal"] = int(_run("sysctl", "-n", "hw.memsize"))
        stat = _run("vm_stat")
        page = int(re.search(r"page size of (\d+)", stat).group(1))
        pages = {key.strip(): int(value.strip(" .")) for key, value in
                 (line.split(":", 1) for line in stat.splitlines()[1:] if ":" in line)
                 if value.strip(" .").isdigit()}
        out["MemAvailable"] = page * sum(pages.get(f"Pages {name}", 0) for name in
                                         ("free", "inactive", "speculative", "purgeable"))
        swap = dict(_SWAP.findall(_run("sysctl", "-n", "vm.swapusage")))
        out["SwapTotal"] = int(float(swap.get("total", 0)) * KIB * KIB)
        out["SwapFree"] = int(float(swap.get("free", 0)) * KIB * KIB)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return out
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


def _pane_shells():
    """pane_id -> shell pid, for every live pane."""
    client, out = herdr_mod.Herdr(), {}
    for pane in herdr_mod.snapshot().get("panes", []):
        try:
            info = client.call("pane.process_info", {"pane_id": pane["pane_id"]})
        except herdr_mod.HerdrError:
            continue
        shell = (info.get("process_info") or {}).get("shell_pid")
        if shell:
            out[shell] = pane["pane_id"]
    return out


def _clip(text, limit=60):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def sweep_ps(uid=None):
    """The sweep's result shape from one `ps` call, for systems without /proc."""
    started = time.monotonic()
    uid = os.getuid() if uid is None else uid
    procs, children = {}, {}
    for line in _run("ps", "-A", "-o", "pid=,ppid=,uid=,rss=,command=").splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5 or not fields[0].isdigit():
            continue
        pid, ppid, owner, rss = (int(field) for field in fields[:4])
        procs[pid] = {"pid": pid, "uid": owner, "rss": rss * KIB, "cmd": fields[4]}
        children.setdefault(ppid, []).append(pid)
    try:
        shells = _pane_shells()
    except herdr_mod.HerdrError:
        shells = {}
    pane_of = {}
    for shell, pane_id in shells.items():
        stack = [shell]
        while stack:
            pid = stack.pop()
            if pid in procs and pid not in pane_of:
                pane_of[pid] = pane_id
                stack.extend(children.get(pid, ()))
    own, foreign, panes = [], [], {}
    for pid, proc in procs.items():
        if proc["uid"] != uid:
            if proc["rss"]:
                foreign.append({"pid": pid, "rss": proc["rss"], "uid": proc["uid"],
                                "comm": os.path.basename(proc["cmd"].split(" -", 1)[0])})
            continue
        record = {"pid": pid, "bytes": proc["rss"], "source": "rss",
                  "cmd": _clip(proc["cmd"]), "pane": pane_of.get(pid)}
        own.append(record)
        if record["pane"]:
            bucket = panes.setdefault(record["pane"], {"bytes": 0, "procs": []})
            bucket["bytes"] += record["bytes"]
            bucket["procs"].append(record)
    return _summary(own, foreign, panes, started, metric="RSS")


def _summary(own, foreign, panes, started, metric="PSS"):
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
        # RSS is the only measure without /proc, so nothing fell back to it.
        "degraded": 0 if "RSS" == metric else
                    sum(1 for record in own if record["source"] == "rss"),
        "metric": metric,
        "elapsed_s": time.monotonic() - started,
    }


def sweep(uid=None):
    """One pass over /proc. Returns own-PSS totals, the pane join, and timing."""
    if not os.path.isdir(PROC):
        return sweep_ps(uid)
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
    return _summary(own, foreign, panes, started)


def collect():
    return {"meminfo": meminfo(), "cgroup": slices(), "procs": sweep(),
            "disks": disks(), "at": time.time()}
