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

Tool state and logs go to `~/.local/state/herdr-*/`.

## Tools (`bin/`)

- **`herdr-sessions`** — table of open agent sessions; `--watch`, `--json`, `--status blocked`.
- **`herdr-sort`** — routes tabs into workspaces by session title (`sort-rules.toml`);
  KEEP / GLANCE / CLOSE ratings, close with a reopenable ledger.
- **`herdr-compose`** — `prefix+i`: nvim split under the agent for the next prompt,
  `Ctrl-S` sends. `prefix+shift+k/j` scroll the agent.
- **`ccnav-herdr`** — turn navigation, `prefix+u/d/f/a` (prev / next / newest / bottom),
  routed via `herdr-compose nav`.

## Plugins (`plugins/`)

- **`herdr-dash`** — `prefix+m`: agents, memory by pane, `MEMORY.md` health,
  prefix cheat-sheet, notes. Set `HERDR_DASH_MEMORY_DIR` to your Claude Code memory directory.
- **`herdr-sort`** — plugin wrapper and close ledger for `bin/herdr-sort`.
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

## Keys

The prefix is `ctrl+b` (herdr's default). On the author's setup, Caps Lock sends it through a
terminal-scoped Karabiner chord. All custom bindings are in `config/config.toml`.
