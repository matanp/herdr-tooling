# herdr-tooling

Personal scripts, plugins and config around herdr, the agent-aware terminal multiplexer. Not
herdr itself. Built against herdr 0.8.0; Python 3.10, stdlib only except where noted.

## Layout and install paths

Nothing here installs itself. Copy or symlink each piece to the location herdr and the scripts expect:

| Repo path | Install to |
|---|---|
| `bin/*` | `~/.local/bin/` |
| `plugins/<name>/` | `~/.local/share/herdr-plugins/<name>/`, then `herdr plugin link <dir>` |
| `plugins/space-scope/space-scope` | also symlinked as `~/.local/bin/herdr-space-scope` |
| `compose/compose.lua` | `~/.local/share/herdr-compose/compose.lua` |
| `config/config.toml` | `~/.config/herdr/config.toml` |
| `config/sort-rules.example.toml` | `~/.config/herdr/sort-rules.toml`, edited for your projects |
| `config/ccnav-keys.example.toml` | key reference for `ccnav-herdr` |
| `hooks/herdr-project-brief.py` | `~/.claude/hooks/`, registered as a Claude Code `SessionStart` hook |
| `skills/close/` | symlinked as `~/.claude/skills/close` |

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
  - `loops` finds closed sessions whose next step went nowhere, clears the ones a later
    session resolved, and walks the rest. `review --since 7d` groups closes by disposition.
  - Model runs go through `claude -p` (`HERDR_CULL_MODEL`), persisted under
    `~/.local/state/herdr-sort/cull-runs` so `w` on a card can `claude --resume` them;
    close notes are cached in `notes.json`, keyed by transcript size.
- **`herdr-compose`** — `prefix+i`: nvim split under the agent for the next prompt,
  `Ctrl-S` sends. `prefix+shift+k/j` scroll the agent.
- **`ccnav-herdr`** — turn navigation, `prefix+u/d/f/a` (prev / next / newest / bottom),
  routed via `herdr-compose nav`.

## Plugins (`plugins/`)

- **`herdr-dash`** — `prefix+m`: agents, memory by pane, `MEMORY.md` health,
  prefix cheat-sheet, notes. Set `HERDR_DASH_MEMORY_DIR` to your Claude Code memory directory.
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
that workspace's `brief` from `sort-rules.toml` as context, so an unqualified prompt resolves
to the right project. Silent on every failure path.

## Claude Code skill

`/close` — the in-session counterpart of `herdr-sort cull`. Checks the session's open items
live, pushes back while any are still the session's to finish, otherwise ends on a close report
and the `herdr-sort close --as <d>` command for this pane. User-invoked only.

## Keys

The prefix is `ctrl+b` (herdr's default). On the author's setup, Caps Lock sends it through a
terminal-scoped Karabiner chord. All custom bindings are in `config/config.toml`.
