"""Health of the MEMORY.md index and the store it routes to.

Three failures this catches, all of them silent: the index grows
past the truncation budget and its tail stops loading; a [[link]] points at a
slug that was never written or was renamed; an entry file exists that the index
never routes to, so nothing will ever recall it.

The budget is the index's own stated ceiling, 24.4 KiB.

Parsing note: code spans and fences are stripped before the [[link]] scan.
TOML array-of-table syntax in a doc body -- `[[panes]]`, `[[events]]` -- is
otherwise indistinguishable from a wiki link and shows up as dangling.
"""

import os
import re

BUDGET = int(24.4 * 1024)
DEFAULT_STORE = os.environ.get(
    "HERDR_DASH_MEMORY_DIR", "~/.claude/projects/-home-me-code-myrepo/memory")

_FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.S | re.M)
_SPAN = re.compile(r"`[^`\n]*`")
_WIKI = re.compile(r"\[\[([^\]\[\n|]+?)\]\]")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,}$")
_MDLINK = re.compile(r"\]\(([^)\s]+\.md)\)")


def strip_code(text):
    return _SPAN.sub(" ", _FENCE.sub(" ", text))


def _slugs_in(text):
    return {name.strip() for name in _WIKI.findall(text)}


def scan(store=None):
    root = os.path.expanduser(store or DEFAULT_STORE)
    index_path = os.path.join(root, "MEMORY.md")
    archive_root = os.path.join(root, "archive")

    try:
        with open(index_path, encoding="utf-8") as handle:
            index_text = handle.read()
    except OSError as err:
        return {"error": f"no index: {err}", "root": root}

    live = sorted(name[:-3] for name in os.listdir(root)
                  if name.endswith(".md") and "MEMORY.md" != name)
    try:
        archived = sorted(name[:-3] for name in os.listdir(archive_root)
                          if name.endswith(".md") and "INDEX.md" != name)
    except OSError:
        archived = []
    known = set(live) | set(archived)

    # The index routes by three spellings: a backticked bare slug, a .md link
    # target, and a wiki link.
    index_body = _FENCE.sub(" ", index_text)
    referenced = set()
    for token in re.findall(r"`([^`\n]+)`", index_body):
        if _SLUG.match(token.strip()):
            referenced.add(token.strip())
    for target in _MDLINK.findall(index_body):
        referenced.add(os.path.basename(target)[:-3])
    referenced |= _slugs_in(index_body)

    dangling, links_total, by_entry, distinct = {}, 0, {}, set()
    for name in live:
        try:
            with open(os.path.join(root, f"{name}.md"), encoding="utf-8") as handle:
                body = strip_code(handle.read())
        except OSError:
            continue
        links = [link.strip() for link in _WIKI.findall(body)]
        links_total += len(links)
        distinct.update(links)
        missing = sorted({link for link in links if link not in known})
        by_entry[name] = len(links)
        for link in missing:
            dangling.setdefault(link, []).append(name)

    size = len(index_text.encode("utf-8"))
    return {
        "root": root,
        "index_bytes": size,
        "budget": BUDGET,
        "fill": size / BUDGET,
        "over": max(0, size - BUDGET),
        "live": len(live),
        "archived": len(archived),
        "links": links_total,
        "links_distinct": len(distinct),
        "dangling": dangling,
        "dangling_count": sum(len(v) for v in dangling.values()),
        "unreferenced": sorted(set(live) - referenced),
        "referenced": len(referenced & set(live)),
        "busiest": sorted(by_entry.items(), key=lambda kv: -kv[1])[:5],
    }
