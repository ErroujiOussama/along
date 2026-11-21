#!/usr/bin/env bash
# Calibrate the bimanual SO-101 follower pair (left=5B8E113306, right=5B61036524).
# sg dialout: this login session predates the dialout group membership.
cd "$(dirname "$0")/.."   # repo root; calibration goes to ./calibration
L=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B8E113306-if00
R=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61036524-if00
exec sg dialout -c ".venv/bin/lerobot-calibrate --robot.type=bi_so_follower --robot.id=bimanual --robot.calibration_dir=calibration --robot.left_arm_config.port=$L --robot.right_arm_config.port=$R"
