"""Prefix cheat-sheet, generated from the live config rather than written down.

Caps Lock is the real prefix: a Ghostty-scoped Karabiner chord emits ctrl+b, so
the config pins ctrl+b and the sheet displays what the fingers actually do.

Built-in defaults come from `herdr --default-config`, where every action is
present but commented out. Uncommenting is what the parser does here, so an
action herdr adds in a later release appears without this file changing.
"""

import os
import re
import subprocess

from . import toml_min

CONFIG = "~/.config/herdr/config.toml"
PREFIX_LABEL = "Caps Lock"

_TRAILING = re.compile(r'^\s*key\s*=\s*"([^"]+)"\s*#\s*(.+?)\s*$')
_DEFAULT = re.compile(r'^#\s*([a-z_]+)\s*=\s*"([^"]*)"(?:\s*#\s*(.*))?$')
_SECTION = re.compile(r"^\[")


def _annotations(path):
    """key -> trailing comment, which the TOML parser drops but the sheet wants."""
    out = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                match = _TRAILING.match(line)
                if match:
                    out[match.group(1)] = match.group(2)
    except OSError:
        pass
    return out


def builtins():
    """Default action bindings, read out of the commented-out reference config."""
    try:
        proc = subprocess.run(["herdr", "--default-config"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if 0 != proc.returncode:
        return []
    out, inside = [], False
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        # A commented-out header still ends the section: [keys] contains a
        # worked [[keys.command]] example whose lines read as fake actions.
        body = stripped[1:].strip() if stripped.startswith("#") else stripped
        if _SECTION.match(body):
            inside = "[keys]" == body
            continue
        if not inside:
            continue
        match = _DEFAULT.match(stripped)
        if not match:
            continue
        action, binding, note = match.groups()
        if "prefix" == action or not binding:
            continue
        out.append({"action": action, "key": binding, "note": note or ""})
    return out


def sheet(path=None):
    path = os.path.expanduser(path or CONFIG)
    try:
        config = toml_min.load(path)
        error = None
    except (toml_min.TomlError, OSError) as err:
        config, error = {}, str(err)

    keys = config.get("keys") or {}
    notes = _annotations(path)
    custom = []
    for entry in keys.get("command") or []:
        binding = entry.get("key") or "?"
        custom.append({
            "key": binding,
            "type": entry.get("type") or "shell",
            "command": entry.get("command") or "",
            "note": notes.get(binding, ""),
        })

    overrides = {name: value for name, value in keys.items()
                 if name not in ("command", "indexed", "prefix")
                 and isinstance(value, str)}

    defaults = [item for item in builtins() if item["action"] not in overrides]
    return {
        "path": path,
        "error": error,
        "prefix": keys.get("prefix") or "ctrl+b",
        "prefix_label": PREFIX_LABEL,
        "custom": custom,
        "overrides": sorted(overrides.items()),
        "defaults": defaults,
    }


def pretty(binding, prefix_label=PREFIX_LABEL):
    return binding.replace("prefix+", f"{prefix_label}+", 1)
