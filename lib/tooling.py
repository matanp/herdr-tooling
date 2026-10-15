"""Per-machine settings for herdr-tooling, from ~/.config/herdr/tooling.toml.

One file says which components this machine runs and how they are configured.
`install` reads it to decide what to link and which key bindings to render;
the tools read it at runtime for paths and for which neighbours exist, so a
dashboard on a machine without herdr-sort drops its tabs section instead of
printing an import error.

A missing file means every component is on, which is how the tools behaved
before this file existed. Everything here is stdlib and Python 3.8+, because the
default python3 on a Mac can be 3.9 and has no tomllib.

Import it from a script symlinked out of the repo with:

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                    "..", "lib"))
"""

import os

import toml_min

PATH = os.environ.get("HERDR_TOOLING_CONFIG") or "~/.config/herdr/tooling.toml"
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# name -> what `install` does for it. Order is the order key blocks render in.
#   bin       repo files linked into ~/.local/bin (src, installed name)
#   plugin    repo plugin directory linked into ~/.local/share/herdr-plugins
#   keys      fragment under config/keys/ appended to config.toml
#   skill     repo skill directory linked into ~/.claude/skills
#   hook      repo script linked into ~/.claude/hooks, run as a SessionStart hook
#   hook_cmd  SessionStart hook command, for a tool already linked into ~/.local/bin
#   requires  components that must also be on
#   linux     reads /proc on its critical path; skipped elsewhere
COMPONENTS = {
    # On, every rendered binding runs through `herdr-keys run KEY --`.
    "keys": {"bin": [("bin/herdr-keys", "herdr-keys")], "plugin": "herdr-keys"},
    "compose": {"bin": [("bin/herdr-compose", "herdr-compose")],
                "keys": "compose.toml"},
    "ccnav": {"bin": [("bin/ccnav-herdr", "ccnav-herdr")],
              "keys": "ccnav.toml", "requires": ["compose"], "linux": True},
    "sessions": {"bin": [("bin/herdr-sessions", "herdr-sessions")], "linux": True},
    "sort": {"bin": [("bin/herdr-sort", "herdr-sort")], "plugin": "herdr-sort",
             "skill": "skills/close"},
    "dash": {"plugin": "herdr-dash", "keys": "dash.toml"},
    # Keys only: the prsessions plugin is installed outside this repo.
    "prsessions": {"keys": "prsessions.toml"},
    "viewer": {"plugin": "viewer", "keys": "viewer.toml"},
    "space-scope": {"bin": [("plugins/space-scope/space-scope", "herdr-space-scope")],
                    "plugin": "space-scope", "keys": "space-scope.toml"},
    "brief-hook": {"hook": "hooks/herdr-project-brief.py"},
    "tabname": {"bin": [("bin/herdr-tabname", "herdr-tabname")],
                "hook_cmd": "herdr-tabname ensure >/dev/null 2>&1 || true"},
}

_cache = {}


def load(path=None):
    """The parsed file, or {} when there is none. Parse errors propagate."""
    path = os.path.expanduser(path or PATH)
    if path not in _cache:
        try:
            _cache[path] = toml_min.load(path)
        except FileNotFoundError:
            _cache[path] = {}
    return _cache[path]


def enabled(name, config=None):
    config = load() if config is None else config
    return bool(config.get("components", {}).get(name, True))


def setting(section, key, default=None, config=None):
    """A value from [section]; strings come back with ~ expanded."""
    config = load() if config is None else config
    value = config.get(section, {}).get(key, default)
    return os.path.expanduser(value) if isinstance(value, str) else value


def briefs(config=None):
    """workspace label -> brief lines, from the [[brief]] tables."""
    config = load() if config is None else config
    out = {}
    for entry in config.get("brief", []):
        label, lines = entry.get("workspace"), entry.get("lines")
        if label and lines:
            out[label] = [str(line) for line in lines]
    return out
