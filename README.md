# herdr-tooling

Personal scripts, plugins and config around herdr, the agent-aware terminal multiplexer. Not
herdr itself. Built against herdr 0.8.0, runs on 0.9.3; Python 3.8+, stdlib only except where noted.

## Install

```sh
git clone https://github.com/matanp/herdr-tooling ~/.local/src/herdr-tooling
~/.local/src/herdr-tooling/install            # first run writes tooling.toml and stops
$EDITOR ~/.config/herdr/tooling.toml          # pick components, set [dash] memory-dir
~/.local/src/herdr-tooling/install --dry-run  # see what it would do
~/.local/src/herdr-tooling/install
```

Moving a machine set up the old way, by copying files by hand? Follow
[`MIGRATING-linux.md`](MIGRATING-linux.md) instead.

Each machine has two files of its own under `~/.config/herdr/`:

| File | Holds |
|---|---|
| `tooling.toml` | Which components run here, their settings, key remaps, `[[brief]]` tables. Example: `config/tooling.example.toml` |
| `config.local.toml` | This machine's own herdr settings: prefix, theme, UI. Seeded from an existing `config.toml` on the first run, else `config/local.example.toml` |

`install` symlinks each enabled component into place, removes the links of
disabled ones, and writes `config.toml` as `config.local.toml` followed by
the `config/keys/*.toml` block of each enabled component. It checks the result
with `herdr config check`, keeps the previous file as `config.toml.bak-*`, and
reloads the server. Don't edit `config.toml` directly: the next run overwrites it.
Because everything is a symlink, `git pull` updates the tools. Re-run `install`
after editing either file.

| Component | Installs | Key | Platforms |
|---|---|---|---|
| `keys` | `bin/herdr-keys` → `~/.local/bin`, plugin `herdr-keys`; wraps every binding | — | linux (plugin) |
| `compose` | `bin/herdr-compose` → `~/.local/bin` | `prefix+i` | linux, macos (needs nvim) |
| `ccnav` | `bin/ccnav-herdr` → `~/.local/bin` | `prefix+u/d/f/a`, `prefix+shift+k/j` | linux (`/proc`); needs compose |
| `sessions` | `bin/herdr-sessions` → `~/.local/bin` | — | linux (`/proc`) |
| `sort` | `bin/herdr-sort`, plugin, `/close` skill → `~/.claude/skills`, seeds `sort-rules.toml` | — | linux; untested on macos |
| `dash` | plugin `herdr-dash` | `prefix+m`, `prefix+shift+m` | linux, macos (RSS by pane instead of PSS/cgroup) |
| `prsessions` | key only; the plugin is not in this repo | `prefix+t` | — |
| `viewer` | plugin `viewer` | `prefix+shift+v`, `prefix+y` | linux, macos (needs `rich`) |
| `space-scope` | plugin + `~/.local/bin/herdr-space-scope` | `prefix+shift+s` | linux, macos |
| `brief-hook` | `~/.claude/hooks/herdr-project-brief.py`, registered in `~/.claude/settings.json` | — | linux, macos |
| `tabname` | `bin/herdr-tabname` → `~/.local/bin`, its `ensure` registered as a SessionStart hook | — | linux, macos |

Plugins are linked from `~/.local/share/herdr-plugins/<name>`. Shared Python
(`toml_min`, `tooling`) lives in `lib/`, and the tools find it by resolving their
own symlinks, so the checkout must be installed with `install`, not copied.
Tool state and logs go to `~/.local/state/herdr-*/`.

## Tools (`bin/`)

- **`herdr-sessions`** — table of open agent sessions; `--watch`, `--json`, `--status blocked`.
- **`herdr-sort`** — routes tabs into workspaces by session title (`sort-rules.toml`);
  KEEP / GLANCE / CLOSE ratings, close with a reopenable ledger.
  - Every close takes a disposition: `done`, `filed`, `merged`, `dropped` (needs `--note`),
    `trivial`. `close --as <d> [--ref GROWTH-n] [--note ...] <tabs>`; bare `close` refuses.
  - `done` / `filed` / `merged` go to Linear first (comment on a referenced issue, else a new
    issue in the suggested project); a Linear failure closes nothing.
  - `cull` walks open tabs: one confirmation for trivial and verified-done tabs, then one card
    per tab or group with a Sonnet draft, checked open items and a single-key decision.
    `popup` opens it as a herdr popup.
  - Session records: one live record per open Claude or Codex session in
    `~/.local/state/herdr-sort/records.json`, holding a recap (what the session is for, what
    got done, where it stands), what it needs from you, and its next step. The daemon
    refreshes a session once its agent is idle and its transcript has been quiet for a
    minute, if it grew since the last recap; `herdr-sort records` runs the same sweep on
    demand (`--force`, `--audit`). Recaps use `haiku` (`HERDR_RECAP_MODEL`); the audit tier
    is cull's Sonnet close note, run when cull asks or after 6h idle, and reused while the
    transcript is unchanged. Closing a tab freezes its record into the close event. The dash,
    cull cards, `log`, `loops` and `reopen` read records and never call a model.
  - Cron backstop for when the daemon is down or misses an event (cron's `PATH` must reach `claude`):
    `*/15 * * * * PATH=$HOME/.local/bin:/usr/bin:/bin herdr-sort records --quiet >> ~/.local/state/herdr-sort/records.log 2>&1`
  - Before the cards, `cull` offers to send `/close` to the closeable Claude tabs you pick
    (CLOSE verdict, idle, not trivial), marked as a batch so the skill keeps its report short.
    They are answered from herdr-dash's REPORTS section, so cull can be quit at any point.
- **`herdr-compose`** — `prefix+i`: nvim split under the agent for the next prompt,
  `Ctrl-S` sends. `prefix+shift+k/j` scroll the agent.
- **`herdr-keys`** — keybinding usage. `stats [--since 7d]`: per-binding counts, never-used
  custom bindings, and built-in actions (tab/space/pane switches, splits, closes, zoom).
  - Custom bindings are exact: while `keys` is on, `install` writes each `[[keys.command]]`
    as `herdr-keys run KEY -- CMD`, with the key after any `[keys]` remap. With it off the
    bindings run unwrapped and uncounted.
  - Built-ins are inferred from the event stream by the `herdr-keys` plugin's startup
    watcher, so mouse and API calls count too. Log in `~/.local/state/herdr-keys/keys.jsonl`.
  - Culling is counted from herdr-sort's close ledger; `cull start` / `cull closed` lines in
    `sort.log` only mark session bounds. `reset` clears the log and starts every section over.
- **`herdr-tabname`** — mirrors each pane's live session title into its tab label every 3s,
  shortened to 20 characters or fewer by Haiku (one call per distinct title, cached). Only touches
  bare-number labels or ones it wrote; type a name to keep it, a number to hand it back.
  `plan`, `once`, `status`, `stop`; `ensure` starts the daemon if none is running.
- **`ccnav-herdr`** — turn navigation, `prefix+u/d/f/a` (prev / next / newest / bottom),
  routed via `herdr-compose nav`.

## Plugins (`plugins/`)

- **`herdr-dash`** — `prefix+m`: batch `/close` reports, ccusage spend, agents, memory by
  pane, `MEMORY.md` health, prefix cheat-sheet, notes. The memory directory is
  `[dash] memory-dir` in `tooling.toml` (`HERDR_DASH_MEMORY_DIR` overrides it). The
  reports and tabs sections appear only where `sort` is on, agents only where `sessions` is.
  - REPORTS lists every session a batch `/close` is talking to, plus tabs ending on an
    unanswered report. Enter opens a fixed-position card, topped by the session record's recap
    and its age (first ask plus Claude Code's last recap before one is built): what you type goes to that
    session, bare Enter moves on, `/view` `/jump` `/leave` `/quit`. `Y` says yes to
    every clean close offer. Flow state is `~/.local/state/herdr-sort/close-flow.json`,
    shared with cull through `plugins/herdr-sort/herdr_close.py`.
- **`herdr-sort`** — plugin wrapper, close ledger and cull core for `bin/herdr-sort`;
  the `cull` popup entrypoint and its action.
- **`space-scope`** — `prefix+shift+s`: toggles the Agents panel between the current space and all spaces.
  Uses `agent.view.set`; reapplied on server start.
- **`viewer`** — `prefix+shift+v` browse, `prefix+y` copy.
  - Single Python script (`view`) rendering with `rich`: `Markdown` for `.md`,
    `Syntax` for code.
  - File list from `git ls-files`; own subsequence fuzzy scorer, no fzf.
  - Enter pages into `less -R`; copy goes to the local clipboard via OSC 52 on `/dev/tty`.
  - Opened as a popup over the socket (`plugin.pane.open`), since the CLI has no
    popup placement; cwd comes from the focused pane.

## Claude Code hook

`herdr-project-brief.py` — on `SessionStart`, resolves the pane's herdr workspace and injects
that workspace's `[[brief]]` from `tooling.toml` (or, when there are none, its `brief` from
`sort-rules.toml`) as context, so an unqualified prompt resolves
to the right project. Silent on every failure path.

`herdr-tabname ensure >/dev/null 2>&1 || true`, as a second `SessionStart` command (registered
by the `tabname` component), keeps the tab-label daemon running across reboots and herdr
restarts, and hands it `ANTHROPIC_BASE_URL`.

## Claude Code skill

`/close` (installed with `sort`) — the in-session counterpart of `herdr-sort cull`. Checks the session's open items
live, pushes back while any are still the session's to finish, otherwise ends on a close report
and `herdr-sort close --as <d> --self` for this pane. User-invoked only.

## Keys

The prefix is `ctrl+b` (herdr's default). On the author's Linux setup, Caps Lock sends it
through a terminal-scoped Karabiner chord; `[dash] prefix-label = "Caps Lock"` names it so in
the cheat-sheet. Each component's bindings are in `config/keys/`. Remap or drop one per
machine with `[keys]` in `tooling.toml`.
