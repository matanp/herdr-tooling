"""Free-text notes that outlive the server.

A plain Markdown file, so a note is greppable and editable without the
dashboard. Per-agent notes key on terminal_id, not pane_id: a pane id is a
position and changes when a tab moves between workspaces, which would silently
re-attach the note to whatever landed in the old slot.

Append-only in the pane; deleting is an edit, and edits go through $EDITOR.
"""

import os
import time

PATH = "~/.local/state/herdr-dash/notes.md"
GLOBAL = "global"
_HEAD = (
    "# herdr-dash notes\n\n"
    "One section per key. `global` is the unattached list; every other section\n"
    "is a terminal_id. Safe to edit by hand.\n"
)


def path():
    return os.path.expanduser(os.environ.get("HERDR_DASH_NOTES") or PATH)


def read():
    """key -> [line, ...], preserving file order."""
    sections, key = {}, None
    try:
        with open(path(), encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip("\n")
                if stripped.startswith("## "):
                    key = stripped[3:].strip()
                    sections.setdefault(key, [])
                elif key and stripped.startswith("- "):
                    sections[key].append(stripped[2:])
    except OSError:
        return {}
    return sections


def write(sections):
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    body = [_HEAD]
    for key in sorted(sections, key=lambda name: (name != GLOBAL, name)):
        lines = sections[key]
        if not lines:
            continue
        body.append(f"\n## {key}\n\n")
        body.extend(f"- {line}\n" for line in lines)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("".join(body))
    os.replace(temporary, target)


def add(text, key=GLOBAL, stamp=None):
    text = " ".join(text.split())
    if not text:
        return False
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp or time.time()))
    sections = read()
    sections.setdefault(key, []).append(f"{when} · {text}")
    write(sections)
    return True


def remove(key, index):
    sections = read()
    lines = sections.get(key) or []
    if not 0 <= index < len(lines):
        return False
    lines.pop(index)
    write(sections)
    return True


def flatten(sections, labels=None):
    """[(key, label, index, line)] with global first, for a single scrollable list."""
    out = []
    for key in sorted(sections, key=lambda name: (name != GLOBAL, name)):
        label = GLOBAL if key == GLOBAL else (labels or {}).get(key, key)
        for index, line in enumerate(sections[key]):
            out.append((key, label, index, line))
    return out
