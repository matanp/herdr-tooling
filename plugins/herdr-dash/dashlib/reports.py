"""REPORTS: the sessions a batch /close is talking to, answered from the dash.

Parsing and flow state belong to herdr-sort's herdr_close, so the cull that sends /close
and the dash that answers it read one source; it is imported from the sibling plugin dir,
which exists both in the repo and in the install.
"""

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "..", "herdr-sort")))

import herdr_close  # noqa: E402


def collect(snap):
    panes = snap.get("panes", [])
    live = {(p.get("agent_session") or {}).get("value") for p in panes} - {None}
    items, checking = herdr_close.gather(panes, herdr_close.prune(live))
    return {"items": items, "checking": checking}
