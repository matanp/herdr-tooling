"""Worktree inventory: how many exist, and which are nobody's.

The interesting number is not the count but the gap between it and the number
herdr has a workspace open for. A worktree with no open workspace is dormant;
a detached one has no branch to go back to, which is what an abandoned rebase
leaves behind. Both are prune candidates, and both cost a full checkout of
disk until someone looks.

`is_prunable` is git's own answer and means the working tree is already gone,
so it is reported separately rather than folded into the dormant count.
"""

from . import herdr as herdr_mod


def scan(source="herdr-dash"):
    result = herdr_mod.Herdr(source).call("worktree.list")
    entries = result.get("worktrees") or []
    dormant, detached, prunable = [], [], []
    for entry in entries:
        name = (entry.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
        if entry.get("is_prunable"):
            prunable.append(name)
        if entry.get("is_detached"):
            detached.append(name)
        if not entry.get("open_workspace_id"):
            dormant.append(name)
    return {
        "total": len(entries),
        "open": sum(1 for entry in entries if entry.get("open_workspace_id")),
        "dormant": dormant,
        "detached": detached,
        "prunable": prunable,
        "repo": (result.get("source") or {}).get("repo_name") or "",
    }
