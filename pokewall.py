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


def _collect_my_completed_gids(token: str, url: str, my_gid: str,
                               base_params: dict) -> set[str]:
    """GIDs of my completed tasks across a paginated task listing endpoint."""
    params = {**base_params, "opt_fields": "completed,assignee", "limit": 100}
    gids: set[str] = set()
    while True:
        r = requests.get(url, headers=_auth(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for task in body.get("data", []):
            if task.get("completed") and (task.get("assignee") or {}).get("gid") == my_gid:
                gids.add(task["gid"])
        nxt = body.get("next_page")
        if not nxt or not nxt.get("offset"):
            return gids
        params = {**params, "offset": nxt["offset"]}


def my_completed_task_gids(token: str, project_gid: str, my_gid: str) -> set[str]:
    """GIDs of my completed tasks in the project."""
    url = f"{ASANA_BASE}/projects/{project_gid}/tasks"
    return _collect_my_completed_gids(
        token, url, my_gid, {"completed_since": "2000-01-01T00:00:00.000Z"})


def my_completed_task_gids_with_tag(token: str, tag_gid: str, my_gid: str) -> set[str]:
    """GIDs of my completed tasks carrying the given tag (any project)."""
    url = f"{ASANA_BASE}/tags/{tag_gid}/tasks"
    return _collect_my_completed_gids(token, url, my_gid, {})


def get_project_workspace(token: str, project_gid: str) -> str:
    r = requests.get(f"{ASANA_BASE}/projects/{project_gid}", headers=_auth(token),
                     params={"opt_fields": "workspace"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["workspace"]["gid"]


def find_tag_gid(token: str, workspace_gid: str, tag_name: str) -> str | None:
    """GID of the workspace tag named `tag_name` (case-insensitive), or None."""
    target = tag_name.strip().lower()
    params = {"opt_fields": "name", "limit": 100}
    url = f"{ASANA_BASE}/workspaces/{workspace_gid}/tags"
    while True:
        r = requests.get(url, headers=_auth(token), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for tag in body.get("data", []):
            if (tag.get("name") or "").strip().lower() == target:
                return tag["gid"]
        nxt = body.get("next_page")
        if not nxt or not nxt.get("offset"):
            return None
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


def prune_sprites(revealed: list[int]) -> int:
    """Delete cached sprites for pokemon no longer on the wallpaper.

    Keeps sprites/ in sync with the revealed set; a pokemon that gets removed,
    reshuffled out, or randomised away has its sprite deleted and is re-fetched
    on demand by fetch_sprite() if it is revealed again later.
    """
    if not SPRITES_DIR.exists():
        return 0
    keep = {f"{pid}.png" for pid in revealed}
    removed = 0
    for path in SPRITES_DIR.glob("*.png"):
        if path.name not in keep:
            path.unlink()
            removed += 1
    return removed


def grid_dims(canvas_w: int, canvas_h: int) -> tuple[int, int, int]:
    """(cols, rows, cell_size) maximizing slot count subject to cell >= MIN_SPRITE."""
    cols = max(1, canvas_w // MIN_SPRITE)
    rows = max(1, canvas_h // MIN_SPRITE)
    cell = min(canvas_w // cols, canvas_h // rows)
    return cols, rows, cell


def reveal_new(revealed: list[int], target_count: int, capacity: int,
               pinned: list[int] | None = None) -> list[int]:
    """Sync revealed list to exactly `target_count` unique pokedex IDs in 1..capacity.

    Pinned: placed first so they survive shrinks (they disappear last) and
            persist across later auto runs that preserve the stored order.
    Grow: append randomly-picked unrevealed IDs.
    Shrink: drop from the end (most recently revealed go first).
    Out-of-range and duplicate IDs are filtered out before sizing.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for pid in list(pinned or []) + list(revealed):
        if 1 <= pid <= capacity and pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    target = min(capacity, max(0, target_count))
    if target < len(ordered):
        return ordered[:target]
    if target > len(ordered):
        pool = [i for i in range(1, capacity + 1) if i not in seen]
        random.shuffle(pool)
        return ordered + pool[:target - len(ordered)]
    return ordered


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
    parser.add_argument("--pin", default="",
                        help="Comma-separated pokedex IDs to always include, e.g. 25,6,150. "
                             "Pinned ones fill first; the rest are random up to the task count.")
    args = parser.parse_args()

    pinned: list[int] = []
    if args.pin:
        try:
            pinned = [int(x) for x in args.pin.split(",") if x.strip()]
        except ValueError:
            sys.exit(f"--pin expects comma-separated integers, got: {args.pin!r}")

    cfg = load_config()
    state = load_state()

    try:
        token = cfg["asana_token"]
        my_gid = get_my_gid(token)
        gids = my_completed_task_gids(token, cfg["project_gid"], my_gid)

        # Also count my completed tasks anywhere that carry the configured tag.
        # The tag gid is stable, so cache it in state to avoid re-resolving each tick.
        tag_name = cfg.get("tag_name", "Tech request")
        tag_cache = state.setdefault("tag_cache", {})
        tag_gid = tag_cache.get(tag_name)
        if tag_gid is None:
            workspace_gid = get_project_workspace(token, cfg["project_gid"])
            tag_gid = find_tag_gid(token, workspace_gid, tag_name)
            if tag_gid:
                tag_cache[tag_name] = tag_gid
                save_state(state)  # persist cache even if this tick renders nothing
        if tag_gid:
            gids |= my_completed_task_gids_with_tag(token, tag_gid, my_gid)
        else:
            log(f"tag {tag_name!r} not found; counting project tasks only")

        count = len(gids)
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
    revealed = reveal_new(prev, count, capacity, pinned)
    diff = len(revealed) - len(prev)

    log(f"completed: {count} | revealed: {len(revealed)}/{capacity} ({diff:+d})")
    if pinned:
        dropped = [p for p in pinned if p not in revealed]
        if dropped:
            log(f"pinned IDs not shown (out of range >{capacity} or beyond count {count}): {dropped}")

    if not args.force and revealed == prev:
        return 0

    out_path = OUT_DIR / "wallpaper.png"
    compose_wallpaper(base_image, revealed, out_path)
    refresh_wallpaper()

    state["revealed"] = revealed
    save_state(state)
    pruned = prune_sprites(revealed)
    msg = f"wallpaper updated: {len(revealed)} pokemon on grid"
    if pruned:
        msg += f"; pruned {pruned} unused sprite(s)"
    log(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
