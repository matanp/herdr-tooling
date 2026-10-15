"""Section layout, shared by the curses pane and the plain-text dump.

One renderer, two entrypoints: the `tab` pane that stays put and the `popup`
bound to a prefix key show the identical view, so a section is written once.

Every figure carries its source in the line that prints it. Machine memory has
three incompatible sources and an unlabelled number from the
wrong one is the specific way this section lies.
"""

import datetime
import os
import time

from . import agents as agents_mod
from . import keys as keys_mod
from . import notes as notes_mod
from . import reports as reports_mod

GIB = 1024 ** 3
SECTIONS = ("reports", "usage", "tabs", "memory", "worktrees", "index", "agents", "keys", "notes")
TITLES = {"reports": "REPORTS", "usage": "USAGE", "tabs": "TABS", "memory": "MEMORY", "worktrees": "WORKTREES",
          "index": "MEMORY.md INDEX", "agents": "AGENTS", "keys": "PREFIX",
          "notes": "NOTES"}


class Line:
    __slots__ = ("text", "style", "target", "section")

    def __init__(self, text="", style="normal", target=None):
        self.text = text
        self.style = style
        self.target = target
        self.section = None


def gib(value):
    """A pane holding 400 MB reads as 0.4G and an orphan at 40 MB as 0.0G."""
    if value < GIB:
        mib = value / (1024 ** 2)
        return f"{mib:.0f}M" if mib >= 0.5 else "<1M"
    return f"{value / GIB:.1f}G"


def _bar(fraction, width):
    filled = max(0, min(width, round(fraction * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _clip(text, width):
    if len(text) <= width:
        return text
    return text[:max(0, width - 1)] + "…"


def _header(name, right, width, number=None):
    title = TITLES[name] if number is None else f"{number} {TITLES[name]}"
    gap = width - len(title) - len(right) - 2
    if gap < 1:
        return Line(_clip(title, width), "head")
    return Line(f"{title} {' ' * gap} {right}", "head")


def memory(model, width, out):
    data = model.get("memory")
    if not data:
        out.append(Line("  collecting…", "dim"))
        return
    info, cgroup, procs = data["meminfo"], data["cgroup"], data["procs"]
    metric = procs.get("metric", "PSS")
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    swap_used = info.get("SwapTotal", 0) - info.get("SwapFree", 0)

    # Summed RSS counts a shared page once per sharer, so without PSS there is
    # no honest machine-wide total of own processes; keep only the pane figure.
    own = f"{gib(procs['own_total'])} own PSS · " if "PSS" == metric else ""
    out.append(_header(
        "memory",
                       f"{own}{gib(procs['pane_total'])} in panes · "
                       f"{procs['elapsed_s']:.1f}s", width, model.get("_number")))
    tight = available < total * 0.12
    out.append(Line(
        f"  machine   {gib(total)} total · {gib(available)} available · "
        f"swap {gib(swap_used)}/{gib(info.get('SwapTotal', 0))} used"
        f"{'' if not tight else '   LOW'}   [{'meminfo' if 'PSS' == metric else 'vm_stat'}]",
        "crit" if tight else "normal"))
    for disk in data.get("disks") or []:
        share = disk["used_fraction"]
        out.append(Line(
            f"  disk      {gib(disk['total'])} on {disk['path']} · "
            f"{share * 100:.0f}% used · {gib(disk['free'])} free"
            f"{'   LOW' if share > 0.9 else ''}   [statvfs]",
            "crit" if share > 0.9 else "warn" if share > 0.8 else "normal"))
    slice_bits = " · ".join(f"{name} {gib(value)}"
                                for name, value in cgroup["slices"].items() if value)
    if slice_bits:
        out.append(Line(_clip(f"  cgroup    {slice_bits}   [cgroup]", width), "normal"))
    brief = bool(model.get("_brief"))
    services = " · ".join(
        f"{name.replace('.service', '').replace('.scope', '')[:22]} {gib(value)}"
        for name, value in cgroup["services"][:4])
    if services:
        out.append(Line(_clip(f"  services  {services}   [cgroup]", width), "dim"))

    labels = model.get("pane_workspace") or {}
    live = set(labels)
    orphans = []
    # Only trustworthy when the snapshot actually came back; see model.py.
    if model.get("panes_known"):
        orphans = sorted(((pane_id, bucket)
                          for pane_id, bucket in procs["panes"].items()
                          if pane_id not in live),
                         key=lambda item: -item[1]["bytes"])
    if orphans:
        total = sum(bucket["bytes"] for _, bucket in orphans)
        out.append(Line())
        out.append(Line(
            f"  orphaned  {len(orphans)} pane(s) hold processes herdr no longer "
            f"has — {gib(total)}, safe to kill · x kills", "crit",
            ("orphan", [pane_id for pane_id, _ in orphans])))
        for pane_id, bucket in orphans[:4]:
            top = bucket["procs"][0]["cmd"] if bucket["procs"] else ""
            out.append(Line(_clip(
                f"    {gib(bucket['bytes']):>7}  {pane_id:<9} "
                f"{len(bucket['procs']):>3}p  {top}", width), "warn",
                ("orphan", [pane_id])))
        if len(orphans) > 4:
            out.append(Line(
                f"    {len(orphans) - 4} more — x on the line above kills all",
                "dim"))

    out.append(Line())
    out.append(Line(f"  by pane   {metric}, own processes only", "sub"))
    ranked = [item for item in
              sorted(procs["panes"].items(), key=lambda item: -item[1]["bytes"])
              if item[0] in live or not model.get("panes_known")]
    top_n = 6 if brief else 8
    for pane_id, bucket in ranked[:top_n]:
        top = bucket["procs"][0]["cmd"] if bucket["procs"] else ""
        space = labels.get(pane_id, "")
        out.append(Line(_clip(
            f"    {gib(bucket['bytes']):>7}  {pane_id:<9} {space[:18]:<18} "
            f"{os.path.basename(top.split()[0]) if top else '':<14} "
            f"{len(bucket['procs'])}p", width),
            "warn" if bucket["bytes"] > 4 * GIB else "normal"))
    if len(ranked) > top_n:
        rest = sum(bucket["bytes"] for _, bucket in ranked[top_n:])
        out.append(Line(
            f"    {gib(rest):>7}  {len(ranked) - top_n} more panes", "dim"))

    # The per-process RSS band says what the cgroup services line already says,
    # from the source that double-counts. A glance keeps the honest one.
    if procs["foreign"] and not brief:
        out.append(Line())
        out.append(Line(
            "  other users   RSS — shown per process, never summed: shared "
            "pages are counted once per sharer", "sub"))
        for record in procs["foreign"][:3]:
            out.append(Line(_clip(
                f"    {gib(record['rss']):>7}  uid {record['uid']:<6} "
                f"{record['comm']}", width), "dim"))
    if procs["degraded"]:
        out.append(Line(
            f"    {procs['degraded']} own process(es) fell back to RSS "
            f"(smaps_rollup unreadable)", "dim"))


def worktrees(model, width, out):
    data = model.get("worktrees")
    if not data:
        out.append(Line("  collecting…", "dim"))
        return
    dormant, detached = data["dormant"], data["detached"]
    out.append(_header(
        "worktrees",
        f"{data['total']} · {len(dormant)} with no open workspace · "
        f"{len(detached)} detached", width, model.get("_number")))
    if detached:
        out.append(Line(_clip(
            "  detached  " + ", ".join(detached), width), "warn"))
    if data["prunable"]:
        out.append(Line(_clip(
            "  prunable  " + ", ".join(data["prunable"]), width), "crit"))
    if not model.get("_brief"):
        for name in dormant:
            if name not in detached:
                out.append(Line(f"    {name}", "dim"))
    elif dormant:
        out.append(Line(f"  dormant   {len(dormant)} — open the tab to list them",
                        "dim"))


def index(model, width, out):
    data = model.get("index")
    if not data:
        out.append(Line("  collecting…", "dim"))
        return
    if data.get("error"):
        out.append(Line(f"  {data['error']}", "crit"))
        return
    fill = data["fill"]
    out.append(_header(
        "index",
                       f"{data['index_bytes']:,} B · {fill * 100:.0f}% of "
                       f"{data['budget'] / 1024:.1f} KiB budget", width, model.get("_number")))
    style = "crit" if fill >= 1 else "warn" if fill > 0.9 else "ok"
    headroom = data["budget"] - data["index_bytes"]
    tail = (f"headroom {headroom:,} B" if headroom > 0
            else f"OVER by {-headroom:,} B — the tail is not loading")
    out.append(Line(f"  {_bar(fill, 34)} {fill * 100:3.0f}%   {tail}", style))
    out.append(Line(
        f"  {data['live']} live · {data['archived']} archived · "
        f"{data['links_distinct']} distinct link targets "
        f"({data['links']} refs)"
        + (f" · {len(data['dangling'])} dangling"
           if model.get("_brief") and data["dangling"] else ""), "normal"))
    if model.get("_brief"):
        return

    if data["dangling"]:
        out.append(Line(f"  {len(data['dangling'])} dangling [[links]]", "warn"))
        for slug in sorted(data["dangling"])[:6]:
            sources = data["dangling"][slug]
            out.append(Line(_clip(
                f"    {slug:<44} ← {', '.join(sources[:2])}"
                f"{'…' if len(sources) > 2 else ''}", width), "dim"))
    else:
        out.append(Line("  no dangling links", "ok"))

    if data["unreferenced"]:
        out.append(Line(
            f"  {len(data['unreferenced'])} entries the index never routes to",
            "warn"))
        for slug in data["unreferenced"][:4]:
            out.append(Line(f"    {slug}", "dim"))
    else:
        out.append(Line("  every live entry is routed from the index", "ok"))


def _branch(name):
    if not name:
        return ""
    parts = name.split("/")
    return _clip(parts[-1], 20)


def agents(model, width, out):
    data = model.get("agents")
    if data is None:
        out.append(Line("  collecting…", "dim"))
        return
    if isinstance(data, str):
        out.append(Line(f"  {data}", "crit"))
        return
    counts = agents_mod.tally(data)
    summary = " · ".join(f"{counts[name]} {name}"
                             for name in ("blocked", "working", "idle", "done",
                                          "unknown") if counts.get(name))
    out.append(_header("agents", summary or "none", width,
                       model.get("_number")))
    noted = model.get("notes") or {}
    for space, rows in agents_mod.by_workspace(data):
        out.append(Line(f"  {space}", "sub"))
        for row in rows:
            status = row.get("status") or "unknown"
            held = row.get("held_s")
            when = f"≥{int(held // 60)}m" if held else "new"
            mark = "✎" if noted.get(row.get("terminal_id")) else " "
            text = (f"   {agents_mod.GLYPH.get(status, '?')}{mark} "
                    f"{(row.get('pane') or ''):<8} "
                    f"{_clip(row.get('title') or '', max(20, width - 46)):<{max(20, width - 46)}} "
                    f"{when:>6}  {_branch(row.get('branch'))}")
            out.append(Line(_clip(text, width),
                            "crit" if "blocked" == status else
                            "ok" if "working" == status else "dim",
                            ("agent", row)))


def keys(model, width, out):
    data = model.get("keys")
    if not data:
        out.append(Line("  collecting…", "dim"))
        return
    out.append(_header("keys", data["prefix"] if data["prefix_label"] == data["prefix"]
                       else f"{data['prefix_label']} = {data['prefix']}",
                       width, model.get("_number")))
    if data.get("error"):
        out.append(Line(f"  config unreadable: {data['error']}", "crit"))
    out.append(Line(f"  custom ({len(data['custom'])})", "sub"))
    for entry in data["custom"]:
        binding = keys_mod.pretty(entry["key"], data["prefix_label"])
        note = entry["note"] or os.path.basename(entry["command"].split()[0] if
                                                 entry["command"] else "")
        out.append(Line(_clip(
            f"    {binding:<20} {note:<26} {entry['command']}", width), "normal"))
    if data["overrides"]:
        out.append(Line(f"  overridden built-ins", "sub"))
        for action, binding in data["overrides"]:
            out.append(Line(
                f"    {keys_mod.pretty(binding, data['prefix_label']):<20} {action}",
                "normal"))
    out.append(Line(f"  built-in ({len(data['defaults'])})", "sub"))
    cell = 38
    columns = max(1, (width - 4) // cell)
    row = []
    for entry in data["defaults"]:
        binding = _clip(keys_mod.pretty(entry["key"], data["prefix_label"]), 19)
        row.append(f"{binding:<20}{entry['action'][:17]:<18}")
        if len(row) == columns:
            out.append(Line(_clip("    " + "".join(row), width), "dim"))
            row = []
    if row:
        out.append(Line(_clip("    " + "".join(row), width), "dim"))


def notes(model, width, out):
    sections = model.get("notes") or {}
    labels = model.get("terminal_titles") or {}
    flat = notes_mod.flatten(sections, labels)
    home = os.path.expanduser("~")
    out.append(_header("notes", notes_mod.path().replace(home, "~"), width,
                       model.get("_number")))
    if not flat:
        out.append(Line("  none yet — n to add one", "dim"))
    current = None
    for key, label, position, text in flat:
        if key != current:
            current = key
            suffix = "" if key == notes_mod.GLOBAL else f"  ({key[:16]})"
            out.append(Line(f"  {label}{suffix}", "sub"))
        out.append(Line(_clip(f"    {text}", width), "normal",
                        ("note", key, position)))
    out.append(Line("  n new · N attach to selected agent · d delete "
                    "· e $EDITOR", "dim"))


def tabs(model, width, out):
    data = model.get("tabs")
    if not data:
        out.append(Line("  " + (model.get("tabs_error") or "collecting…"),
                        "crit" if model.get("tabs_error") else "dim"))
        return
    counts = data["counts"]
    summary = " · ".join(f"{counts.get(v, 0)} {label}" for v, label in
                         (("CLOSE", "closable"), ("GLANCE", "glance"), ("KEEP", "keep")))
    out.append(_header("tabs", f"{summary} · {data['total_closed']} in the ledger",
                       width, model.get("_number")))
    if data.get("error"):
        out.append(Line(_clip(f"  verdicts stale: {data['error']}", width), "warn"))
    style = "warn" if counts.get("CLOSE", 0) >= 20 else "normal"
    out.append(Line("  herdr-sort stale · close --safe · log [words] · reopen <#n>", style))
    shown = data["recent"][:3] if model.get("_brief") else data["recent"]
    if shown:
        out.append(Line("  recently closed", "sub"))
    for row in shown:
        out.append(Line(_clip(f"   #{row['n']:<2} {_clip(row['title'], 48):<48} "
                              f"{row['workspace']}", width), "normal"))
        if row.get("next") and not model.get("_brief"):
            out.append(Line(_clip(f"       {row['next']}", width), "dim"))


def _report_tag(item):
    rep = item["report"]
    if item["blocked"]:
        return "blocked", "crit"
    if item["stalled"]:
        return "stalled", "warn"
    if item["told_yes"] and "offer" == rep["kind"]:
        return "not shut", "warn"
    if "offer" == rep["kind"]:
        return rep["disposition"] or "close", "ok" if reports_mod.herdr_close.clean_offer(rep) \
            else "warn"
    return rep["kind"], "warn"


def _report_detail(item):
    rep = item["report"]
    fails = sum("fail" == r.lower() for r, _ in rep["items"])
    if "reply" == rep["kind"]:
        lines = [l for l in (item["reply"] or "").strip().splitlines() if l.strip()]
        text = lines[-1].strip() if lines else "(no reply)"
    elif "keep" == rep["kind"]:
        text = "wants: " + (rep["ask"] or (rep["items"][0][1] if rep["items"] else "?"))
    else:
        text = "next: " + (rep["next"] or "?")
    if rep.get("worktree"):
        text += f"  · removes worktree {os.path.basename(rep['worktree'].rstrip('/'))}"
    if rep["side"]:
        text += "  · also asks something"
    return text + (f"  · {fails} FAIL" if fails else "")


def reports(model, width, out):
    data = model.get("reports")
    if not isinstance(data, dict):
        out.append(_header("reports", "batch /close", width, model.get("_number")))
        out.append(Line(f"  {data}" if data else "  collecting…", "crit" if data else "dim"))
        return
    items, checking = data["items"], data["checking"]
    tally = {}
    for item in items:
        tally[item["report"]["kind"]] = tally.get(item["report"]["kind"], 0) + 1
    right = " · ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in tally.items())
    right = " · ".join(p for p in (right, f"{len(checking)} working" if checking else "") if p)
    out.append(_header("reports", right or "nothing waiting", width, model.get("_number")))
    for item in items[:8] if model.get("_brief") else items:
        tag, style = _report_tag(item)
        out.append(Line(_clip(f"  {tag:<8} {_clip(item['title'], 34):<34}  "
                              f"{_report_detail(item)}", width), style,
                        target=("report", item)))
    if model.get("_brief") and len(items) > 8:
        out.append(Line(f"  … {len(items) - 8} more (enter walks them all)", "dim"))
    if checking:
        out.append(Line(_clip("  working: " + ", ".join(_clip(c["title"], 24)
                                                     for c in checking), width), "dim"))
    clean = sum(reports_mod.herdr_close.clean_offer(i["report"]) and not i["told_yes"]
                for i in items)
    if items:
        out.append(Line("  enter answers them one by one"
                        + (f" · Y says yes to the {clean} clean offers" if clean > 1 else ""),
                        "dim"))


def _tokens(value):
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return str(value)


def _ago(seconds):
    if seconds < 90:
        return f"{int(max(0, seconds))}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h" if seconds < 172800 else f"{int(seconds // 86400)}d"


def usage(model, width, out):
    data = model.get("usage")
    if not data or data.get("pending"):
        out.append(_header("usage", "ccusage", width, model.get("_number")))
        out.append(Line("  " + (model.get("usage_error") or "collecting…"),
                        "crit" if model.get("usage_error") else "dim"))
        return
    totals = data.get("totals")
    age = time.time() - data.get("fetched_at", 0)
    right = f"ccusage · all agents · {_ago(age)} ago"
    out.append(_header("usage", right, width, model.get("_number")))
    if data.get("error"):
        out.append(Line(_clip(f"  refresh failed: {data['error']}", width),
                        "warn" if totals else "crit"))
    if not totals:
        return
    if data.get("warning"):
        out.append(Line(_clip(f"  cost understated: {data['warning']}", width), "warn"))
    if data.get("date") != datetime.date.today().isoformat():
        out.append(Line(f"  figures are from {data.get('date')} — refreshing", "warn"))
    labels = (("today", "today"), ("week", "this week"), ("month", "this month"))
    if model.get("_brief"):
        out.append(Line("  " + " · ".join(
            f"{label} ${totals[key]['cost']:,.2f}" for key, label in labels),
            "normal"))
        return
    for key, label in labels:
        row = totals[key]
        out.append(Line(
            f"  {label:<11} {'$' + format(row['cost'], ',.2f'):>10}   {_tokens(row['tokens']):>7} tok   "
            f"{row['days']}d", "normal"))
    models = data.get("models_today") or []
    if models:
        out.append(Line(_clip("  today on   " + ", ".join(models), width), "dim"))


RENDERERS = {"reports": reports, "usage": usage, "tabs": tabs, "memory": memory, "worktrees": worktrees, "index": index,
             "agents": agents, "keys": keys, "notes": notes}


def build(model, width, order=SECTIONS, collapsed=(), brief=()):
    lines = []
    for position, name in enumerate(order, 1):
        if name in collapsed:
            start = len(lines)
            lines.append(Line(f"{position} {TITLES[name]}   \u2500 collapsed", "head"))
            lines.append(Line())
            for line in lines[start:]:
                line.section = name
            continue
        start = len(lines)
        model["_number"] = position
        model["_brief"] = name in brief
        RENDERERS[name](model, width, lines)
        lines.append(Line())
        for line in lines[start:]:
            line.section = name
    return lines


def plain(model, width=100, order=SECTIONS, brief=()):
    return "\n".join(line.text
                     for line in build(model, width, order, brief=brief))
