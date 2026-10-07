---
name: close
description: Usher this session to a close — audit its open items live, push back if any are still this session's to finish, otherwise write the close report and close the herdr tab with a disposition.
argument-hint: "[done|filed|dropped|trivial] [--ref GROWTH-n] [anything the close should know]"
disable-model-invocation: true
---

The in-session counterpart of `herdr-sort cull`: same dispositions, same open-item kinds, same
bias. Cull reads a transcript tail cold; you have the whole session, so the bar is higher — every
open item is checked live, and a session with work left that it can still do does not close.

Your stance is a **gatekeeper**, not a scribe. The user typing `/close` is a request to *check*
whether this session is done, not an instruction to declare it done. Arguments are the user's
expected disposition and context; they inform the verdict, they do not decide it.

## 1. Inventory

List every **open item** the session could lose when the tab closes, typed with herdr's kinds:

- `uncommitted` — changes not committed, commits not pushed, a branch with no PR.
- `install_drift` — an installed copy (`~/.local/bin`, `~/.claude/...`, a symlink target) that may
  differ from its repo.
- `action_left` — anything promised, deferred or handed to the user: "after this I'll…", a
  follow-up PR, a reply owed on a review, a Linear ticket to update.
- `unverified` — a claim or a code path the session asserted but never ran: a fix with no spec
  run, a screenshot never looked at, a number never sanity-checked.
- `external_state` — background tasks and subagents, dev stacks, DBLab clones, worktrees, CI
  runs, deploys, tickets whose state the work should have moved.

Also scan for two things cull cannot see: the user's **last ask** (answered in full, or
partially?) and any **question the user asked that got no answer**.

Done when every turn of the session that touched files, git, GitHub, Linear or a long-running
process has been accounted for — not just the last few.

## 2. Check

Give every item a result — `pass`, `fail`, or `manual` — from a check run **now**, never from
memory of what happened earlier. Typical checks:

- `git -C <worktree> status --short` and `git -C <worktree> log @{u}..` for every worktree the
  session touched (they are rarely the main checkout).
- `ghapi pr <n>`, `ghapi ci <full-40-char-sha>`; `fetch_pr_review.sh <n>` when a bot review
  is in play — a STALE review is a `fail`.
- Linear issue state via the Linear MCP tools.
- `diff` / `cmp` between an installed copy and its repo source.
- Background tasks and subagents still running.
- `~/.claude/tools/docguard --branch <ref>` when the branch carries both docs and app code.

`manual` means no check fits; say what the user would have to look at.

## 3. Verdict

**Keep** — push back — when any of these hold:

- a `fail` or `unverified` item this session can resolve in the next few minutes;
- a background task, subagent or CI run this session is waiting on is still in flight;
- the last ask is partially answered, or a user question is unanswered;
- the arguments say `done` and a check says otherwise.

Pushing back means: name each blocking item with its check result, propose the concrete next
action for each, and stop. Write no close report and no docs yet. If the user then says to close
anyway, close as `filed` with those items carried in the report — not as `done`.

**Close** otherwise, with one disposition:

- `done` — every check passes and nothing is left that anyone must do.
- `filed` — real work remains but it is legitimately not this session's: waiting on review, CI
  beyond this session, a deploy, a decision only the user or a teammate can make.
- `dropped` — abandoned or superseded; needs a one-line reason.
- `trivial` — a one-off question; there was never anything to track.

Between `done` and `filed`, choose `filed`: a lost next step is the costly mistake.

## 4. Document what the disposition needs

- `done` / `trivial` / `dropped`: nothing beyond the close report.
- `filed`: the close report *is* the Linear content — `herdr-sort close` files it. Name the
  ticket if one exists so it is passed as `--ref`.
- A working doc the session already started that has gone stale: amend it per
  `working-docs-amend`. Start a new doc only if the user asks for one.
- Lessons about tools, skills or the user's preferences: recommend `/academia-get-better` in one
  line; do not run it inside `/close`.

## 5. Close report

End the turn with the report as your final message. `herdr-sort close` reads the final assistant
message in full when it writes the close note, so this message is what lands in the ledger and in
Linear — write it for the user reading it weeks from now.

```
**Verdict:** close as <disposition>   (or: **Verdict:** keep — <n> blocking)
**Outcome:** <one line, what the session achieved>
**Next step:** <the single most important thing left, or "none">
**Open items:**
- [pass|fail|manual] <kind> — <text> (<check and its detail>)
**Artifacts:** PRs #…, tickets GROWTH-…, branches, worktrees, docs
**Resume:** <the resume command for this session, if known>
```

Then give the close command, built from the verdict:

    herdr-sort close --as <disposition> [--ref <ticket>] [--note "<one line>"] --yes --self

and ask for a go-ahead. On a yes, run it as your **last tool call** — it closes this pane, and
the Linear write happens before the pane goes. `--self` is what lets it close the focused tab;
a pane id in its place is refused as "the tab you're in", with nothing written. The report must already be in an earlier turn for
`herdr-sort` to read it, which is why the command never runs in the same turn as the report.

When `HERDR_PANE_ID` is unset, the session is not in herdr: deliver the report and stop.
