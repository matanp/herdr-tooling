# Moving a hand-installed machine to `install`

This is for a machine set up the old way, by copying or symlinking files to the
paths the README used to list. It moves that machine to the `install` script and
the per-machine `tooling.toml`. Do it once per machine.

## What changes

| Before | After |
|---|---|
| `~/.config/herdr/config.toml` edited by hand, holding settings and every key binding | `config.local.toml` holds this machine's settings; `install` generates `config.toml` from it plus `config/keys/*.toml` |
| Tools copied or symlinked from wherever the checkout lived | Everything symlinked from `~/.local/src/herdr-tooling` |
| `HERDR_DASH_MEMORY_DIR` set in the environment | `[dash] memory-dir` in `tooling.toml` (the env var still overrides it) |
| Briefs in `sort-rules.toml` | Either place works; `[[brief]]` in `tooling.toml` wins when it has any |
| `~/.local/share/herdr-compose/compose.lua` | Not needed; compose finds it in the checkout |

## 1. Put the checkout in its fixed place

```sh
git clone https://github.com/matanp/herdr-tooling ~/.local/src/herdr-tooling
```

If a checkout already exists elsewhere, either move it there (`mv <old> ~/.local/src/herdr-tooling`)
or leave it and use the new clone. If `~/.config/herdr/config.toml` is a symlink to the
old checkout's `config/config.toml`, replace it with a real file before anything else, or
the next pull leaves herdr with no config:

```sh
cp --remove-destination "$(readlink -f ~/.config/herdr/config.toml)" ~/.config/herdr/config.toml
```

## 2. Write `tooling.toml`

```sh
~/.local/src/herdr-tooling/install    # writes ~/.config/herdr/tooling.toml, then stops
```

Edit `~/.config/herdr/tooling.toml` to match what this machine runs:

```toml
[components]
compose     = true
viewer      = true
dash        = true
space-scope = true
brief-hook  = true
sort        = true
sessions    = true
ccnav       = true

[dash]
memory-dir   = "~/.claude/projects/<your-project>/memory"   # was HERDR_DASH_MEMORY_DIR
prefix-label = "Caps Lock"
```

## 3. Clear what the old install left in the way

```sh
~/.local/src/herdr-tooling/install --dry-run
```

`install` replaces only symlinks that point into `~/.local/src/herdr-tooling`. Every
`skip <path>: exists and is not ours` line is an old copy or a link to another checkout.
Move each one aside:

```sh
mv ~/.local/bin/herdr-sort ~/.local/bin/herdr-sort.old       # repeat for each skipped path
```

The likely ones are `herdr-compose`, `ccnav-herdr`, `herdr-sessions`, `herdr-sort` and
`herdr-space-scope` in `~/.local/bin`, the four directories in
`~/.local/share/herdr-plugins/`, and `~/.claude/hooks/herdr-project-brief.py`. Run the
dry run again until the only lines left are `link`, `write` and `herdr plugin link`.

Plugins already linked from `~/.local/share/herdr-plugins/<name>` stay linked: the path
is the same, only what it points at changes.

## 4. Install, then remove the duplicate key bindings

```sh
~/.local/src/herdr-tooling/install
```

On this first run `install` copies your existing `config.toml` to `config.local.toml`,
because it did not write that file. That copy still holds every `[[keys.command]]`
block, and the generated config adds them again from `config/keys/`. Open
`~/.config/herdr/config.local.toml` and delete every `[[keys.command]]` block, with its
comment, keeping the prefix, `[ui]`, `[ui.toast]` and `[theme]`. Then:

```sh
~/.local/src/herdr-tooling/install
grep -c 'keys.command' ~/.config/herdr/config.toml    # one per binding, no repeats
```

If `herdr config check` rejects the duplicates, the first run stops before writing
`config.toml`. Do the same edit and re-run.

## 5. Check it

- `herdr config check` prints `config: ok`, and the old file is kept as `config.toml.bak-*`.
- `herdr plugin list` shows herdr-dash, herdr-sort, space-scope and viewer enabled.
- `prefix+m` opens the dashboard with its tabs and agents sections, and `prefix+u/d/f/a`
  move between turns.
- The running herdr-sort daemon is still the old code until herdr restarts. That's
  harmless.

Once everything works, delete the `*.old` files and `~/.local/share/herdr-compose/`.

## Afterwards

`git pull` in `~/.local/src/herdr-tooling` updates the tools. Re-run `install` after
editing `tooling.toml` or `config.local.toml`. Edit those two files, never `config.toml`.
