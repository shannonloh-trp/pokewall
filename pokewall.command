#!/bin/bash
# Double-click this file in Finder to refresh the wallpaper now.
# Uses --force so it regenerates even if the task count is unchanged.
cd "$(dirname "$0")"
/usr/bin/python3 pokewall.py --force
