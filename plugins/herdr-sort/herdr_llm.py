"""Model and Linear adapters for herdr-sort cull, both over `claude -p`.

Every call is a persisted run started from ledger.MODEL_CWD, so a suggestion can be
interrogated later with `claude --resume <model_session>`; the ledger skips that
folder. Structured output comes back through --json-schema.
"""

import collections
import datetime
import json
import os
import re
import subprocess
import time

import herdr_ledger as ledger

TEAM = "Citations Growth"
LINEAR_MCP = {"mcpServers": {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp"}}}
PROJECTS_PATH = os.path.join(ledger.STATE_DIR, "projects.json")
PROJECTS_TTL_S = 86400
TAIL_CHARS = 24000
ISSUE_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

READ_TOOLS = ("mcp__linear__get_issue",)
CHECK_TYPES = ("repo_clean", "pr_merged", "file_exists", "paths_match", "ticket_state",
               "pane_gone", "process_gone", "manual")
OPEN_KINDS = ("uncommitted", "install_drift", "action_left", "unverified", "external_state")


class LLMError(RuntimeError):
    pass


class LinearError(RuntimeError):
    pass


def model():
    return os.environ.get("HERDR_CULL_MODEL", "claude-sonnet-5-5")


def argv(schema, tools=()):
    """--tools "" removes the built-ins; Linear tools exist only when named, and only
    those exact names are allowed, so anything else lands in permission_denials."""
    args = ["claude", "-p", "--model", model(), "--output-format", "json",
            "--json-schema", json.dumps(schema), "--setting-sources", "",
            "--strict-mcp-config", "--tools", ""]
    if tools:
        args += ["--mcp-config", json.dumps(LINEAR_MCP), "--allowedTools", ",".join(tools)]
    return args


def run(prompt, schema, tools=(), error=LLMError, timeout=300):
    """One claude -p call -> (structured output, session id). Stderr alone is not a
    failure: a connector warning is printed on every run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    os.makedirs(ledger.MODEL_CWD, exist_ok=True)
    try:
        out = subprocess.run(argv(schema, tools), input=prompt, capture_output=True,
                             text=True, timeout=timeout, cwd=ledger.MODEL_CWD, env=env)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise error(f"claude -p: {err}") from err
    if out.returncode:
        raise error(ledger._clip(out.stderr or out.stdout or f"exit {out.returncode}", 300))
    try:
        result = json.loads(out.stdout)
    except ValueError as err:
        raise error(f"unparseable claude -p output: {ledger._clip(out.stdout, 200)}") from err
    sid = result.get("session_id")
    if result.get("is_error"):
        raise error(f"claude -p error ({sid}): {ledger._clip(str(result.get('result')), 300)}")
    if result.get("permission_denials"):
        names = [d.get("tool_name") for d in result["permission_denials"]]
        raise error(f"claude -p tried disallowed tools ({sid}): {names}")
    data = result.get("structured_output")
    if data is None:
        try:
            data = json.loads(result.get("result") or "")
        except ValueError as err:
            raise error(f"no structured output ({sid})") from err
    if not isinstance(data, dict):
        raise error(f"structured output is not an object ({sid})")
    return data, sid


# --- transcripts -------------------------------------------------------------

def _text_blocks(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type") in ("text", "input_text",
                                                                  "output_text"))


def _claude_turns(path):
    turns, last_prompt, final = collections.deque(maxlen=600), "", ""
    with open(path, errors="replace") as handle:
        for line in handle:
            if '"type":"attachment"' in line or '"file-history-snapshot"' in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") not in ("user", "assistant") or row.get("isSidechain"):
                continue
            content = (row.get("message") or {}).get("content")
            if "user" == row["type"]:
                if row.get("isMeta"):
                    continue
                text = _text_blocks(content)
                if ledger._is_human_prompt(text):
                    last_prompt = text
                    turns.append(f"USER: {text}")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and "tool_result" == block.get("type"):
                        body = block.get("content")
                        body = body if isinstance(body, str) else _text_blocks(body)
                        turns.append(f"  result: {ledger._clip(body, 300)}")
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if "tool_use" == block.get("type"):
                    args = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    turns.append(f"  tool {block.get('name')}: {ledger._clip(args, 300)}")
                elif "text" == block.get("type") and block.get("text", "").strip():
                    final = block["text"]
                    turns.append(f"ASSISTANT: {final}")
    return turns, last_prompt, final


def _codex_turns(path):
    turns, last_prompt, final = collections.deque(maxlen=600), "", ""
    with open(path, errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload") or {}
            kind, sub = row.get("type"), payload.get("type")
            if "event_msg" == kind and "task_complete" == sub:
                final = payload.get("last_agent_message") or final
            elif "response_item" != kind:
                continue
            elif "message" == sub:
                text = _text_blocks(payload.get("content"))
                if "user" == payload.get("role") and ledger._is_human_prompt(text):
                    last_prompt = text
                    turns.append(f"USER: {text}")
                elif "assistant" == payload.get("role") and text.strip():
                    final = text
                    turns.append(f"ASSISTANT: {text}")
            elif sub in ("custom_tool_call", "function_call"):
                body = payload.get("input") or payload.get("arguments") or ""
                turns.append(f"  tool {payload.get('name')}: {ledger._clip(body, 300)}")
            elif sub in ("custom_tool_call_output", "function_call_output"):
                body = payload.get("output")
                body = _text_blocks(body) if isinstance(body, list) else str(body or "")
                turns.append(f"  result: {ledger._clip(body, 300)}")
    return turns, last_prompt, final


def transcript_tail(rec, budget=TAIL_CHARS):
    """{"tail", "last_prompt", "final_reply"}: the newest turns that fit the budget,
    plus the last human prompt and final assistant message, both unclipped."""
    path = rec.get("file")
    if not path or not os.path.exists(path):
        return {"tail": "", "last_prompt": "", "final_reply": rec.get("last_reply") or ""}
    reader = _codex_turns if "codex" == rec.get("agent") else _claude_turns
    turns, last_prompt, final = reader(path)
    kept, used = [], 0
    for turn in reversed(turns):
        turn = turn if len(turn) <= 4000 else turn[:3999] + "…"
        if used + len(turn) > budget:
            break
        kept.append(turn)
        used += len(turn) + 1
    return {"tail": "\n".join(reversed(kept)), "last_prompt": last_prompt,
            "final_reply": final}


def _when(stamp):
    if not stamp:
        return "?"
    return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


# --- schemas -----------------------------------------------------------------

STR_OR_NULL = {"type": ["string", "null"]}

CLOSE_NOTE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["disposition", "outcome", "next_step", "open_items", "issues", "prs",
                 "project", "draft_title", "draft_body"],
    "properties": {
        "disposition": {"type": "string", "enum": ["done", "filed", "dropped", "trivial"]},
        "outcome": {"type": "string"},
        "next_step": STR_OR_NULL,
        "open_items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "text", "check"],
            "properties": {
                "kind": {"type": "string", "enum": list(OPEN_KINDS)},
                "text": {"type": "string"},
                "check": {
                    "type": "object", "additionalProperties": False, "required": ["type"],
                    "properties": {
                        "type": {"type": "string", "enum": list(CHECK_TYPES)},
                        "path": {"type": "string"}, "pr": {"type": "string"},
                        "a": {"type": "string"}, "b": {"type": "string"},
                        "issue": {"type": "string"}, "state": {"type": "string"},
                        "pane_id": {"type": "string"}, "name": {"type": "string"}}}}}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "prs": {"type": "array", "items": {"type": "string"}},
        "project": STR_OR_NULL,
        "draft_title": {"type": "string"},
        "draft_body": {"type": "string"}}}

RESOLVED_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["resolved_by", "why"],
    "properties": {"resolved_by": STR_OR_NULL, "why": {"type": "string"}}}

PROJECTS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["projects"],
    "properties": {"projects": {"type": "array", "items": {"type": "string"}}}}

ISSUE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["found", "id", "title", "state", "project", "url"],
    "properties": {"found": {"type": "boolean"}, "id": {"type": "string"},
                   "title": {"type": "string"}, "state": {"type": "string"},
                   "project": STR_OR_NULL, "url": {"type": "string"}}}

WRITE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ok", "id", "url", "error"],
    "properties": {"ok": {"type": "boolean"}, "id": {"type": "string"},
                   "url": {"type": "string"}, "error": STR_OR_NULL}}


# --- prompts -----------------------------------------------------------------

CLOSE_NOTE_PROMPT = """\
You are reviewing a coding-agent session whose terminal tab the user is about to close.
Most sessions end on a next step that is the user's; your job is to make sure it is not
lost. Read the session below and return the structured close note.

disposition (a suggestion; the user confirms):
- done: the work is finished and nothing is left that the user must do.
- filed: real work remains; it should become a Linear issue or a comment on one.
- dropped: the session was abandoned or superseded, and nothing in it is worth tracking.
- trivial: a one-off question or empty session; there was never anything to track.
Prefer filed over done when in doubt: a missed next step is the costly mistake.

outcome: one line, what the session achieved. next_step: the single most important thing
left, or null. open_items: everything a closed tab could lose, each typed:
uncommitted (changes not committed), install_drift (an installed copy that may differ
from its repo), action_left (something left for the user to do), unverified (a path that
was never tested), external_state (panes, processes, tickets, deploys). Give each a check
from the fixed set, filling only the fields that check uses:
repo_clean {{path}}, pr_merged {{pr}}, file_exists {{path}}, paths_match {{a, b}},
ticket_state {{issue, state}}, pane_gone {{pane_id}}, process_gone {{name}}, or manual.
Never write a shell command anywhere. Use manual when no listed check fits.

issues: Linear identifiers the session works on (like GROWTH-123). prs: PR numbers as
strings. project: the existing Linear project from the list below this work belongs to,
exactly as spelled there, or null when none fits. draft_title / draft_body: a Linear
issue for this session in Markdown -- what was done, what is left, links -- written for
the user reading it weeks later.

Open GROWTH projects:
{projects}

Session: {title}
Agent: {agent}   cwd: {cwd}   span: {start} -> {last}
First request: {first_ask}
Recap: {recap}
PRs named: {prs}
Files written: {files}

Transcript tail (oldest first; tool calls and results clipped):
<transcript>
{tail}
</transcript>

The user's last prompt, in full:
<last_prompt>
{last_prompt}
</last_prompt>

The final assistant message, in full:
<final_reply>
{final_reply}
</final_reply>
"""

RESOLVED_PROMPT = """\
A coding-agent session was closed while it still had an open next step. Below are
sessions that started after it closed and share a PR, issue, files, or workspace with
it. Decide whether one of them actually resolved that next step.

Mark it resolved only when a later session clearly did the work or settled the question.
Overlapping topic is not enough: a false "resolved" hides the item from the user, which
is worse than leaving it open. resolved_by is the later session's key exactly as given,
or null. why: one sentence citing what in that session resolved it, or why none did.

Closed session ({closed_at}): {title}
Outcome / summary: {summary}
Next step: {next_step}
PRs: {prs}   Files: {files}
Final assistant message:
<final_reply>
{final_reply}
</final_reply>

Later sessions:
{candidates}
"""

PROJECTS_PROMPT = f"""\
Use mcp__linear__list_projects with team "{TEAM}" to list that team's projects. Page
through with the cursor until there are no more. Return the names of the projects that
are not completed and not canceled, exactly as spelled. Do not call any other tool.
"""

ISSUE_PROMPT = """\
Call mcp__linear__get_issue with id "{ident}". Return found=false (other fields empty)
if it does not exist. Otherwise return id (the identifier, like GROWTH-123), title,
state (the status name), project (its name, or null), and url. Change nothing.
"""

COMMENT_PROMPT = """\
Add exactly one comment to Linear issue {ident}: call mcp__linear__save_comment once,
with issueId "{ident}" and the body between the markers below, verbatim (not including
the marker lines). Do not edit the issue, do not touch labels, call nothing else
except mcp__linear__get_issue if you must confirm the issue exists. Return ok, the new
comment's id, its url, and error=null; on failure ok=false and the error.

-----BEGIN BODY-----
{body}
-----END BODY-----
"""

CREATE_PROMPT = """\
Create exactly one Linear issue with a single mcp__linear__save_issue call:
- team: "{team}"
- project: "{project}"
- state: "{state}"
- title: the line between the TITLE markers, verbatim
- description: the text between the BODY markers, verbatim
Pass no other fields. Never pass labels, addLabels, removeLabels, a template, a
delegate, or an assignee: labels in this workspace can dispatch automated agent runs.
If the project does not exist (you may check with mcp__linear__list_projects), do not
create anything and return ok=false with the error. Never create a project or retry
with a different one. Return ok, the new issue's identifier (like GROWTH-123) as id,
its url, and error=null.

-----BEGIN TITLE-----
{title}
-----END TITLE-----

-----BEGIN BODY-----
{body}
-----END BODY-----
"""


def _list(values, limit=20):
    values = list(values or [])
    return ", ".join(values[:limit]) + (f" (+{len(values) - limit})" if len(values) > limit
                                        else "") if values else "(none)"


# --- adapters ----------------------------------------------------------------

class ClaudeLLM:
    def close_note(self, rec):
        tail = transcript_tail(rec)
        try:
            projects = self.projects()
        except LLMError:
            projects = []
        prompt = CLOSE_NOTE_PROMPT.format(
            projects="\n".join(f"- {p}" for p in projects) or "(unavailable)",
            title=rec.get("title") or "(untitled)", agent=rec.get("agent"),
            cwd=rec.get("cwd") or "?", start=_when(rec.get("start")),
            last=_when(rec.get("last")), first_ask=rec.get("first_ask") or "(none)",
            recap=rec.get("recap") or rec.get("summary") or "(none)",
            prs=_list(rec.get("prs")), files=_list(rec.get("files"), 40),
            tail=tail["tail"] or "(empty)", last_prompt=tail["last_prompt"] or "(none)",
            final_reply=tail["final_reply"] or "(none)")
        note, sid = run(prompt, CLOSE_NOTE_SCHEMA)
        note["model_session"] = sid
        if note.get("project") not in projects:
            note["project"] = None
        return note

    def resolved_later(self, closed, candidates):
        if not candidates:
            return {"resolved_by": None, "why": "no later sessions to check",
                    "model_session": None}
        rec, event = closed.get("rec") or {}, closed.get("event") or {}
        final = transcript_tail(rec, budget=0)["final_reply"] if rec else ""
        blocks = []
        for cand in candidates:
            text, _ = ledger.summary_of(cand)
            blocks.append("\n".join([
                f"key: {cand['key']}",
                f"title: {cand.get('title') or '(untitled)'}   started: "
                f"{_when(cand.get('start'))}",
                f"summary: {ledger._clip(text, 600)}",
                f"next step: {cand.get('next_step') or '(none)'}",
                f"PRs: {_list(cand.get('prs'))}   files: {_list(cand.get('files'), 15)}",
                f"final reply: {ledger._clip(cand.get('last_reply'), 600)}"]))
        summary, _ = ledger.summary_of(rec) if rec else ("", "")
        prompt = RESOLVED_PROMPT.format(
            closed_at=_when(event.get("closed_at")),
            title=event.get("title") or rec.get("title") or "(untitled)",
            summary=summary or "(none)", next_step=closed.get("next_step") or "(none)",
            prs=_list(rec.get("prs")), files=_list(rec.get("files"), 20),
            final_reply=final or rec.get("last_reply") or "(none)",
            candidates="\n\n".join(blocks))
        data, sid = run(prompt, RESOLVED_SCHEMA)
        keys = {c["key"] for c in candidates}
        return {"resolved_by": data.get("resolved_by") if data.get("resolved_by") in keys
                else None, "why": data.get("why") or "", "model_session": sid}

    def projects(self):
        """Open GROWTH project names, cached on disk for a day; a stale cache beats
        none when the refresh fails."""
        cached = {}
        try:
            with open(PROJECTS_PATH, encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            pass
        if cached.get("names") and time.time() - cached.get("at", 0) < PROJECTS_TTL_S:
            return cached["names"]
        try:
            data, _ = run(PROJECTS_PROMPT, PROJECTS_SCHEMA, tools=("mcp__linear__list_projects",))
        except LLMError:
            if cached.get("names"):
                return cached["names"]
            raise
        names = sorted({n.strip() for n in data.get("projects") or [] if n.strip()})
        os.makedirs(ledger.STATE_DIR, exist_ok=True)
        with open(PROJECTS_PATH, "w", encoding="utf-8") as handle:
            json.dump({"at": time.time(), "names": names}, handle)
        return names


class ClaudeLinear:
    def issue(self, ident):
        data, _ = run(ISSUE_PROMPT.format(ident=ident), ISSUE_SCHEMA, tools=READ_TOOLS,
                      error=LinearError)
        if not data.get("found"):
            raise LinearError(f"{ident}: no such issue")
        return {"id": data["id"], "title": data["title"], "state": data["state"],
                "project": data.get("project"), "url": data["url"]}

    def comment(self, ident, body):
        if not ISSUE_RE.match(ident or ""):
            raise LinearError(f"not an issue identifier: {ident!r}")
        data, sid = run(COMMENT_PROMPT.format(ident=ident, body=body), WRITE_SCHEMA,
                        tools=("mcp__linear__get_issue", "mcp__linear__save_comment"),
                        error=LinearError)
        if not data.get("ok") or not data.get("id"):
            raise LinearError(f"comment on {ident} failed ({sid}): {data.get('error')}")
        return {"id": data["id"], "issue": ident, "url": data.get("url") or ""}

    def create_issue(self, title, body, project, state):
        if state not in ("Done", "Backlog"):
            raise LinearError(f"refusing state {state!r}: only Done or Backlog")
        prompt = CREATE_PROMPT.format(team=TEAM, project=project, state=state,
                                      title=" ".join((title or "").split()), body=body)
        data, sid = run(prompt, WRITE_SCHEMA,
                        tools=("mcp__linear__list_projects", "mcp__linear__save_issue"),
                        error=LinearError)
        if not data.get("ok") or not ISSUE_RE.match(data.get("id") or ""):
            raise LinearError(f"create in {project!r} failed ({sid}): "
                              f"{data.get('error') or data.get('id')}")
        return {"id": data["id"], "url": data.get("url") or ""}
