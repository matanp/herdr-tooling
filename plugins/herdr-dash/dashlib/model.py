"""Collect every section's data into one dict.

Sections that can fail independently do: a dead herdr server must not blank the
memory and index sections, which need no server at all. Each slot holds either
its data or an error string, and the renderer prints whichever it finds.
"""

import concurrent.futures

from . import agents as agents_mod
from . import herdr as herdr_mod
from . import keys as keys_mod
from . import memdoc
from . import notes as notes_mod
from . import procmem
from . import worktrees as worktrees_mod
from . import tabs as tabs_mod


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as err:  # a section failing is a section, not the pane
        return f"{type(err).__name__}: {err}"


def _herdr_side():
    """Snapshot, agents and the pane->workspace join, which share one server."""
    try:
        snap = herdr_mod.snapshot()
    except Exception as err:
        # panes_known gates the orphan band. Without the guard an unreachable
        # server makes every live pane look orphaned -- the loudest possible
        # wrong answer -- so the band must fail closed, not open.
        return {"agents": f"{type(err).__name__}: {err}", "pane_workspace": {},
                "terminal_titles": {}, "terminals": {}, "panes_known": False}
    panes, terminals = herdr_mod.pane_index(snap)
    spaces = herdr_mod.workspace_labels(snap)
    pane_workspace = {pane_id: spaces.get(pane.get("workspace_id"), "")
                      for pane_id, pane in panes.items()}
    try:
        rows = agents_mod.rows(terminals)
    except Exception as err:
        rows = f"{type(err).__name__}: {err}"
    titles = {}
    if not isinstance(rows, str):
        titles = {row["terminal_id"]: row.get("title") or row.get("pane")
                  for row in rows if row.get("terminal_id")}
    return {"agents": rows, "pane_workspace": pane_workspace,
            "terminal_titles": titles, "terminals": terminals,
            "panes_known": True}


def collect(sections=None):
    wanted = set(sections or ("tabs", "memory", "worktrees", "index", "agents", "keys",
                              "notes"))
    model = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {}
        if "memory" in wanted:
            jobs["memory"] = pool.submit(_safe, procmem.collect)
        if "index" in wanted:
            jobs["index"] = pool.submit(_safe, memdoc.scan)
        if "keys" in wanted:
            jobs["keys"] = pool.submit(_safe, keys_mod.sheet)
        if "worktrees" in wanted:
            jobs["worktrees"] = pool.submit(_safe, worktrees_mod.scan)
        if "tabs" in wanted:
            jobs["tabs"] = pool.submit(_safe, tabs_mod.view)
        # Agents needs the snapshot, and the memory section wants its workspace
        # labels, so it runs whenever either is asked for.
        if {"agents", "memory", "notes"} & wanted:
            jobs["_herdr"] = pool.submit(_herdr_side)
        for name, job in jobs.items():
            value = job.result()
            if "_herdr" == name:
                model.update(value)
            else:
                model[name] = None if isinstance(value, str) else value
                if isinstance(value, str):
                    model[f"{name}_error"] = value
    model["notes"] = notes_mod.read()
    return model
