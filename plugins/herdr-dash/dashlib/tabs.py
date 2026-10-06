"""Tab-hygiene section: herdr-sort's close verdicts and its ledger of closed sessions.

The ledger module lives with herdr-sort, which owns it; this only reads it.
"""

import os
import sys

sys.path.insert(0, os.path.expanduser("~/.local/share/herdr-plugins/herdr-sort"))

import herdr_ledger  # noqa: E402


def view():
    return herdr_ledger.dash_view(limit=8)
