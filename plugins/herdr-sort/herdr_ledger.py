"""Session ledger for herdr-sort: what every agent session was, and how to get it back.

Closing a tab is only safe if the session can be found again, so every Claude and
Codex session on the box gets one row: id, resume command, title, summary, span,
engaged time, agent time, prompt count, files written, PRs named, and the recap's
next step. Rows are rebuilt from the agents' own transcripts, so the ledger is a
cache -- deleting sessions.json loses nothing but the Haiku summaries.
"""

import concurrent.futures
import datetime
import fcntl
import glob
import json
import os
import re
import socket
import subprocess
import time

STATE_DIR = os.path.expanduser("~/.local/state/herdr-sort")
INDEX_PATH = os.path.join(STATE_DIR, "sessions.json")
CLOSES_PATH = os.path.join(STATE_DIR, "closes.jsonl")
SCROLLBACK_DIR = os.path.join(STATE_DIR, "scrollback")

CLAUDE_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")
CODEX_INDEX = os.path.expanduser("~/.codex/session_index.jsonl")
CODEX_GLOBS = [os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl"),
               os.path.expanduser("~/.codex/archived_sessions/rollout-*.jsonl")]

SUMMARY_MODEL = "claude-haiku-4-5-20251001"
# Summaries run from here so their own (unpersisted) sessions never land in a
# project directory the ledger scans.
SUMMARY_CWD = os.path.join(STATE_DIR, "summarize")

ENGAGED_GAP_S = 1800
ENGAGED_CAP_S = 300
KEEP_ACTIVE_S = 24 * 3600
PR_RE = re.compile(r"(?:\bPR\s*#?|/pull/|#)(\d{5})\b")
RECAP_TRAILER = re.compile(r"\s*\(disable recaps in /config\)\s*$")
NEXT_RE = re.compile(r"\b(Next(?: action| step)?\b.*)$", re.S)
IGNORED_PREFIXES = tuple(os.path.expanduser(p) for p in
                         ("~/.claude/", "~/scratch", "/tmp/", "~/.local/state/"))


def _ts(text):
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _clip(text, limit):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_human_prompt(text):
    stripped = (text or "").lstrip()
    return bool(stripped) and not stripped.startswith(
        ("<", "[Request interrupted", "Caveat:", "# AGENTS.md", "[SYSTEM"))


def _engaged(times):
    times = sorted(t for t in times if t)
    return sum(min(b - a, ENGAGED_CAP_S) for a, b in zip(times, times[1:])
               if b - a < ENGAGED_GAP_S)


def _next_step(recap):
    match = NEXT_RE.search(recap or "")
    return _clip(match.group(1), 220) if match else None


def _record(agent, sid, path, **fields):
    stat = os.stat(path)
    rec = {"key": f"{agent}:{sid}", "agent": agent, "id": sid, "file": path,
           "mtime": stat.st_mtime, "size": stat.st_size}
    rec.update(fields)
    times = [t for t in (rec.get("start"), rec.get("last")) if t]
    rec["span_s"] = (max(times) - min(times)) if times else 0
    rec["next_step"] = _next_step(rec.get("recap"))
    rec["resume"] = (f"claude --resume {sid}" if "claude" == agent
                     else f"codex resume {sid}")
    return rec


def parse_claude(path):
    sid = os.path.basename(path)[:-len(".jsonl")]
    title = recap = cwd = None
    times, prompts, files, prs = [], [], [], []
    agent_ms = tools = 0
    last_reply = ""
    with open(path, errors="replace") as handle:
        for line in handle:
            if '"type":"attachment"' in line or '"file-history-snapshot"' in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("type")
            if "ai-title" == kind:
                title = row.get("aiTitle") or row.get("title") or title
                continue
            if "system" == kind:
                if "turn_duration" == row.get("subtype"):
                    agent_ms += row.get("durationMs") or 0
                elif "away_summary" == row.get("subtype"):
                    recap = RECAP_TRAILER.sub("", row.get("content") or "").strip() or recap
                continue
            if kind not in ("user", "assistant") or row.get("isSidechain"):
                continue
            cwd = row.get("cwd") or cwd
            content = (row.get("message") or {}).get("content")
            if "user" == kind:
                if row.get("isMeta"):
                    continue
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or []
                    if isinstance(b, dict) and "text" == b.get("type"))
                if _is_human_prompt(text):
                    prompts.append(text)
                    times.append(_ts(row.get("timestamp")))
                    prs.extend(PR_RE.findall(text))
                continue
            times.append(_ts(row.get("timestamp")))
            for block in content if isinstance(content, list) else []:
                if "tool_use" == block.get("type"):
                    tools += 1
                    args = block.get("input") or {}
                    if block.get("name") in ("Edit", "Write", "NotebookEdit"):
                        target = args.get("file_path") or args.get("notebook_path")
                        if target and target not in files:
                            files.append(target)
                    elif "Bash" == block.get("name"):
                        prs.extend(PR_RE.findall(args.get("command") or ""))
                elif "text" == block.get("type") and block.get("text", "").strip():
                    last_reply = block["text"]
                    prs.extend(PR_RE.findall(last_reply))
    stamps = [t for t in times if t]
    return _record(
        "claude", sid, path, title=title, cwd=cwd,
        start=min(stamps) if stamps else None, last=max(stamps) if stamps else None,
        engaged_s=_engaged(stamps), agent_s=agent_ms / 1000, prompts=len(prompts),
        tool_calls=tools, files=files[:60], prs=list(dict.fromkeys(prs))[-8:],
        first_ask=_clip(prompts[0] if prompts else "", 400),
        asks=[_clip(p, 200) for p in prompts[1:6]],
        last_reply=_clip(last_reply, 600), recap=recap)


def _codex_names():
    names = {}
    if os.path.exists(CODEX_INDEX):
        for line in open(CODEX_INDEX, errors="replace"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            names[row["id"]] = row
    return names


def parse_codex(path, names):
    sid = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
                    path).group(1)
    cwd = None
    times, prompts, files, prs = [], [], [], []
    started, agent_s, tools = {}, 0.0, 0
    last_reply = ""
    with open(path, errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload") or {}
            kind, sub = row.get("type"), payload.get("type")
            stamp = _ts(row.get("timestamp"))
            if "session_meta" == kind:
                cwd = payload.get("cwd") or cwd
            elif "event_msg" == kind and "task_started" == sub:
                started[payload.get("turn_id")] = stamp
            elif "event_msg" == kind and "task_complete" == sub:
                begin = started.pop(payload.get("turn_id"), None)
                if begin and stamp:
                    agent_s += stamp - begin
                last_reply = payload.get("last_agent_message") or last_reply
            elif "response_item" == kind and "message" == sub:
                text = " ".join(b.get("text", "") for b in payload.get("content") or []
                                if isinstance(b, dict))
                if "user" == payload.get("role") and _is_human_prompt(text):
                    prompts.append(text)
                    times.append(stamp)
                    prs.extend(PR_RE.findall(text))
                elif "assistant" == payload.get("role") and text.strip():
                    times.append(stamp)
                    last_reply = text
            elif "response_item" == kind and sub in ("custom_tool_call", "function_call"):
                tools += 1
                body = payload.get("input") or payload.get("arguments") or ""
                # The patch can arrive JSON-escaped, so a literal backslash ends the path.
                for target in re.findall(r"\*\*\* (?:Update|Add) File: ([^\n\\]+)", body):
                    target = target.strip()
                    if not target.startswith("/") and cwd:
                        target = os.path.join(cwd, target)
                    if target not in files:
                        files.append(target)
                prs.extend(PR_RE.findall(body))
    stamps = [t for t in times if t]
    meta = names.get(sid) or {}
    return _record(
        "codex", sid, path, title=meta.get("thread_name"), cwd=cwd,
        start=min(stamps) if stamps else None,
        last=max(stamps + [_ts(meta.get("updated_at")) or 0]) if stamps else _ts(meta.get("updated_at")),
        engaged_s=_engaged(stamps), agent_s=agent_s, prompts=len(prompts), tool_calls=tools,
        files=files[:60], prs=list(dict.fromkeys(prs))[-8:],
        first_ask=_clip(prompts[0] if prompts else "", 400),
        asks=[_clip(p, 200) for p in prompts[1:6]],
        last_reply=_clip(last_reply, 600), recap=None)


# --- index ------------------------------------------------------------------

def load_index():
    try:
        with open(INDEX_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


SUMMARY_FIELDS = ("summary", "summary_source", "summary_for")


def save_index(index):
    """Merge under a lock: the dash, `stale`, and a long `--summarize` run all write
    this file, and a plain overwrite from a stale copy would drop summaries."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(INDEX_PATH + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for key, disk in load_index().items():
            mine = index.get(key)
            if mine is None:
                index[key] = disk
            elif disk.get("summary") and not mine.get("summary"):
                for field in SUMMARY_FIELDS:
                    if field in disk:
                        mine[field] = disk[field]
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(index, handle, separators=(",", ":"))
        os.replace(tmp, INDEX_PATH)


def _sources():
    for path in glob.glob(CLAUDE_GLOB):
        if os.path.dirname(path) != SUMMARY_CWD:
            yield "claude", path
    for pattern in CODEX_GLOBS:
        for path in glob.glob(pattern):
            yield "codex", path


def sync(index=None, only=None, progress=None):
    """Re-parse transcripts whose size or mtime moved; keep summaries across re-parses."""
    index = load_index() if index is None else index
    names = _codex_names()
    by_file = {r.get("file"): r for r in index.values()}
    todo = []
    for agent, path in _sources():
        if only and path not in only:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        old = by_file.get(path)
        if old and old.get("mtime") == stat.st_mtime and old.get("size") == stat.st_size:
            continue
        todo.append((agent, path))
    for n, (agent, path) in enumerate(todo, 1):
        try:
            rec = parse_claude(path) if "claude" == agent else parse_codex(path, names)
        except (OSError, AttributeError) as err:
            if progress:
                progress(f"skip {path}: {err}")
            continue
        old = index.get(rec["key"]) or by_file.get(path) or {}
        for keep in ("summary", "summary_source", "summary_for"):
            if keep in old:
                rec[keep] = old[keep]
        index[rec["key"]] = rec
        if progress and n % 100 == 0:
            progress(f"parsed {n}/{len(todo)}")
    save_index(index)
    return index, len(todo)


def _summary_prompt(rec):
    parts = [f"Session title: {rec.get('title') or '(none)'}",
             f"First request: {rec.get('first_ask') or '(none)'}"]
    if rec.get("asks"):
        parts.append("Later requests:\n" + "\n".join(f"- {a}" for a in rec["asks"]))
    parts.append(f"Final assistant reply: {rec.get('last_reply') or '(none)'}")
    return ("Summarize this coding-agent session for a log the user skims to decide "
            "whether to reopen it. One or two plain sentences, at most 45 words: what it "
            "was about, then where it ended -- what got done and what was left open. "
            "No preamble, no markdown.\n\n" + "\n\n".join(parts))


def _haiku(prompt):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    os.makedirs(SUMMARY_CWD, exist_ok=True)
    out = subprocess.run(
        ["claude", "-p", "--model", SUMMARY_MODEL, "--no-session-persistence",
         "--tools", "", "--strict-mcp-config", "--setting-sources", ""],
        input=prompt, capture_output=True, text=True, timeout=120, cwd=SUMMARY_CWD, env=env)
    text = out.stdout.strip()
    if out.returncode or not text:
        raise RuntimeError(_clip(out.stderr or f"exit {out.returncode}", 160))
    return _clip(text, 400)


def needs_summary(rec):
    """Recaps are Claude Code's own summary; Haiku fills in only where none was written,
    and re-runs when the session has grown since its summary."""
    if rec.get("recap") or not (rec.get("first_ask") or rec.get("last_reply")):
        return False
    return rec.get("summary_for") != rec.get("size")


def summarize(index, keys=None, workers=6, limit=None, progress=None):
    todo = [r for r in index.values() if (keys is None or r["key"] in keys) and needs_summary(r)]
    todo.sort(key=lambda r: -(r.get("last") or 0))
    if limit:
        todo = todo[:limit]
    done = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_haiku, _summary_prompt(r)): r for r in todo}
        for job in concurrent.futures.as_completed(jobs):
            rec = jobs[job]
            try:
                rec["summary"] = job.result()
                rec["summary_source"] = "haiku"
                rec["summary_for"] = rec.get("size")
                done += 1
            except (RuntimeError, subprocess.TimeoutExpired) as err:
                failed += 1
                if progress:
                    progress(f"summary failed for {rec['key']}: {err}")
            if (done + failed) % 25 == 0:
                save_index(index)
                if progress:
                    progress(f"summarized {done + failed}/{len(todo)}")
    save_index(index)
    return done, failed


def summary_of(rec):
    if rec.get("recap"):
        return rec["recap"], "recap"
    if rec.get("summary"):
        return rec["summary"], "haiku"
    return rec.get("first_ask") or rec.get("last_reply") or "", "first ask"


# --- live panes -------------------------------------------------------------

def _socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock")


def call(method, params=None):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(15)
    conn.connect(_socket_path())
    try:
        conn.sendall(json.dumps({"id": "herdr-ledger", "method": method,
                                 "params": params or {}}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    response = json.loads(buf.split(b"\n", 1)[0])
    if "error" in response:
        err = response["error"]
        raise RuntimeError(f"{method}: {err.get('code')}: {err.get('message')}")
    return response.get("result", {})


AGENT_SUFFIX = re.compile(r"\s*\|\s*[\w.-]+$")
GLYPHS = re.compile(r"^[\s←-⇿─-➿⬀-⯿■-◿️✳]+")


def _pane_title(pane):
    return AGENT_SUFFIX.sub("", GLYPHS.sub("", pane.get("terminal_title_stripped") or "")).strip()


def live_panes(snap=None):
    """Every pane with its workspace, tab label, and ledger key where one resolves.

    Codex panes carry no session id; the title is joined to session_index's
    thread_name, newest first. Two threads with one name resolve to the newer,
    which is why the ledger stores the uuid rather than re-joining later.
    """
    snap = snap or call("session.snapshot")["snapshot"]
    spaces = {w["workspace_id"]: w for w in snap["workspaces"]}
    tabs = {t["tab_id"]: t for t in snap["tabs"]}
    per_ws = {}
    for pane in snap["panes"]:
        per_ws[pane["workspace_id"]] = per_ws.get(pane["workspace_id"], 0) + 1
    by_name = {}
    for sid, row in sorted(_codex_names().items(), key=lambda kv: kv[1].get("updated_at") or ""):
        by_name[(row.get("thread_name") or "").strip()] = sid
    rows = []
    for pane in snap["panes"]:
        key = None
        session = pane.get("agent_session") or {}
        if "claude" == pane.get("agent") and session.get("value"):
            key = f"claude:{session['value']}"
        elif "codex" == pane.get("agent"):
            sid = by_name.get(_pane_title(pane))
            key = f"codex:{sid}" if sid else None
        ws = spaces.get(pane["workspace_id"], {})
        rows.append({"pane_id": pane["pane_id"], "tab_id": pane["tab_id"],
                     "terminal_id": pane["terminal_id"], "agent": pane.get("agent"),
                     "status": pane.get("agent_status"), "title": _pane_title(pane),
                     "cwd": pane.get("cwd"), "workspace_id": pane["workspace_id"],
                     "workspace": ws.get("label", ""), "workspace_number": ws.get("number", 0),
                     "tab_label": tabs.get(pane["tab_id"], {}).get("label", ""),
                     "focused": pane["pane_id"] == snap.get("focused_pane_id"),
                     "workspace_panes": per_ws.get(pane["workspace_id"], 0),
                     "key": key})
    return rows


_TOPLEVEL = {}


def _repo(path):
    folder = os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    if folder not in _TOPLEVEL:
        out = subprocess.run(["git", "-C", folder, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        _TOPLEVEL[folder] = out.stdout.strip() if 0 == out.returncode else None
    return _TOPLEVEL[folder]


def dirty_files(rec):
    """Files this session wrote that git still reports changed: (modified code, new code, docs).

    Every session shares the main checkout, so a dirty file means "touched here and
    dirty now", not "dirty because of this session" -- the verdict leans on it
    conservatively for code and only lists docs, which survive a closed tab anyway.
    """
    code, fresh, docs = [], [], []
    by_repo = {}
    for path in rec.get("files") or []:
        if path.startswith(IGNORED_PREFIXES) or not os.path.exists(path):
            continue
        repo = _repo(path)
        if repo:
            by_repo.setdefault(repo, []).append(os.path.relpath(path, repo))
    for repo, rels in by_repo.items():
        out = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--"] + rels,
                             capture_output=True, text=True)
        for line in out.stdout.splitlines():
            state, rel = line[:2], line[3:].strip()
            path = os.path.join(repo, rel)
            if rel.startswith(("docs/", "tmp/")) or rel.endswith(".md"):
                docs.append(path)
            else:
                (fresh if "??" == state else code).append(path)
    return code, fresh, docs


SHELLS = {"bash", "zsh", "sh", "fish", "dash"}


def _shell_busy(pane_id):
    try:
        info = call("pane.process_info", {"pane_id": pane_id}).get("process_info") or {}
    except (RuntimeError, OSError):
        return "unreadable process info"
    names = [p.get("name") for p in info.get("foreground_processes") or []]
    busy = [n for n in names if n and n not in SHELLS]
    return busy[0] if busy else None


def verdict(row, rec, now=None):
    """KEEP / GLANCE / CLOSE with the one reason that decided it.

    Almost every recap ends on a next step that is the user's, so an open question is
    not a reason to keep a tab: the ledger preserves it. What a closed tab would really
    lose is a running process, a turn in flight, or uncommitted code.
    """
    now = now or time.time()
    if row["focused"]:
        return "KEEP", "the tab you're in"
    if row["status"] in ("working", "blocked"):
        return "KEEP", f"agent {row['status']}"
    if not row["agent"]:
        busy = _shell_busy(row["pane_id"])
        if busy:
            return "KEEP", f"running {busy}"
        return "CLOSE", "idle shell; scrollback saved on close"
    if rec is None:
        return "GLANCE", ("no codex session matched this title; can't resume"
                          if "codex" == row["agent"] else "no transcript found")
    code, fresh, _ = dirty_files(rec)
    if code:
        return "KEEP", f"{len(code)} modified code file(s) uncommitted: {os.path.basename(code[0])}"
    idle = now - (rec.get("last") or now)
    if idle < KEEP_ACTIVE_S:
        return "GLANCE", f"active {human(idle)} ago"
    if fresh:
        return "GLANCE", f"{len(fresh)} new file(s) never committed: {os.path.basename(fresh[0])}"
    return "CLOSE", f"idle {human(idle)}"


def human(seconds):
    seconds = max(0, seconds or 0)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.0f}d"


def assess(index=None):
    rows = live_panes()
    keys = {r["key"] for r in rows if r["key"]}
    index = index or load_index()
    files = {index[k]["file"] for k in keys if k in index}
    index, _ = sync(index, only=files) if files else (index, 0)
    missing = [r for r in rows if r["key"] and r["key"] not in index]
    if missing:
        index, _ = sync(index)
    now = time.time()
    for row in rows:
        rec = index.get(row["key"]) if row["key"] else None
        row["verdict"], row["reason"] = verdict(row, rec, now)
        row["rec"] = rec
    return rows, index


# --- close / reopen ---------------------------------------------------------

def closes():
    rows = []
    if os.path.exists(CLOSES_PATH):
        for line in open(CLOSES_PATH, errors="replace"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _save_scrollback(row):
    try:
        text = call("pane.read", {"pane_id": row["pane_id"], "source": "recent_unwrapped",
                                  "lines": 400}).get("read", {})
    except (RuntimeError, OSError):
        return None
    body = text.get("text") if isinstance(text, dict) else None
    if not body:
        return None
    os.makedirs(SCROLLBACK_DIR, exist_ok=True)
    path = os.path.join(SCROLLBACK_DIR, f"{row['terminal_id']}.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def close(rows, index):
    """Record first, close second: a row that fails to record is never closed."""
    done = []
    os.makedirs(STATE_DIR, exist_ok=True)
    # Counted down per close, not read off the snapshot: a batch that sees one
    # static count closes a space's last tabs together and deletes the space.
    remaining = {row["workspace_id"]: row["workspace_panes"] for row in rows}
    for row in rows:
        if row["focused"]:
            row["skipped"] = "the tab you're in"
            continue
        if remaining[row["workspace_id"]] <= 1:
            row["skipped"] = f"last tab in {row['workspace']!r}; closing it deletes the space"
            continue
        rec = index.get(row["key"]) if row["key"] else None
        code, fresh, docs = dirty_files(rec) if rec else ([], [], [])
        event = {"closed_at": time.time(), "key": row["key"], "agent": row["agent"],
                 "title": (rec or {}).get("title") or row["title"],
                 "workspace": row["workspace"], "tab_label": row["tab_label"],
                 "cwd": (rec or {}).get("cwd") or row["cwd"],
                 "terminal_id": row["terminal_id"], "verdict": row.get("verdict"),
                 "resume": (rec or {}).get("resume"),
                 "uncommitted": (code + fresh + docs)[:30],
                 "scrollback": _save_scrollback(row)}
        with open(CLOSES_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        try:
            call("pane.close", {"pane_id": row["pane_id"]})
        except (RuntimeError, OSError) as err:
            row["skipped"] = f"close failed after recording: {err}"
            continue
        remaining[row["workspace_id"]] -= 1
        done.append(event)
    return done


def find_closed(query):
    events = closes()
    if not events:
        return []
    query = query.strip()
    if query.lstrip("#").isdigit() and len(query.lstrip("#")) < 5:
        n = int(query.lstrip("#"))
        ordered = list(reversed(events))
        return [ordered[n - 1]] if 0 < n <= len(ordered) else []
    hits = [e for e in events if e.get("key") and e["key"].split(":", 1)[1].startswith(query)]
    if hits:
        return hits[-1:]
    words = query.lower().split()
    return [e for e in reversed(events)
            if all(w in (e.get("title") or "").lower() for w in words)]


def reopen(event):
    if not event.get("resume"):
        raise RuntimeError("this row has no resume command (a bare shell); open a tab instead")
    snap = call("session.snapshot")["snapshot"]
    target = next((w for w in snap["workspaces"]
                   if w["label"].strip().lower() == (event.get("workspace") or "").strip().lower()),
                  None)
    cwd = event.get("cwd") or os.path.expanduser("~")
    label = event.get("tab_label") or _clip(event.get("title") or "", 44)
    if target:
        made = call("tab.create", {"workspace_id": target["workspace_id"], "cwd": cwd,
                                   "label": label, "focus": True})
    else:
        made = call("workspace.create", {"label": event.get("workspace") or "All / Dump",
                                         "cwd": cwd, "focus": True})
    pane = made["root_pane"]["pane_id"]
    if not target:
        call("tab.rename", {"tab_id": made["root_pane"]["tab_id"], "label": label})
    # Input sent before the new shell draws its prompt is dropped, not queued.
    deadline = time.time() + 10
    while time.time() < deadline:
        screen = call("pane.read", {"pane_id": pane, "source": "visible"})["read"].get("text", "")
        if screen.strip():
            break
        time.sleep(0.2)
    else:
        raise RuntimeError(f"new shell in {pane} never drew a prompt; run: {event['resume']}")
    call("pane.send_text", {"pane_id": pane, "text": event["resume"]})
    call("pane.send_keys", {"pane_id": pane, "keys": ["enter"]})
    return pane, (target or {}).get("label") or event.get("workspace")


# --- dash -------------------------------------------------------------------

_DASH = {"at": 0, "counts": {}}
DASH_TTL_S = 60


def dash_view(limit=8):
    """For herdr-dash: verdict counts (recomputed at most once a minute) and the
    most recent closes. A failed assess keeps the last counts and says so."""
    now = time.time()
    if now - _DASH["at"] > DASH_TTL_S:
        try:
            rows, _ = assess()
            counts = {}
            for row in rows:
                counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
            _DASH.update(at=now, counts=counts, error=None)
        except Exception as err:  # the section degrades; the dash must not die
            _DASH.update(at=now, error=f"{type(err).__name__}: {err}")
    index = load_index()
    events = closes()
    recent = []
    for n, event in enumerate(reversed(events[-limit:]), 1):
        rec = index.get(event.get("key") or "", {})
        text, _ = summary_of(rec) if rec else ("", "")
        recent.append({"n": n, "when": event["closed_at"], "title": event.get("title") or "",
                       "workspace": event.get("workspace") or "",
                       "next": rec.get("next_step") if rec else None, "summary": text})
    return {"recent": recent, "counts": _DASH["counts"], "counts_at": _DASH["at"],
            "error": _DASH.get("error"), "total_closed": len(events)}
