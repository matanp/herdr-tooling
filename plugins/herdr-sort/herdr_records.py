"""Session records: one live recap per open agent session, read by every herdr tool.

`refresh` is the one producer. The daemon calls it as agents settle, cron and
`herdr-sort records` call it as a sweep, and cull calls it with the keys it wants
audited. Readers (`load`, `view`, `closed_view`) never call a model.

A record has two tiers. The recap tier (recap, needs, next_step) follows every
settled turn on a cheap model; the audit tier is cull's close note, run only when
asked for or after long idleness. When a tab closes, ledger.close copies the record
into the close event, so the ledger keeps it once the tab is gone.
"""

import concurrent.futures
import fcntl
import glob
import json
import os
import tempfile

import herdr_ledger as ledger

RECORDS_PATH = os.path.join(ledger.STATE_DIR, "records.json")
INFLIGHT_DIR = os.path.join(ledger.STATE_DIR, "records-inflight")

AGENTS = ("claude", "codex")
SETTLED = ("idle", "done")
SETTLE_S = 60
MIN_GROWTH = 6000
AUDIT_IDLE_S = 6 * 3600
FAIL_RETRY_S = 900
PRUNE_S = 14 * 86400

RECAP_FIELDS = ("recap", "needs", "next_step", "model", "built_for", "built_at")
AUDIT_FIELDS = ("audit", "audit_for", "audit_at")
ERROR_FIELDS = ("error", "failed_for", "failed_at")
CLOSE_FIELDS = ("recap", "needs", "next_step", "model", "built_at", "audit")


# --- store ------------------------------------------------------------------

def load():
    try:
        with open(RECORDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _built(record):
    return record.get("built_at") or 0


def _audited(record):
    return record.get("audit_at") or 0


def _touched(record):
    # A success clears the error, so the error fields follow whichever copy ran last.
    return max(_built(record), _audited(record), record.get("failed_at") or 0)


def save(records, now=None):
    """Merge under a lock, newest tier wins: the daemon, the cron sweep and cull all
    write this file, each holding a copy read before its model calls."""
    now = now or 0
    os.makedirs(ledger.STATE_DIR, exist_ok=True)
    with open(RECORDS_PATH + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = load()
        for key, mine in records.items():
            theirs = merged.get(key) or {}
            new = dict(theirs)
            new.update({k: v for k, v in mine.items()
                        if k not in RECAP_FIELDS + AUDIT_FIELDS + ERROR_FIELDS})
            for fields, stamp in ((RECAP_FIELDS, _built), (AUDIT_FIELDS, _audited),
                                  (ERROR_FIELDS, _touched)):
                if stamp(mine) >= stamp(theirs):
                    for field in fields:
                        new.pop(field, None)
                        if field in mine:
                            new[field] = mine[field]
            if "frozen_at" not in mine:
                new.pop("frozen_at", None)
            merged[key] = new
        if now:
            merged = {k: r for k, r in merged.items()
                      if not r.get("frozen_at") or now - r["frozen_at"] < PRUNE_S}
        fd, tmp = tempfile.mkstemp(prefix="records.", suffix=".tmp", dir=ledger.STATE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, separators=(",", ":"))
        os.replace(tmp, RECORDS_PATH)
    return merged


# --- in-flight guard --------------------------------------------------------

def _claim(key):
    """An open, flocked handle, or None when another refresh holds this session. The
    lock is per open file, so it guards threads of one process as well as processes."""
    os.makedirs(INFLIGHT_DIR, exist_ok=True)
    handle = open(os.path.join(INFLIGHT_DIR, key.replace(":", "_").replace("/", "_")), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# --- refresh ----------------------------------------------------------------

def _plan(row, rec, current, audit, force, now):
    """(recap?, audit?) for one open pane, from the transcript's size and age."""
    try:
        stat = os.stat(rec["file"])
    except (OSError, KeyError, TypeError):
        return False, False, None
    size, quiet = stat.st_size, now - stat.st_mtime
    asked = rec["key"] in audit
    if row.get("status") not in SETTLED and not (
            asked and row.get("status") not in ("working", "blocked")):
        return False, False, size
    if quiet < SETTLE_S and not (asked or force):
        return False, False, size
    if current.get("failed_for") == size and now - (current.get("failed_at") or 0) < FAIL_RETRY_S:
        return False, False, size
    built = current.get("built_for")
    grown = size != built and (not current.get("recap") or force or asked
                               or size - (built or 0) >= MIN_GROWTH)
    want_audit = (asked or quiet >= AUDIT_IDLE_S) and current.get("audit_for") != size
    return grown, want_audit, size


def _job(llm, rec, current, do_recap, do_audit):
    """Model calls for one session; each tier fails on its own, keeping what it had."""
    out, errors = {}, []
    if do_recap:
        try:
            out["recap"] = llm.recap(rec, previous=current.get("recap"))
        except Exception as err:  # noqa: BLE001 -- one bad call never blanks a record
            errors.append(f"recap: {type(err).__name__}: {err}")
    if do_audit:
        recap = (out.get("recap") or {}).get("recap") or current.get("recap")
        try:
            out["audit"] = llm.close_note(dict(rec, recap=recap or rec.get("recap")))
        except Exception as err:  # noqa: BLE001
            errors.append(f"audit: {type(err).__name__}: {err}")
    return out, errors


def refresh(snapshot, index, records, llm, now, audit=(), force=False, workers=4,
            progress=None):
    """Return the record store brought up to date with the open sessions.

    snapshot is ledger.live_panes() rows for every open pane; index is the ledger's
    session index. Only Claude and Codex panes whose transcript the index knows are
    eligible. A session is refreshed once its agent is idle and its transcript has
    been quiet for SETTLE_S, and only when it grew by MIN_GROWTH since its recap.
    audit names keys to audit now (and refresh, skipping the settle wait); force skips
    the settle wait and the growth threshold for everything. A record whose tab is
    gone is frozen, not refreshed.
    """
    audit = set(audit or ())
    out = {k: dict(v) for k, v in (records or {}).items()}
    live = {}
    for row in snapshot:
        rec = index.get(row.get("key") or "")
        if row.get("agent") not in AGENTS or not rec or ledger._own_run(rec.get("file")):
            continue
        live[rec["key"]] = (row, rec)
    for key, record in out.items():
        if key in live:
            record.pop("frozen_at", None)
        elif not record.get("frozen_at"):
            record["frozen_at"] = now
    todo = []
    for key, (row, rec) in live.items():
        current = out.setdefault(key, {"key": key})
        current.update(agent=rec.get("agent"), title=rec.get("title") or row.get("title"))
        if not rec.get("prompts"):
            continue
        do_recap, do_audit, size = _plan(row, rec, current, audit, force, now)
        if do_recap or do_audit:
            todo.append((key, rec, size, do_recap, do_audit))
    claimed = []
    try:
        for key, rec, size, do_recap, do_audit in todo:
            handle = _claim(key)
            if handle:
                claimed.append((handle, key, rec, size, do_recap, do_audit))
        if not claimed:
            return out
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            jobs = {pool.submit(_job, llm, rec, out[key], do_recap, do_audit): (key, size)
                    for _, key, rec, size, do_recap, do_audit in claimed}
            for n, job in enumerate(concurrent.futures.as_completed(jobs), 1):
                key, size = jobs[job]
                made, errors = job.result()
                current = out[key]
                if made.get("recap"):
                    current.update(made["recap"], built_for=size, built_at=now)
                if made.get("audit"):
                    current.update(audit=made["audit"], audit_for=size, audit_at=now)
                if errors:
                    current.update(error="; ".join(errors), failed_for=size, failed_at=now)
                elif made:
                    for field in ERROR_FIELDS:
                        current.pop(field, None)
                if progress:
                    progress(f"records {n}/{len(jobs)}")
    finally:
        for handle, *_ in claimed:
            handle.close()
    return out


# --- readers ----------------------------------------------------------------

def fallback(rec):
    """First ask plus Claude Code's newest recap, for a session with no record yet."""
    parts = [p for p in ((rec or {}).get("first_ask"), (rec or {}).get("recap")) if p]
    return " · ".join(parts) or None


def view(key, records, rec=None):
    """{"text", "needs", "next_step", "at", "source"} for a session, or None.

    source is "record" when a recap was built, else "fallback" with no "at"."""
    record = (records or {}).get(key or "") or {}
    if record.get("recap"):
        return {"text": record["recap"], "needs": record.get("needs"),
                "next_step": record.get("next_step"), "at": record.get("built_at"),
                "model": record.get("model"), "audit": record.get("audit"),
                "audit_at": record.get("audit_at"), "source": "record"}
    text = fallback(rec)
    if not text:
        return None
    return {"text": text, "needs": None, "next_step": (rec or {}).get("next_step"),
            "at": None, "model": None, "audit": None, "audit_at": None, "source": "fallback"}


def close_fields(key, records):
    """What ledger.close copies into the close event, so `log` shows the last recap."""
    record = (records or {}).get(key or "") or {}
    if not record.get("recap"):
        return None
    return {f: record[f] for f in CLOSE_FIELDS if record.get(f) is not None}


def closed_view(event, rec):
    """(text, source) for a closed session: the record frozen into its close event,
    else the ledger's own summary from before records existed."""
    frozen = (event or {}).get("record") or {}
    if frozen.get("recap"):
        return frozen["recap"], "record"
    return ledger.summary_of(rec) if rec else ("", "")


# --- sweep ------------------------------------------------------------------

def _stale_files(rows, index):
    """Transcripts of settled open panes that the index has not parsed at their size,
    including sessions it has never seen."""
    files = set()
    for row in rows:
        key = row.get("key")
        if row.get("agent") not in AGENTS or not key or row.get("status") in ("working", "blocked"):
            continue
        rec = index.get(key)
        path = rec.get("file") if rec else locate(key)
        if not path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not rec or rec.get("size") != stat.st_size or rec.get("mtime") != stat.st_mtime:
            files.add(path)
    return files


def locate(key):
    agent, _, sid = (key or "").partition(":")
    if not sid:
        return None
    if "claude" == agent:
        hits = glob.glob(os.path.join(ledger.CLAUDE_PROJECTS, "*", f"{sid}.jsonl"))
    else:
        hits = [p for pattern in ledger.CODEX_GLOBS
                for p in glob.glob(pattern.replace("rollout-*", f"rollout-*{sid}"))]
    return hits[0] if hits else None


def sweep(llm, now, audit=(), force=False, rows=None, progress=None):
    """One pass over every open pane, from the live snapshot: sync what moved, refresh,
    save. Returns (rows, index, records)."""
    rows = ledger.live_panes() if rows is None else rows
    index = ledger.load_index()
    files = _stale_files(rows, index)
    if files:
        index, _ = ledger.sync(index, only=files)
    records = refresh(rows, index, load(), llm, now, audit=audit, force=force,
                      progress=progress)
    return rows, index, save(records, now=now)
