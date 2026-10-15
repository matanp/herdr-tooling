"""Batch /close, shared by herdr-sort (which sends it) and herdr-dash (which answers it).

A session is "in flow" from the moment /close is sent to it until its tab is gone or the
user leaves it. Both tools read the same flow file and the same transcript tails, so the
dash picks up a batch that cull started, and a quit loses nothing: the agents keep working
and the next look rebuilds everything from their transcripts.
"""

import glob
import json
import os
import re
import tempfile
import time

STATE_DIR = os.path.expanduser("~/.local/state/herdr-sort")
FLOW_PATH = os.path.join(STATE_DIR, "close-flow.json")
PROJECTS = os.path.expanduser("~/.claude/projects")
TAIL_BYTES = 1 << 19
STALL_S = 60

# The skill reads this argument as "answered in a batch"; a typed /close carries none.
CLOSE_PROMPT = "/close sent by herdr-sort cull as part of a batch"

# Agents word the keep line freely ("keep, with 1 item blocking", "don't close yet"), and a
# code review's "**Verdict:** CONFIRMED" must not read as a close report.
REPORT_VERDICT = re.compile(r"\*\*Verdict:?\*\*:?\s*((?:keep|close|don.t close)[^\n]*)", re.I)
REPORT_COMMAND = re.compile(r"herdr-sort close\b[^\n`]*?--self\b[^\n`]*")
REPORT_NEXT = re.compile(r"^\*\*Next step:\*\*\s*(.+)$", re.M)
RECAP_TRAILER = re.compile(r"\s*\(disable recaps in /config\)\s*$")
REPORT_ITEM = re.compile(r"^\s*-\s*\[(fail|manual|unverified)\]\s*(.+)$", re.M | re.I)

_paths = {}


def transcript(sid):
    """The Claude transcript for a session id, or None."""
    if sid not in _paths or not os.path.exists(_paths[sid] or ""):
        found = glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl"))
        _paths[sid] = max(found, key=os.path.getmtime) if found else None
    return _paths[sid]


def _ts(text):
    import datetime
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


_tails = {}
_asks = {}


def _tail(path):
    """The end of a transcript, cached on (size, mtime) since the dash asks every few
    seconds: the last assistant text and its time, when /close last arrived, and Claude
    Code's own recaps (away_summary) with their times."""
    try:
        stat = os.stat(path)
    except (OSError, TypeError):
        return {"reply": None, "stamp": None, "close_at": None, "recaps": []}
    sig = (stat.st_size, stat.st_mtime_ns)
    if path in _tails and _tails[path][0] == sig:
        return _tails[path][1]
    info = {"reply": None, "stamp": None, "close_at": None, "recaps": []}
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, stat.st_size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return info
    for line in lines:
        if not any(m in line for m in ('"assistant"', '"away_summary"', "/close</command-name>")):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        kind, stamp = row.get("type"), _ts(row.get("timestamp"))
        if "system" == kind and "away_summary" == row.get("subtype"):
            text = RECAP_TRAILER.sub("", row.get("content") or "").strip()
            if text:
                info["recaps"].append((stamp, text))
        elif "user" == kind and "/close</command-name>" in line:
            info["close_at"] = stamp
        elif "assistant" == kind and not row.get("isSidechain"):
            content = (row.get("message") or {}).get("content")
            texts = [b["text"] for b in content if isinstance(b, dict)
                     and "text" == b.get("type") and b.get("text", "").strip()] \
                if isinstance(content, list) else []
            if texts:
                info["reply"], info["stamp"] = texts[-1], stamp
    _tails[path] = (sig, info)
    return info


def tail_reply(path):
    """(text, timestamp) of the last assistant text in a Claude transcript."""
    info = _tail(path)
    return info["reply"], info["stamp"]


def first_ask(path):
    """The session's first human prompt, read from the head once: it never changes."""
    if path in _asks:
        return _asks[path]
    found = None
    try:
        with open(path, errors="replace") as handle:
            for n, line in enumerate(handle):
                if n > 2000:
                    break
                if '"user"' not in line or '"isMeta":true' in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                content = (row.get("message") or {}).get("content")
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or []
                    if isinstance(b, dict) and "text" == b.get("type"))
                text = text.strip()
                if "user" == row.get("type") and text and not text.startswith("<"):
                    found = " ".join(text.split())
                    break
    except OSError:
        pass
    _asks[path] = found
    return found


_records = {"sig": None, "data": {}}


def load_records():
    """The session records store, re-read only when records.json changes. Any failure
    reads as no records, so every session falls back and the dash keeps drawing."""
    try:
        import herdr_records
        stat = os.stat(herdr_records.RECORDS_PATH)
        sig = (stat.st_size, stat.st_mtime_ns)
        if sig != _records["sig"]:
            _records.update(sig=sig, data=herdr_records.load())
        return _records["data"]
    except Exception:  # noqa: BLE001
        return {}


def recap(sid, path, records):
    """herdr_records.view for the session: its record's recap, else the first ask plus
    Claude Code's newest recap from before /close arrived (later ones describe the close
    itself). None when there is neither."""
    info = _tail(path)
    before = [t for stamp, t in info["recaps"]
              if not info["close_at"] or (stamp or 0) < info["close_at"]]
    fallback = {"first_ask": first_ask(path), "recap": before[-1] if before else None}
    try:
        import herdr_records
        return herdr_records.view(f"claude:{sid}", records, fallback)
    except Exception:  # noqa: BLE001
        text = " · ".join(p for p in fallback.values() if p)
        return {"text": text, "needs": None, "at": None, "source": "fallback"} if text \
            else None


def report(text):
    """What a reply asks of the user: an offer to close, a keep, or a plain reply."""
    text = text or ""
    verdict = REPORT_VERDICT.search(text)
    verdict = verdict.group(1).strip() if verdict else None
    commands = REPORT_COMMAND.findall(text)
    command = commands[-1].strip().rstrip("`") if commands else None
    keep = verdict is not None and not verdict.lower().startswith("close")
    kind = "keep" if keep else "offer" if command else "reply"
    disposition = re.search(r"--as\s+(\w+)", command or "")
    worktree = re.search(r"--remove-worktree\s+(\S+)", command or "")
    nxt = REPORT_NEXT.search(text)
    # The ask is the last paragraph that is prose: not the command, and not the report's
    # **Field:** lines or its "- [fail]" items, which the card shows on their own.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text)
                  if p.strip() and "herdr-sort close" not in p
                  and not p.strip().startswith(("```", "**", "- ["))]
    ask = paragraphs[-1] if paragraphs else ""
    items = sorted(REPORT_ITEM.findall(text), key=lambda i: 0 if "fail" == i[0].lower() else 1)
    return {"kind": kind, "verdict": verdict, "command": command,
            "disposition": disposition.group(1) if disposition else None,
            "worktree": worktree.group(1).strip("'\"") if worktree else None,
            "next": nxt.group(1).strip() if nxt else None, "items": items, "ask": ask,
            # "Run it?" alone is the go-ahead; anything longer bundles a second question
            # that a bare "yes" would leave unanswered.
            "side": ask if "offer" == kind and (len(ask) > 60 or ask.count("?") > 1) else None}


def clean_offer(rep):
    return "offer" == rep["kind"] and not rep["side"]


# --- flow state ---------------------------------------------------------------

def load_flow():
    try:
        with open(FLOW_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save_flow(flow):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="close-flow.", suffix=".tmp", dir=STATE_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(flow, handle)
    os.replace(tmp, FLOW_PATH)


def mark(sid, said, now=None):
    flow = load_flow()
    flow[sid] = dict(flow.get(sid) or {}, sent_at=now or time.time(), said=said, left=None)
    _save_flow(flow)


def leave(sid, reply_at):
    """Hide a session until it replies again; a later reply brings it back."""
    flow = load_flow()
    flow[sid] = dict(flow.get(sid) or {}, left=reply_at or time.time())
    _save_flow(flow)


def prune(live_sids):
    flow = load_flow()
    gone = [sid for sid in flow if sid not in live_sids]
    if gone:
        for sid in gone:
            flow.pop(sid)
        _save_flow(flow)
    return flow


def say(call, pane_id, sid, text):
    """Type text into the session's prompt and record that it is in flow."""
    call("agent.prompt", {"target": pane_id, "text": text})
    mark(sid, text)


# --- what to show ---------------------------------------------------------------

def gather(panes, flow, now=None):
    """(items, checking) for the panes of a herdr snapshot.

    items: idle Claude sessions waiting on the user -- a /close report, or any reply in a
    session the batch is talking to. checking: in-flow sessions still working.
    """
    now = now or time.time()
    records = load_records()
    items, checking = [], []
    for pane in panes:
        sid = (pane.get("agent_session") or {}).get("value")
        if "claude" != pane.get("agent") or not sid:
            continue
        entry = flow.get(sid) or {}
        status = pane.get("agent_status")
        if "working" == status:
            if entry:
                checking.append({"pane_id": pane["pane_id"], "sid": sid,
                                 "title": _title(pane), "since": entry.get("sent_at")})
            continue
        path = transcript(sid)
        text, stamp = tail_reply(path) if path else (None, None)
        rep = report(text)
        sent_at = entry.get("sent_at") or 0
        replied = bool(entry) and (stamp or 0) >= sent_at - 1
        stalled = bool(entry) and not replied and now - sent_at > STALL_S
        pending = bool(rep["verdict"] or rep["command"])
        blocked = bool(entry) and "blocked" == status
        if not (replied or stalled or pending or blocked):
            continue
        if entry.get("left") and (stamp or 0) <= entry["left"]:
            continue
        items.append({"pane_id": pane["pane_id"], "sid": sid, "title": _title(pane),
                      "recap": recap(sid, path, records) if path else None,
                      "status": status, "reply": text, "replied_at": stamp, "report": rep,
                      "said": entry.get("said"), "stalled": stalled, "blocked": blocked,
                      "told_yes": "yes" == (entry.get("said") or "").strip().lower()})
    order = {"offer": 0, "keep": 1, "reply": 2}
    items.sort(key=lambda i: (order[i["report"]["kind"]], -(i["replied_at"] or 0)))
    return items, checking


def _title(pane):
    title = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
    return re.sub(r"^[\s←-⇿─-➿⬀-⯿■-◿️✳]+",
                  "", title).strip() or pane["pane_id"]
