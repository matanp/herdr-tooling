"""Cull core for herdr-sort: what each open tab or dropped close should become.

`assess` builds cards -- trivial rules, grouping, close notes, open-item checks,
artifact checks, and for closed sessions whether a later session resolved the next
step. `record` turns a decision on a card into Linear writes and close-event fields.
Both take their model, Linear and probe dependencies as arguments; the CLI owns
rendering, keys, and the ledger writes.
"""

import concurrent.futures
import filecmp
import json
import os
import re
import subprocess

import herdr_ledger as ledger

DISPOSITIONS = ("done", "filed", "merged", "dropped", "trivial", "kept")
CHECK_TYPES = ("repo_clean", "pr_merged", "file_exists", "paths_match", "ticket_state",
               "pane_gone", "process_gone", "manual")
FALLBACK_PROJECT = "Agent session log"
ISSUE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
MAX_CANDIDATES = 12
RESOLVE_PREFIX = "resolved_later:"


# --- trivial rules ----------------------------------------------------------

def _trivial(row, rec):
    if row is not None and not row.get("agent") and "CLOSE" == row.get("verdict"):
        return "idle shell"
    if rec is None:
        return None
    if not rec.get("prompts"):
        return "no prompts"
    if not rec.get("tool_calls") and not rec.get("files") and not rec.get("prs"):
        return "no tool calls, files or PRs"
    return None


def _written(rec):
    return [p for p in (rec or {}).get("files") or []
            if not p.startswith(ledger.IGNORED_PREFIXES)]


def _docs(rec):
    return [p for p in _written(rec) if p.endswith(".md") or "/docs/" in p]


def _text_issues(rec):
    rec = rec or {}
    blob = " ".join([rec.get("title") or "", rec.get("recap") or "", rec.get("first_ask") or "",
                     rec.get("last_reply") or ""] + list(rec.get("asks") or []))
    return set(ISSUE_RE.findall(blob))


# --- close notes ------------------------------------------------------------

def _notes_for(recs, llm, notes, workers, progress):
    """key -> (note, error). Re-asks the model only when the transcript size moved."""
    out, todo = {}, {}
    for rec in recs:
        cached = notes.get(rec["key"])
        if cached and cached.get("size") == rec.get("size") and cached.get("note"):
            out[rec["key"]] = (cached["note"], None)
        else:
            todo[rec["key"]] = rec
    if not todo:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {pool.submit(llm.close_note, rec): rec for rec in todo.values()}
        for n, job in enumerate(concurrent.futures.as_completed(jobs), 1):
            rec = jobs[job]
            try:
                note = job.result()
            # Any model failure degrades to a card with no suggestion; one bad call
            # must not stop the walk from being built.
            except Exception as err:  # noqa: BLE001
                out[rec["key"]] = (None, f"{type(err).__name__}: {err}")
            else:
                notes[rec["key"]] = {"size": rec.get("size"), "note": note}
                out[rec["key"]] = (note, None)
            if progress:
                progress(f"close notes {n}/{len(todo)}")
    return out


# --- checks -----------------------------------------------------------------

def _run_check(probe, check):
    kind = (check or {}).get("type")
    if kind not in CHECK_TYPES or "manual" == kind:
        return "manual", "" if "manual" == kind else f"unknown check {kind!r}"
    try:
        if "repo_clean" == kind:
            ok = probe.repo_clean(check["path"])
            return ("pass", "clean") if ok else ("fail", f"uncommitted changes in {check['path']}")
        if "pr_merged" == kind:
            state = probe.pr_state(check["pr"])
            if state is None:
                return "manual", f"PR #{check['pr']} state unknown"
            if "merged" == state:
                return "pass", "merged"
            return "fail", f"PR #{check['pr']} {state}"
        if "file_exists" == kind:
            ok = probe.file_exists(check["path"])
            return ("pass", "present") if ok else ("fail", f"missing: {check['path']}")
        if "paths_match" == kind:
            ok = probe.paths_match(check["a"], check["b"])
            return ("pass", "match") if ok else ("fail", f"{check['a']} differs from {check['b']}")
        if "ticket_state" == kind:
            state = probe.ticket_state(check["issue"])
            if state is None:
                return "manual", f"{check['issue']} state unknown"
            if state.strip().lower() == str(check["state"]).strip().lower():
                return "pass", state
            return "fail", f"{check['issue']} is {state}, wanted {check['state']}"
        if "pane_gone" == kind:
            alive = probe.pane_alive(check["pane_id"])
            return ("fail", f"pane {check['pane_id']} still open") if alive else ("pass", "gone")
        alive = probe.process_alive(check["name"])
        return ("fail", f"{check['name']} still running") if alive else ("pass", "gone")
    except Exception as err:  # noqa: BLE001 -- a probe failure is "go look", never a crash
        return "manual", f"check failed: {type(err).__name__}: {err}"


def _checked_items(notes):
    items = []
    for note in notes:
        for item in note.get("open_items") or []:
            items.append(item)
    return items


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def _artifacts(members, notes, probe):
    prs, docs, issues = [], [], []
    for member in members:
        prs.extend((member["rec"] or {}).get("prs") or [])
        docs.extend(_docs(member["rec"]))
    for note in notes:
        prs.extend(str(p) for p in note.get("prs") or [])
        issues.extend(note.get("issues") or [])
    return {
        "prs": [{"pr": p, "state": _safe(probe.pr_state, p)} for p in dict.fromkeys(prs)],
        "docs": [{"path": p, "exists": bool(_safe(probe.file_exists, p))}
                 for p in dict.fromkeys(docs)],
        "issues": [{"id": i, "state": _safe(probe.ticket_state, i)}
                   for i in dict.fromkeys(issues)],
    }


# --- cards ------------------------------------------------------------------

def _card(kind, members, notes_by_key, probe, trivial=None):
    keyed = [m for m in members if m["key"] in notes_by_key]
    errors = [notes_by_key[m["key"]][1] for m in keyed if notes_by_key[m["key"]][1]]
    for member in members:
        member["note"] = notes_by_key.get(member["key"], (None, None))[0]
    good = [m for m in members if m["note"]]
    primary = max(good or members, key=lambda m: (m["rec"] or {}).get("last") or 0)
    note = primary["note"]
    member_notes = [m["note"] for m in good]
    items = []
    for item in _checked_items(member_notes):
        result, detail = _run_check(probe, item.get("check"))
        items.append(dict(item, result=result, detail=detail))
    if not trivial and not keyed and not good:
        errors.append("no transcript found")
    suggested = None if errors or note is None else note.get("disposition")
    done_ok = bool(member_notes and not errors
                   and all("done" == n.get("disposition") for n in member_notes)
                   and all(i["result"] != "fail" for i in items))
    first = members[0]
    row, rec, event = primary["row"], primary["rec"], primary["event"]
    title = ((rec or {}).get("title") or (row or {}).get("title")
             or (event or {}).get("title") or "(shell)")
    if len(members) > 1:
        title += f" (+{len(members) - 1})"
    if "open" == kind:
        ident = "open:" + ",".join(str(m["pane_id"] or m["key"]) for m in members)
        workspace = (first["row"] or {}).get("workspace") or ""
    else:
        ident = f"loop:{first['key']}@{first['event'].get('closed_at')}"
        workspace = first["event"].get("workspace") or ""
    held = any((m["row"] or {}).get("focused") or "KEEP" == (m["row"] or {}).get("verdict")
               or (m["row"] or {}).get("status") in ("working", "blocked") for m in members)
    auto = None if held else ("trivial" if trivial else ("done" if done_ok else None))
    return {"id": ident, "kind": kind, "members": members, "title": title,
            "workspace": workspace, "trivial": trivial, "note": note,
            "note_error": "; ".join(errors) or None, "open_items": items,
            "artifacts": _artifacts(members, member_notes, probe), "suggested": suggested,
            "done_ok": done_ok, "auto": auto, "resolved_by": None,
            "project": (note or {}).get("project")}


def _group(members, notes_by_key):
    """Union members sharing a PR, a referenced issue, or a written file."""
    parent = list(range(len(members)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner = {}
    for i, member in enumerate(members):
        rec = member["rec"] or {}
        note = notes_by_key.get(member["key"], (None, None))[0] or {}
        marks = ([("pr", str(p)) for p in (rec.get("prs") or []) + (note.get("prs") or [])]
                 + [("issue", i_) for i_ in note.get("issues") or []]
                 + [("file", f) for f in _written(rec)])
        for mark in marks:
            if mark in owner:
                parent[find(i)] = find(owner[mark])
            else:
                owner[mark] = i
    groups = {}
    for i, member in enumerate(members):
        groups.setdefault(find(i), []).append(member)
    return list(groups.values())


def _rec_for(row, index):
    return (index.get(row["key"]) if row.get("key") else None) or row.get("rec")


def _open_cards(rows, index, notes_by_key, probe):
    cards, plain = [], []
    for row in rows:
        rec = _rec_for(row, index)
        member = {"key": row.get("key"), "pane_id": row.get("pane_id"), "row": row,
                  "rec": rec, "event": None}
        reason = _trivial(row, rec)
        if reason:
            cards.append(_card("open", [member], notes_by_key, probe, trivial=reason))
        else:
            plain.append(member)
    for group in _group(plain, notes_by_key):
        cards.append(_card("open", group, notes_by_key, probe))
    return cards


# --- loops ------------------------------------------------------------------

def _loop_events(closes, index, rows):
    """Latest close per session, if it has no disposition and is not trivial by rule."""
    open_keys = {r.get("key") for r in rows if r.get("key")}
    latest = {}
    for event in closes:
        if event.get("kind") == "resolve" or not event.get("key"):
            continue
        latest[event["key"]] = event
    out = []
    for key, event in latest.items():
        rec = index.get(key)
        if event.get("disposition") or key in open_keys or rec is None:
            continue
        if _trivial(None, rec):
            continue
        out.append((event, rec))
    return out


def _marks(rec, note):
    prs = {str(p) for p in (rec.get("prs") or []) + ((note or {}).get("prs") or [])}
    issues = set((note or {}).get("issues") or []) | _text_issues(rec)
    return prs, issues, set(_written(rec))


def _candidates(event, rec, note, index, closes, rows, notes):
    prs, issues, files = _marks(rec, note)
    workspace_of = {e["key"]: e.get("workspace") for e in closes if e.get("key")}
    workspace_of.update({r["key"]: r.get("workspace") for r in rows if r.get("key")})
    after = event.get("closed_at") or 0
    scored = []
    for cand in index.values():
        if cand["key"] == rec["key"] or (cand.get("start") or 0) <= after:
            continue
        c_note = (notes.get(cand["key"]) or {}).get("note")
        c_prs, c_issues, c_files = _marks(cand, c_note)
        score = (3 * bool(prs & c_prs) + 3 * bool(issues & c_issues) + 2 * bool(files & c_files)
                 + bool(event.get("workspace")
                        and workspace_of.get(cand["key"]) == event.get("workspace"))
                 + bool(event.get("cwd") and cand.get("cwd") == event.get("cwd")))
        if score:
            scored.append((-score, cand.get("start") or 0, cand))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [s[2] for s in scored[:MAX_CANDIDATES]]


def _resolve_sig(candidates):
    return ",".join(f"{c['key']}:{c.get('size')}" for c in candidates)


def _resolve_loops(cards, index, closes, rows, llm, notes, workers, progress):
    todo = []
    for card in cards:
        member = card["members"][0]
        cands = _candidates(member["event"], member["rec"], card["note"], index, closes, rows,
                            notes)
        if not cands:
            continue
        cache_key = f"{RESOLVE_PREFIX}{member['key']}@{member['event'].get('closed_at')}"
        cached = notes.get(cache_key)
        if cached and cached.get("sig") == _resolve_sig(cands):
            _apply_resolution(card, cached.get("result"), cands)
            continue
        todo.append((card, cands, cache_key))
    if not todo:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {}
        for card, cands, cache_key in todo:
            member = card["members"][0]
            closed = {"rec": member["rec"], "event": member["event"],
                      "next_step": _next_step(card)}
            jobs[pool.submit(llm.resolved_later, closed, cands)] = (card, cands, cache_key)
        for n, job in enumerate(concurrent.futures.as_completed(jobs), 1):
            card, cands, cache_key = jobs[job]
            try:
                result = job.result()
            except Exception:  # noqa: BLE001 -- unresolved is the safe direction
                result = None
            else:
                notes[cache_key] = {"sig": _resolve_sig(cands), "result": result}
            _apply_resolution(card, result, cands)
            if progress:
                progress(f"resolved-later checks {n}/{len(todo)}")


def _apply_resolution(card, result, candidates):
    by_key = {c["key"]: c for c in candidates}
    hit = by_key.get((result or {}).get("resolved_by"))
    if hit:
        card["resolved_by"] = {"key": hit["key"], "title": hit.get("title") or "",
                               "why": result.get("why") or "",
                               "model_session": result.get("model_session")}
        card["auto"] = None


def _next_step(card):
    member = card["members"][0]
    return ((card["note"] or {}).get("next_step") or (member["rec"] or {}).get("next_step")
            or None)


def _has_next_step(card):
    if card["note_error"] and card["note"] is None:
        return True
    return bool(_next_step(card) or card["open_items"])


# --- public -----------------------------------------------------------------

def assess(rows, index, closes, llm, now, notes=None, probe=None, progress=None, workers=6):
    """Cards for every open tab (kind "open") and every unresolved close (kind "loop").

    A loop whose note could not be built is kept: a missed next step is the costly
    error, so an unknown one is shown rather than hidden. Loops that a later session
    resolved are returned with `resolved_by` set.
    """
    notes = {} if notes is None else notes
    probe = probe or RealProbe()
    loop_events = _loop_events(closes, index, rows)
    want = []
    for row in rows:
        rec = _rec_for(row, index)
        if rec and not _trivial(row, rec):
            want.append(rec)
    want.extend(rec for _, rec in loop_events)
    notes_by_key = _notes_for(want, llm, notes, workers, progress)
    cards = _open_cards(rows, index, notes_by_key, probe)
    loops = []
    for event, rec in sorted(loop_events, key=lambda er: er[0].get("closed_at") or 0):
        member = {"key": event["key"], "pane_id": None, "row": None, "rec": rec, "event": event}
        card = _card("loop", [member], notes_by_key, probe)
        if _has_next_step(card):
            loops.append(card)
    _resolve_loops(loops, index, closes, rows, llm, notes, workers, progress)
    return cards + loops


def draft(card, disposition):
    """Title and body that `record` files when the decision carries none."""
    note = card.get("note") or {}
    title = note.get("draft_title") or card["title"]
    lines = []
    if note.get("draft_body"):
        lines += [note["draft_body"].strip(), ""]
    if note.get("outcome"):
        lines.append(f"Outcome: {note['outcome']}")
    step = _next_step(card)
    if step:
        lines.append(f"Next step: {step}")
    pending = [i for i in card.get("open_items") or [] if i.get("result") in ("fail", "manual")]
    if pending:
        lines += ["", "Open items:"]
        lines += [f"- [{i['result']}] {i.get('text') or i.get('kind')}"
                  + (f" ({i['detail']})" if i.get("detail") else "") for i in pending]
    lines += ["", "Sessions:"]
    for member in card["members"]:
        rec = member["rec"] or {}
        resume = rec.get("resume") or (member["event"] or {}).get("resume") or "(no resume command)"
        name = rec.get("title") or (member["row"] or {}).get("title") or member["key"] or "(shell)"
        lines.append(f"- {name}: `{resume}`")
    arts = card.get("artifacts") or {}
    if arts.get("prs"):
        lines += ["", "PRs: " + ", ".join(
            f"#{p['pr']}" + (f" ({p['state']})" if p.get("state") else "") for p in arts["prs"])]
    if arts.get("docs"):
        lines += ["", "Docs:"] + [f"- {d['path']}" + ("" if d.get("exists") else " (missing)")
                                  for d in arts["docs"]]
    if "merged" == disposition:
        lines.insert(0, "Merged from a closed tab working on the same thing.\n")
    return {"title": title, "body": "\n".join(lines).strip() + "\n"}


def record(card, decision, linear, now):
    """Carry out a decision: Linear writes first, then one CloseFields per member.

    A Linear failure propagates and nothing is returned, so the caller never records
    a close whose filing did not happen.
    """
    disposition = decision.get("disposition")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")
    if "kept" == disposition:
        return []
    note = card.get("note") or {}
    text = (decision.get("note") or "").strip() or None
    ref = ref_url = None
    if "dropped" == disposition and not text:
        raise ValueError("dropped needs a reason")
    if "trivial" == disposition:
        text = text or card.get("trivial")
    elif disposition in ("merged", "done", "filed"):
        known = list(note.get("issues") or []) + [
            i["id"] for i in (card.get("artifacts") or {}).get("issues") or []]
        target = decision.get("ref") or (None if "merged" == disposition
                                         else next(iter(known), None))
        if "merged" == disposition and not target:
            raise ValueError("merged needs --ref: the surviving tab's ticket")
        made = draft(card, disposition)
        body = decision.get("body") or made["body"]
        if target:
            comment = linear.comment(target, body)
            ref, ref_url = f"{target}#{comment['id']}", comment.get("url")
        else:
            issue = linear.create_issue(
                decision.get("title") or made["title"], body,
                decision.get("project") or card.get("project") or FALLBACK_PROJECT,
                "Done" if "done" == disposition else "Backlog")
            ref, ref_url = issue["id"], issue.get("url")
        if not text:
            text = note.get("outcome") if "done" == disposition else _next_step(card)
    out = []
    for member in card["members"]:
        fields = {"key": member["key"], "pane_id": member["pane_id"],
                  "disposition": disposition, "note": text, "ref": ref, "ref_url": ref_url,
                  "model_session": (member.get("note") or note).get("model_session")}
        if "loop" == card["kind"]:
            fields["closed_at"] = member["event"].get("closed_at")
        out.append(fields)
    return out


class RealProbe:
    """Live state for open-item checks. Every method may raise; assess turns that into
    a manual result."""

    def __init__(self, linear=None):
        self.linear = linear
        self._panes = None

    def repo_clean(self, path):
        path = os.path.expanduser(path)
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        out = subprocess.run(["git", "-C", folder, "status", "--porcelain", "--", path],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise RuntimeError(ledger._clip(out.stderr, 160))
        return not out.stdout.strip()

    def pr_state(self, n):
        out = subprocess.run(["ghapi", "pr", str(n).lstrip("#"), "--json"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode:
            return None
        pr = json.loads(out.stdout)
        if pr.get("merged_at") or pr.get("merged"):
            return "merged"
        return pr.get("state")

    def file_exists(self, path):
        return os.path.exists(os.path.expanduser(path))

    def paths_match(self, a, b):
        a, b = os.path.expanduser(a), os.path.expanduser(b)
        if os.path.isdir(a) and os.path.isdir(b):
            return _dirs_match(filecmp.dircmp(a, b))
        if not (os.path.isfile(a) and os.path.isfile(b)):
            raise RuntimeError(f"cannot compare {a} and {b}")
        return filecmp.cmp(a, b, shallow=False)

    def ticket_state(self, ident):
        if self.linear is None:
            return None
        return self.linear.issue(ident).get("state")

    def pane_alive(self, pane_id):
        if self._panes is None:
            snap = ledger.call("session.snapshot")["snapshot"]
            self._panes = {p["pane_id"] for p in snap["panes"]}
        return pane_id in self._panes

    def process_alive(self, name):
        out = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True, timeout=10)
        if out.returncode not in (0, 1):
            raise RuntimeError(f"pgrep exit {out.returncode}")
        return 0 == out.returncode


def _dirs_match(cmp):
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(cmp.left, cmp.right, cmp.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(_dirs_match(sub) for sub in cmp.subdirs.values())
