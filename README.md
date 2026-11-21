# so101

Two SO-101 follower arms and one camera on one laptop, behind a single daemon.

```
arms + camera ← robotd.py (only bus owner, one shared state) ← so101.py (Python API) ← robot CLI · tui.py panel · agents
```

## Setup

```bash
./install.sh                     # uv sync, `robot` command, systemd user unit, LeRobot calibration links
systemctl --user start robotd    # never autostarts; logs: journalctl --user -u robotd -f
robot save rest                  # once, with the arms folded at rest: home/stop/shutdown park here
```

## Use

```bash
robot                            # state: target vs measured per joint, torque, loop Hz
robot move left elbow -5         # relative (deg; gripper %); `set` = absolute
robot open right · robot close right · robot goto left 0.2 0 0.08 · robot tip left
robot torque off left            # limp for hand-guiding — HOLD THE ARM; `torque on left` holds the new pose
robot save NAME · robot recall NAME [side] · robot stop · robot home · robot snap
robot view [left|right]         # live video window of the wrist cameras (through robotd)
robot raw "move right_elbow_flex -5; wait; snap"   # raw protocol; ';' chains, wait = until settled
.venv/bin/python tui.py          # keyboard panel + live camera (v); shared as quack session so101-tui
systemctl --user stop robotd     # park at rest, release torque
```

```python
from so101 import Robot
bot = Robot()
bot.left.move(elbow=-5); bot.right.goto(0.2, 0, 0.08); bot.left.torque(False); img = bot.frame()
```

`robot help` lists everything. Joints: pan lift elbow wrist roll gripper. Sides: left (white), right (orange).

## Files

| | |
|---|---|
| `robotd.py`, `robotd.service` | daemon + systemd user unit |
| `so101.py` · `robot.py` · `tui.py` | Python API · CLI · panel |
| `kin.py`, `so101.urdf` | FK/IK (URDF from TheRobotStudio/SO-ARM100, meshes stripped) |
| `calibration/` | servo calibration, the only copy (`tools/calibrate.sh` rewrites it) |
| `poses.json` | saved poses (`rest`, …) |
| `test_sim.py` | 20 end-to-end checks against the sim |
| `tools/` | `scan.py` (which servo IDs answer), `calibrate.sh` — stop robotd first |
| `AGENTS.md` | rules for coding agents driving the arms |

## How robotd works

- 50 Hz loop; commands only change the target (reply < 1 ms, no queueing), so a new command preempts the old one.
- Targets ramp at 45°/s, are clamped to the calibrated range, and may lead the measurement by ≤ 15°.
  A small integral term (≤ 6°) cancels gravity sag.
- Torque off: the arm's targets follow your hand, and motion commands for it are rejected.
- Power cut: robotd sends nothing; when the bus returns it re-applies servo settings and holds the current pose.
- USB replug: it reopens the ports by their by-id paths every 2 s until the arms answer, then holds the current pose.
- Crash: systemd restarts it, holding the pose it finds.
- Knobs: `SO101_P` (servo P, 16), `SO101_VEL`, `SO101_LEAD`, `SO101_KI`.
- No hardware: `SO101_SIM=1` on robotd and on any client (own socket and poses); then `SO101_SIM=1 .venv/bin/python test_sim.py`.

## Hardware

- LEFT = white `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B8E113306-if00`; RIGHT = orange `…5B61036524-if00`. STS3215 ×6 each, 1 Mbaud.
- Camera `/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB2.0_CAM1_USB2.0_CAM1-video-index0`, facing the arms (left arm on the image right).
  Plug it into the laptop, not the hub (hub → USB 1.1, 160x120 purple frames).
- Units: degrees, 0 = mid-range; gripper 0–100, 0 = closed.
- Rest ≈ pan 0, lift -102, elbow 96, wrist_flex 60–67, gripper 3–5 (lift and elbow on their hard stops).

## Kinematics

`fk(q5)` → tip xyz (m, arm base frame: x forward, y left, z up); `ik(q0, xyz)` gripper pointing down, ~3 mm accurate in x 0.18–0.32, |y| ≤ 0.08, z ≥ 0.02.
Pan/wrist sign match to the hardware is still unverified.

## Lessons

- Leaving rest: raise shoulder_lift ≥ 10° first, then elbow, then wrist; blind moves drag the gripper on the table.
- `goto` from rest fails (first IK step jumps ~37°): jog to a pointing-down pose, `save ready_<side>`, `goto` from there.
- Never use LeRobot `max_relative_target`: it clips goals to present ±N°, so loaded joints (elbow) never get enough torque.

## Next: grasping

Per arm, `goto` 4–5 table points at z≈0.01, snap each, fit `cv2.findHomography(tip_pixels, robot_xy)`.
Then: object pixel → xy → hover z=0.08 → open → z≈0.015 → close → lift → confirm in a snap.
