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
| `compose` | `bin/herdr-compose` → `~/.local/bin` | `prefix+i` | linux, macos (needs nvim) |
| `ccnav` | `bin/ccnav-herdr` → `~/.local/bin` | `prefix+u/d/f/a`, `prefix+shift+k/j` | linux (`/proc`); needs compose |
| `sessions` | `bin/herdr-sessions` → `~/.local/bin` | — | linux (`/proc`) |
| `sort` | `bin/herdr-sort`, plugin, seeds `sort-rules.toml` | — | linux; untested on macos |
| `dash` | plugin `herdr-dash` | `prefix+m` | linux, macos (RSS by pane instead of PSS/cgroup) |
| `viewer` | plugin `viewer` | `prefix+shift+v`, `prefix+y` | linux, macos (needs `rich`) |
| `space-scope` | plugin + `~/.local/bin/herdr-space-scope` | `prefix+shift+s` | linux, macos |
| `brief-hook` | `~/.claude/hooks/herdr-project-brief.py`, registered in `~/.claude/settings.json` | — | linux, macos |

Plugins are linked from `~/.local/share/herdr-plugins/<name>`. Shared Python
(`toml_min`, `tooling`) lives in `lib/`, and the tools find it by resolving their
own symlinks, so the checkout must be installed with `install`, not copied.
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
  prefix cheat-sheet, notes. The memory directory is `[dash] memory-dir` in
  `tooling.toml` (`HERDR_DASH_MEMORY_DIR` overrides it). The tabs and agents sections
  appear only where `sort` and `sessions` are on.
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
that workspace's `[[brief]]` from `tooling.toml` (or, when there are none, its `brief` from
`sort-rules.toml`) as context, so an unqualified prompt resolves
to the right project. Silent on every failure path.

## Keys

The prefix is `ctrl+b` (herdr's default). On the author's Linux setup, Caps Lock sends it
through a terminal-scoped Karabiner chord; `[dash] prefix-label = "Caps Lock"` names it so in
the cheat-sheet. Each component's bindings are in `config/keys/`. Remap or drop one per
machine with `[keys]` in `tooling.toml`.
