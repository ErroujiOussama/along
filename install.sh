#!/usr/bin/env bash
# One-time setup on the robot laptop: venv, `robot` command, systemd user unit, LeRobot calibration links.
set -euo pipefail
cd "$(dirname "$0")"; ROOT=$PWD
uv sync
mkdir -p ~/.local/bin ~/.config/systemd/user
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/robot.py "$@"\n' "$ROOT" "$ROOT" > ~/.local/bin/robot
chmod +x ~/.local/bin/robot
ln -sf "$ROOT/robotd.service" ~/.config/systemd/user/robotd.service
systemctl --user daemon-reload
# plain LeRobot tools read calibration from its cache: point it at the repo copy (one source of truth)
CACHE=~/.cache/huggingface/lerobot/calibration/robots/so_follower
mkdir -p "$CACHE"
for s in left right; do ln -sf "$ROOT/calibration/bimanual_$s.json" "$CACHE/bimanual_$s.json"; done
echo "installed: robot CLI, robotd.service (start: systemctl --user start robotd), calibration links"
