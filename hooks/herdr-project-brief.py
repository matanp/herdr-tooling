#!/usr/bin/env python3
"""SessionStart hook: tell a new Claude session which project its space owns.

Resolves HERDR_WORKSPACE_ID to a workspace label over the herdr socket, looks
that label up against the `brief` keys in sort-rules.toml, and returns the
brief as additionalContext so an unqualified prompt ("status?") resolves.

Every failure path is silent and exit 0: a session start must never be blocked
by this, and an unmatched or renamed workspace must inject nothing rather than
fall back to a guess, because a brief naming the wrong project is worse for
every prompt in that pane than no brief at all.
"""

import json
import os
import re
import socket
import sys

RULES = os.path.expanduser("~/.config/herdr/sort-rules.toml")
RULE_SPLIT = re.compile(r"^\[\[rule\]\]\s*$", re.M)
WORKSPACE = re.compile(r'^workspace\s*=\s*"([^"]*)"', re.M)
BRIEF = re.compile(r"^brief\s*=\s*\[(.*?)\]\s*$", re.M | re.S)
QUOTED = re.compile(r'"([^"]*)"')
# Anchored at a token boundary, not \b: "linear.app/acme/project" would
# otherwise read as the repo path "app/acme/project" and silently drop
# every line carrying a Linear URL.
REPO_PATH = re.compile(r"(?:^|(?<=\s))(?:docs|app|lib|spec|config|db)/[A-Za-z0-9._/-]+")


def briefs():
    with open(RULES, encoding="utf-8") as handle:
        chunks = RULE_SPLIT.split(handle.read())
    out = {}
    for chunk in chunks[1:]:
        label = WORKSPACE.search(chunk)
        body = BRIEF.search(chunk)
        if label and body:
            lines = QUOTED.findall(body.group(1))
            if lines:
                out[label.group(1)] = lines
    return out


def workspace_label(workspace_id, socket_path):
    request = {
        "id": "herdr-project-brief",
        "method": "workspace.get",
        "params": {"workspace_id": workspace_id},
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        client.close()
    response = json.loads(buf.split(b"\n", 1)[0])
    return response["result"]["workspace"]["label"]


def live(line, cwd):
    """Drop a line whose doc pointer has been moved or archived away."""
    for path in REPO_PATH.findall(line):
        if not os.path.exists(os.path.join(cwd or "", path)):
            return False
    return True


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if payload.get("agent_id"):
        return
    # resume replays a transcript that already carries the brief; compact drops
    # it, which is the case that has to re-inject.
    if payload.get("source") == "resume":
        return
    if os.environ.get("HERDR_ENV") != "1":
        return
    workspace_id = os.environ.get("HERDR_WORKSPACE_ID")
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not workspace_id or not socket_path:
        return
    brief = briefs().get(workspace_label(workspace_id, socket_path))
    if not brief:
        return
    lines = [line for line in brief if live(line, payload.get("cwd"))]
    if not lines:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
