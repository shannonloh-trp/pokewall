# pokewall

Turns a macOS desktop wallpaper into a slowly-filling pokedex driven by completed Asana tasks. Each task you finish reveals one new pokemon at its canonical pokedex slot on the wallpaper grid; un-completing a task removes the most recently revealed one.

## How it works

Every tick (manual run or background poll):

1. Asks Asana how many tasks in a given project are assigned to you and marked complete.
2. Compares that count to `state.json` and grows or shrinks the revealed list — growing picks a random unrevealed pokemon, shrinking drops the most recently revealed.
3. Composes `base.jpg` + sprites into `out/wallpaper.png` at each pokemon's pokedex-grid position.
4. Nudges macOS's `WallpaperAgent` to re-read the file in place.

## Setup

1. Install dependencies into the system Python user-site (the LaunchAgent uses `/usr/bin/python3`, not a venv):

   ```bash
   /usr/bin/python3 -m pip install --user requests Pillow
   ```

2. Copy the config template and fill it in:

   ```bash
   cp config.example.json config.json
   ```

   - `asana_token` — Asana Personal Access Token
   - `project_gid` — numeric segment after `/project/` in the Asana board URL
   - `base_image` — path to your wallpaper canvas (must match your display aspect ratio)

3. Set `out/wallpaper.png` as your desktop wallpaper **once** in System Settings → Wallpaper, with **Show on all Spaces** enabled. Don't change it again — the script overwrites this file in place each run.

## Usage

```bash
# One-shot (only re-renders if the revealed set changed)
python3 pokewall.py

# Force regenerate (after editing base.jpg, sprite size, or for testing)
python3 pokewall.py --force

# Background polling every 30s, survives reboots
launchctl load -w ~/Library/LaunchAgents/com.shannon.pokewall.plist
launchctl unload ~/Library/LaunchAgents/com.shannon.pokewall.plist
launchctl list | grep pokewall
tail -f log.txt

# Double-click in Finder for an immediate refresh
open run-pokewall.command
```

## Files

- `pokewall.py` — the entire program
- `config.json` — runtime config (gitignored; holds the real PAT)
- `config.example.json` — template
- `state.json` — `{ "revealed": [...] }`, the list of pokedex IDs currently on the wallpaper, in reveal order
- `base.jpg` — wallpaper canvas at display resolution; `base.original.jpg` is the pre-resize backup
- `sprites/{id}.png` — PokeAPI sprite cache, lazy-fetched on first use
- `out/wallpaper.png` — generated wallpaper (overwritten each render)
- `log.txt` — LaunchAgent stdout/stderr
- `run-pokewall.command` — Finder-clickable that runs `pokewall.py --force`

## Tuning

Two constants near the top of `pokewall.py`:

- `MIN_SPRITE` — cell size floor; lowering it packs more slots into the grid
- `SPRITE_SIZE` — rendered sprite size; raising it above `MIN_SPRITE` makes pokemon overlap into neighboring cells

Grid layout is computed from `base.jpg` dimensions, not the display, so the base image must match your display's aspect ratio or macOS "Fill Screen" will crop sprites out of view.

## Requirements

- macOS (uses `killall WallpaperAgent`)
- Python 3.9+ (system Python is fine)
- `requests`, `Pillow`
- An Asana account with a project to track
