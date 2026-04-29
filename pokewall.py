#!/usr/bin/env python3
"""Update macOS wallpaper based on completed Asana tasks in a project."""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
SPRITES_DIR = ROOT / "sprites"
OUT_DIR = ROOT / "out"

ASANA_BASE = "https://app.asana.com/api/1.0"
SPRITE_URL = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{id}.png"
POKEMON_MAX_ID = 1010
MIN_SPRITE = 94      # cell size floor — defines grid capacity
SPRITE_SIZE = 170    # rendered sprite size; >= MIN_SPRITE so sprites overlap into neighboring cells


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"missing {CONFIG_PATH}; copy config.example.json and fill in values")
    cfg = json.loads(CONFIG_PATH.read_text())
    required = {"asana_token", "project_gid", "base_image"}
    missing = required - cfg.keys()
    if missing:
        sys.exit(f"config.json missing keys: {sorted(missing)}")
    if cfg["asana_token"].startswith("PASTE_"):
        sys.exit("config.json still contains placeholder values")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text())
        s.setdefault("revealed", [])
        return s
    return {"revealed": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_my_gid(token: str) -> str:
    r = requests.get(f"{ASANA_BASE}/users/me", headers=_auth(token), timeout=30)
    r.raise_for_status()
    return r.json()["data"]["gid"]


def count_my_completed_tasks(token: str, project_gid: str, my_gid: str) -> int:
    params = {
        "completed_since": "2000-01-01T00:00:00.000Z",
        "opt_fields": "completed,assignee",
        "limit": 100,
    }
    url = f"{ASANA_BASE}/projects/{project_gid}/tasks"
    count = 0
    while True:
        r = requests.get(url, headers=_auth(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for task in body.get("data", []):
            if not task.get("completed"):
                continue
            assignee = task.get("assignee") or {}
            if assignee.get("gid") == my_gid:
                count += 1
        nxt = body.get("next_page")
        if not nxt or not nxt.get("offset"):
            return count
        params = {**params, "offset": nxt["offset"]}


def fetch_sprite(pid: int) -> Path | None:
    path = SPRITES_DIR / f"{pid}.png"
    if path.exists():
        return path
    SPRITES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(SPRITE_URL.format(id=pid), timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.content:
        return None
    path.write_bytes(r.content)
    return path


def grid_dims(canvas_w: int, canvas_h: int) -> tuple[int, int, int]:
    """(cols, rows, cell_size) maximizing slot count subject to cell >= MIN_SPRITE."""
    cols = max(1, canvas_w // MIN_SPRITE)
    rows = max(1, canvas_h // MIN_SPRITE)
    cell = min(canvas_w // cols, canvas_h // rows)
    return cols, rows, cell


def reveal_new(revealed: list[int], target_count: int, capacity: int) -> list[int]:
    """Sync revealed list to exactly `target_count` unique pokedex IDs in 1..capacity.

    Grow: append randomly-picked unrevealed IDs.
    Shrink: drop from the end (most recently revealed go first).
    Out-of-range IDs from prior runs are filtered out before sizing.
    """
    seen: set[int] = set()
    filtered: list[int] = []
    for pid in revealed:
        if 1 <= pid <= capacity and pid not in seen:
            seen.add(pid)
            filtered.append(pid)
    target = min(capacity, max(0, target_count))
    if target < len(filtered):
        return filtered[:target]
    if target > len(filtered):
        pool = [i for i in range(1, capacity + 1) if i not in seen]
        random.shuffle(pool)
        return filtered + pool[:target - len(filtered)]
    return filtered


def compose_wallpaper(base_path: Path, revealed: list[int], out_path: Path) -> None:
    base = Image.open(base_path).convert("RGBA")
    W, H = base.size
    cols, rows, cell = grid_dims(W, H)
    capacity = cols * rows
    x_off = (W - cols * cell) // 2
    y_off = (H - rows * cell) // 2
    sprite_size = max(cell, SPRITE_SIZE)
    # Padded canvas so sprites overflowing the visible area don't error;
    # we crop back to base size at the end.
    pad = sprite_size
    canvas = Image.new("RGBA", (W + 2 * pad, H + 2 * pad), (0, 0, 0, 0))
    canvas.paste(base, (pad, pad))
    for pid in revealed:
        if pid > capacity:
            continue
        sp = fetch_sprite(pid)
        if not sp:
            continue
        try:
            s = Image.open(sp).convert("RGBA")
        except (OSError, ValueError):
            continue
        s = s.resize((sprite_size, sprite_size), Image.LANCZOS)
        idx = pid - 1
        col, row = idx % cols, idx // cols
        cx = pad + x_off + col * cell + cell // 2
        cy = pad + y_off + row * cell + cell // 2
        canvas.alpha_composite(s, (cx - sprite_size // 2, cy - sprite_size // 2))
    final = canvas.crop((pad, pad, pad + W, pad + H))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    final.convert("RGB").save(out_path, "PNG")


def refresh_wallpaper() -> None:
    """Force WallpaperAgent to re-read the current wallpaper file.

    We keep the wallpaper at a fixed path that the user pointed System
    Settings → Wallpaper at (with 'Show on all Spaces' enabled). Killing
    WallpaperAgent restarts it; on restart it re-reads the same path and
    picks up the updated image content. Crucially, this never calls
    `set picture`, so the 'all Spaces' toggle is left alone.
    """
    subprocess.run(["killall", "WallpaperAgent"], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Regenerate even if completed-task count is unchanged")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state()

    try:
        token = cfg["asana_token"]
        my_gid = get_my_gid(token)
        count = count_my_completed_tasks(token, cfg["project_gid"], my_gid)
    except requests.RequestException as e:
        log(f"asana fetch failed: {e}")
        return 1

    base_image = Path(os.path.expanduser(cfg["base_image"]))
    if not base_image.exists():
        sys.exit(f"base image not found: {base_image}")

    with Image.open(base_image) as probe:
        cols, rows, _ = grid_dims(*probe.size)
    capacity = cols * rows

    prev = state.get("revealed", [])
    revealed = reveal_new(prev, count, capacity)
    diff = len(revealed) - len(prev)

    log(f"completed: {count} | revealed: {len(revealed)}/{capacity} ({diff:+d})")

    if not args.force and revealed == prev:
        return 0

    out_path = OUT_DIR / "wallpaper.png"
    compose_wallpaper(base_image, revealed, out_path)
    refresh_wallpaper()

    state["revealed"] = revealed
    save_state(state)
    log(f"wallpaper updated: {len(revealed)} pokemon on grid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
