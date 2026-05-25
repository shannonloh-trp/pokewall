# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Personal tool that turns the user's macOS wallpaper into a pokedex visualization driven by completed Asana tasks. Each task they finish "reveals" one new pokemon at its canonical pokedex slot on the wallpaper grid; un-completing a task removes the most recently revealed one.

## Commands

```bash
# Manual one-shot (only regenerates if revealed-set changed)
python3 ~/Development/pokewall/pokewall.py

# Force regenerate (after editing base.jpg, sprite size, or for testing)
python3 ~/Development/pokewall/pokewall.py --force

# Always include specific pokemon by pokedex ID (rest filled randomly to the task count)
python3 ~/Development/pokewall/pokewall.py --pin 25,6,150 --force

# Background polling (every 30s, survives reboots)
launchctl load -w ~/Library/LaunchAgents/com.shannon.pokewall.plist
launchctl unload ~/Library/LaunchAgents/com.shannon.pokewall.plist  # stop
launchctl list | grep pokewall                                       # check
tail -f ~/Development/pokewall/log.txt                               # watch

# Double-clickable refresh (in Finder)
open ~/Development/pokewall/run-pokewall.command
```

No tests, no build, no linter — single-file Python script.

## Architecture

The flow each tick (manual or LaunchAgent-driven):

1. **Asana count** — `count_my_completed_tasks()` paginates `/projects/{gid}/tasks` and returns the number of tasks where `assignee.gid == my_gid` AND `completed == true`. Three HTTP calls per tick: `/users/me`, the paginated tasks listing.
2. **Reveal sync** — `reveal_new(prev_revealed, count, capacity, pinned)` returns a list of pokedex IDs of length exactly `min(count, capacity)`. Grows by appending random unrevealed IDs; shrinks by truncating from the end. **The `revealed` list order encodes reveal history** — the last element is the most-recently-revealed and the first to disappear when count drops. **Pinned IDs (from `--pin`) are placed at the front**, so they disappear last and — because auto runs preserve the stored order — persist across later ticks without re-passing `--pin` (until the count drops below the number of pins). Auto/LaunchAgent runs pass no pins; they stay fully Asana-driven and random.
3. **Idempotence guard** — if `revealed == prev` and `--force` is not set, exit without re-rendering.
4. **Compose** — `compose_wallpaper()` lays sprites onto a padded canvas at their pokedex-grid positions (slot of pid `N` = `((N-1) % cols, (N-1) // cols)`), then crops back to base size. Padding allows sprites larger than the cell to overflow without erroring.
5. **Refresh** — `refresh_wallpaper()` calls `killall WallpaperAgent`. **It does not call `osascript set picture`** — see "macOS gotchas" below.

### State model

`state.json` is the source of truth for what's currently on the wallpaper:

```json
{ "revealed": [7, 38, 61, 139, 148, 201, ...] }
```

The list of pokedex IDs in reveal order. `len(revealed)` is the visible pokemon count; `revealed[-1]` is the next to disappear if the user un-completes a task. `last_count` may also appear (legacy field, ignored by current code).

### Grid layout

Computed each run from the **base image dimensions**, not the display:

- `cols = base_w // MIN_SPRITE`, `rows = base_h // MIN_SPRITE` — packs as many ≥94px cells as fit
- `cell = min(base_w // cols, base_h // rows)` — actual cell size (≥ MIN_SPRITE)
- Sprites render at `max(cell, SPRITE_SIZE)` px, centered on cell — overflows into neighboring cells when `SPRITE_SIZE > cell`

Two constants near the top of `pokewall.py` control sizing: `MIN_SPRITE` (cell floor → grid capacity) and `SPRITE_SIZE` (rendered size, can exceed the cell). Bumping `SPRITE_SIZE` makes pokemon bigger and more overlappy without changing slot positions.

## Configuration

`config.json` (gitignored — contains a real Asana PAT):

```json
{
  "asana_token":       "Asana Personal Access Token",
  "project_gid":       "Numeric segment after /project/ in the Asana board URL",
  "base_image":        "~/Development/pokewall/base.jpg",
  "done_section_name": "Completed"   // legacy, currently unused (kept for history)
}
```

Counting is by `assignee=me AND completed=true` in the project, not by section membership — the `done_section_name` field is dead code preserved for context.

## macOS gotchas (load-bearing knowledge)

These constraints drove non-obvious design choices. **Don't "simplify" them away without understanding why.**

- **Wallpaper output is a fixed path** (`out/wallpaper.png`) that the user has manually selected once via System Settings → Wallpaper, with **"Show on all Spaces"** enabled. Each run overwrites that file in place.
- **Never call `osascript ... set picture` again.** Doing so pins a wallpaper to the *current* Space and silently disables "Show on all Spaces" — every other Space stops updating. The user re-enables that toggle, the next `set picture` flips it off again. We use `killall WallpaperAgent` instead, which forces re-read of the file at the user-set path without touching the per-Space pinning. NSWorkspace's `setDesktopImageURL` has the same problem — it's not a viable swap.
- **Base image must match display aspect ratio.** If `base.jpg` aspect ≠ display aspect, macOS "Fill Screen" crops the edges and any sprite in the cropped columns becomes invisible to the user. The current `base.jpg` is `3024×1964` (display-native); `base.original.jpg` is the user's pre-resize source. The script does **not** auto-detect display dims — it uses the base image as the placement canvas, so the base must be sized correctly.
- **System Python is 3.9** (`/usr/bin/python3`). Dependencies (`requests`, `Pillow`) are installed to the user-site (`~/Library/Python/3.9/lib/python/site-packages`) — not a venv. The LaunchAgent invokes `/usr/bin/python3` directly, so any new dependency must be importable from the system Python's user-site.
- **Sprite cache is on-disk** under `sprites/{id}.png`, lazy-fetched from the PokeAPI sprites repo URL pattern at first use. After each render, `prune_sprites()` deletes any sprite not in the current revealed set, so the folder mirrors what's on the wallpaper (currently the 22 revealed). Removed/reshuffled pokemon are re-fetched on demand if revealed again. Don't bulk-fetch.

## File map

- `pokewall.py` — the entire program
- `config.json` — runtime config (real PAT inside)
- `config.example.json` — template
- `state.json` — `{ "revealed": [...] }`, source of truth for current wallpaper contents
- `base.jpg` — wallpaper canvas at display resolution; `base.original.jpg` is the pre-resize backup
- `sprites/{id}.png` — PokeAPI sprite cache, lazy-populated and pruned to match the revealed set on each render (see `prune_sprites()`)
- `out/wallpaper.png` — generated wallpaper (overwritten each render)
- `log.txt` — LaunchAgent stdout/stderr
- `run-pokewall.command` — Finder-clickable that runs `pokewall.py --force`; has a custom pokeball icon set via `fileicon`
- `~/Library/LaunchAgents/com.shannon.pokewall.plist` — schedules `pokewall.py` every 30s; lives outside this directory
