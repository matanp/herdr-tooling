"""Remove a session's linked git worktree once its work is on a remote.

`herdr-sort close --remove-worktree PATH` calls check() before anything is written and
remove() after the close is recorded. Removal is refused unless the worktree is linked (not a
main checkout), unlocked, has no tracked changes or operation in progress, and its HEAD is
contained in a remote-tracking ref, and nothing else uses it: no other open pane's cwd and
no installed symlink points inside it. Before removing, commits only its HEAD reflog reaches are
tagged wt-removed/<name>/<sha>, and untracked files are copied under SAVE_ROOT.
"""

import os
import shutil
import subprocess
import time

SAVE_ROOT = os.path.expanduser("~/.local/state/herdr-sort/removed-worktrees")
LINK_DIRS = ("~/.local/bin", "~/.claude/skills", "~/.claude/tools", "~/.claude/agents",
             "~/.claude/commands", "~/.local/share/herdr-plugins")
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
               "REVERT_HEAD", "BISECT_LOG")


class Refused(Exception):
    pass


def _git(cwd, *args, check=True):
    out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if check and out.returncode:
        raise Refused(f"git {args[0]} failed: {out.stderr.strip() or out.returncode}")
    return out.stdout


def _inside(child, root):
    child = os.path.realpath(child)
    return child == root or child.startswith(root + os.sep)


def _links_into(path, link_dirs):
    """Symlinks one or two levels under link_dirs that resolve inside path: an install that
    runs from the worktree would break when it goes."""
    found = []
    for base in (os.path.expanduser(d) for d in link_dirs):
        try:
            entries = [os.path.join(base, n) for n in os.listdir(base)]
        except OSError:
            continue
        for entry in entries:
            nested = []
            if os.path.isdir(entry) and not os.path.islink(entry):
                try:
                    nested = [os.path.join(entry, n) for n in os.listdir(entry)]
                except OSError:
                    pass
            found += [e for e in [entry] + nested
                      if os.path.islink(e) and _inside(os.path.realpath(e), path)]
    return found


def check(path, other_cwds=(), link_dirs=None):
    """Facts about the worktree at path; raises Refused with the first reason it must stay.
    other_cwds are the working directories of every other open pane."""
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise Refused(f"{path} is not a directory")
    users = [c for c in other_cwds if c and _inside(c, path)]
    if users:
        raise Refused(f"{path} is another open pane's cwd ({users[0]})")
    links = _links_into(path, LINK_DIRS if link_dirs is None else link_dirs)
    if links:
        raise Refused(f"{path} is where {links[0]} points; an installed copy runs from it")
    top = _git(path, "rev-parse", "--show-toplevel").strip()
    if os.path.realpath(top) != path:
        raise Refused(f"{path} is inside a worktree, not its root ({top})")
    git_dir = _git(path, "rev-parse", "--absolute-git-dir").strip()
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if os.path.realpath(git_dir) == os.path.realpath(common):
        raise Refused(f"{path} is a main checkout, not a linked worktree")
    if os.path.exists(os.path.join(git_dir, "locked")):
        raise Refused(f"{path} is locked")
    busy = [n for n in IN_PROGRESS if os.path.exists(os.path.join(git_dir, n))]
    if busy:
        raise Refused(f"{path} has an operation in progress ({busy[0]})")
    if _git(path, "status", "--porcelain", "--untracked-files=no").strip():
        raise Refused(f"{path} has uncommitted changes to tracked files")
    head = _git(path, "rev-parse", "HEAD").strip()
    branch = _git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip() or None
    if not _git(path, "for-each-ref", "--count=1", "--contains", head, "refs/remotes").strip():
        raise Refused(f"{path}: HEAD {head[:12]} is on no remote-tracking ref "
                      "(not pushed or merged)")
    tips = sorted(set(_git(path, "reflog", "show", "--format=%H", "HEAD",
                           check=False).split()))
    # Only the worktree's HEAD reflog dies with it; branch reflogs live in the common dir.
    lost = set()
    if tips:
        lost = set(_git(path, "rev-list", *tips, "--not", "--branches", "--tags",
                        "--remotes").split())
    untracked = [p for p in _git(path, "ls-files", "--others", "--exclude-standard",
                                 "-z").split("\0") if p]
    return {"path": path, "common": common, "head": head, "branch": branch,
            "unreachable": [t for t in tips if t in lost], "untracked": untracked}


def describe(info):
    return (f"{info['path']} ({info['branch'] or 'detached'} @ {info['head'][:12]}"
            + (f", tags {len(info['unreachable'])} unreachable" if info["unreachable"] else "")
            + (f", copies {len(info['untracked'])} untracked" if info["untracked"] else "")
            + ")")


def remove(path, other_cwds=(), save_root=None, now=None, link_dirs=None):
    """Re-check, tag, copy out, remove. Returns the outcome for the close event; a refusal
    at any step before the remove leaves the worktree in place."""
    try:
        info = check(path, other_cwds, link_dirs)
        name = os.path.basename(info["path"])
        tags = []
        for sha in info["unreachable"]:
            tag = f"wt-removed/{name}/{sha[:12]}"
            _git(info["path"], "tag", "-f", tag, sha)
            tags.append(tag)
        saved = None
        if info["untracked"]:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
            saved = os.path.join(save_root or SAVE_ROOT, f"{name}-{stamp}")
            for rel in info["untracked"]:
                dest = os.path.join(saved, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(os.path.join(info["path"], rel), dest, follow_symlinks=False)
        if _inside(os.getcwd(), info["path"]):
            os.chdir(os.path.expanduser("~"))
        # --force here only discards untracked and ignored files: tracked changes were
        # refused by check() and untracked files are copied. A locked worktree still refuses.
        out = subprocess.run(["git", "--git-dir", info["common"], "worktree", "remove",
                              "--force", info["path"]], capture_output=True, text=True)
        if out.returncode:
            raise Refused(f"git worktree remove failed: {out.stderr.strip()}")
    except (Refused, OSError) as err:
        return {"path": path, "removed": False, "reason": str(err)}
    return {"path": info["path"], "removed": True, "branch": info["branch"],
            "head": info["head"], "tags": tags, "untracked_saved": saved}
