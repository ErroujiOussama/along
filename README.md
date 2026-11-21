# so101

Two SO-101 follower arms and three cameras on one laptop, behind a single daemon.

```
arms + cameras ← robotd.py (only bus owner, one shared state) ← so101.py (Python API) ← robot CLI · tui.py panel · agents
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
robot step "move right_elbow_flex -5; set right_gripper 20"   # run, wait until settled, snap_all.jpg + ~4 state lines
robot step                       # just look: wrist cams on top (400x225), scene below (800x450) + joints, HOLDING, tip xyz + moved
robot recall ready right         # from rest: gripper pointing down, tip 10 cm above the table (min 5.7 cm on the way)
robot nudge right 2 0 -1         # tip dx dy dz cm, ≤ 3 cm; discouraged (re-estimate + goto instead)
robot locate 400 300 0.05        # scene pixel (800x450) -> xyz where its ray meets z = 0.05 m, both arm frames
robot ik right 0.3 0.05 0.06     # dry run: joint targets for a tip xyz, moves nothing
robot move left elbow -5         # relative (deg; gripper %); `set` = absolute
robot open right · robot close right · robot goto left 0.2 0 0.08 · robot tip left
robot torque off left            # limp for hand-guiding — HOLD THE ARM; `torque on left` holds the new pose
robot save NAME · robot recall NAME [side] · robot stop · robot home
robot snap [left|right|scene]    # full-res newest frames → snap_<cam>.jpg (only when detail is needed)
robot cams                       # which cameras deliver (unplugged = grey tile in step/view/stream)
robot cam scene [k=v ...|reset]  # V4L2 controls (exposure, gain, focus, …), saved in cams.json, re-applied on replug
robot view [cam ...]             # live video window, all cameras side by side (through robotd)
robot stream [port]              # same as a web page (MJPEG, :8101); /inspect = rollout inspector (steps, images, 3D replay, live)
                                 # page has a "camera settings" panel (same as robot cam; open directly with /#settings)
                                 # share: `tailcat serve exec -- socat - TCP:127.0.0.1:8101`, viewers `tailcat browse <addr>`
robot raw "move right_elbow_flex -5; wait; snap"   # raw protocol; ';' chains, wait = until settled
.venv/bin/python tui.py          # keyboard panel + live camera (v); shared as quack session so101-tui
systemctl --user stop robotd     # park at rest, release torque
```

## Along (voice helper)

```bash
along idle      # once, with the table in its idle state: what the detector (YOLO-World, along_world.pt) may ignore
along run       # active only while an object of interest is there (new in the scene, or an arm holding/offering):
                # C920 mic -> Silero VAD -> ElevenLabs STT -> Codex (tmux `along`, persona prompts/along.md)
                # -> robot + `along say` (ElevenLabs TTS). Otherwise idle: mic off, no voice commands
along look "where is the toothbrush?" · along see · along say "hi" · along tell "[voice] hold this"   # detections · speak · type into the Codex session
```
Key: `ELEVENLABS_API_KEY` in `.env`. Watch the agent: `tmux attach -t along`; debug page `robot stream` → `/along`
(detector boxes, state, conversation, Codex pane, type-as-voice, force wake).

```python
from so101 import Robot
bot = Robot()
bot.left.move(elbow=-5); bot.right.goto(0.2, 0, 0.08); bot.left.torque(False); img = bot.frame()
bot.step("move right_elbow_flex -5"); bot.snap_all(); bot.right.holding(); bot.cams()
bot.locate(400, 300); bot.right.ik(0.3, 0.05, 0.06)
```

`robot help` lists everything. Joints: pan lift elbow wrist roll gripper. Sides: left (white), right (orange).

## Files

| | |
|---|---|
| `robotd.py`, `robotd.service` | daemon + systemd user unit |
| `so101.py` · `robot.py` · `tui.py` | Python API · CLI · panel |
| `kin.py`, `so101.urdf` | FK/IK (URDF from TheRobotStudio/SO-ARM100, meshes stripped) |
| `calibration/` | servo calibration, the only copy (`tools/calibrate.sh` rewrites it) |
| `poses.json` · `cams.json` | saved poses (`rest`, `ready`) · camera settings (scene: 50 Hz anti-flicker) |
| `v4l.py` | V4L2 controls without v4l2-ctl |
| `test_sim.py` | end-to-end checks against the sim |
| `tools/` | `scan.py` (which servo IDs answer), `calibrate.sh` — stop robotd first |
| `AGENTS.md` · `prompts/` · `notes/` | rules for coding agents driving the arms · reusable task prompts (`pick.md`) · parked ideas (`premotor.md`) |

## How robotd works

- 50 Hz loop; commands only change the target (reply < 1 ms, no queueing), so a new command preempts the old one.
- Targets ramp at 45°/s, are clamped to the calibrated range, and may lead the measurement by ≤ 15°.
  A small integral term (≤ 6°) cancels gravity sag.
- Torque off: the arm's targets follow your hand, and motion commands for it are rejected.
- Power cut: robotd sends nothing; when the bus returns it re-applies servo settings and holds the current pose.
- USB replug: it reopens the arm ports by their by-id paths every 2 s until the arms answer, then holds the current pose.
  Cameras: a camera with no frame for 2 s reports unplugged (snap skips it, view/stream show a grey tile) and reopens by by-id path when it is back.
- Crash: systemd restarts it, holding the pose it finds.
- Knobs: `SO101_P` (servo P, 16), `SO101_VEL`, `SO101_LEAD`, `SO101_KI`.
- No hardware: `SO101_SIM=1` on robotd and on any client (own socket and poses); then `SO101_SIM=1 .venv/bin/python test_sim.py`.

## Hardware

- LEFT = white `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B8E113306-if00`; RIGHT = orange `…5B61036524-if00`. STS3215 ×6 each, 1 Mbaud.
- Cameras (`/dev/v4l/by-id/…`): left wrist icSpring, right wrist Sonix USB2.0_CAM1 (1280x720), scene Logitech C920 (1920x1080).
  Plug the cameras into the laptop, not the hub (hub → USB 1.1, 160x120 purple frames).
- Units: degrees, 0 = mid-range; gripper 0–100, 0 = closed.
- Rest ≈ pan 0, lift -102, elbow 96, wrist_flex 60–67, gripper 3–5 (lift and elbow on their hard stops).

## Kinematics

`fk(q5)` → tip xyz (m, arm base frame: x forward, y left, z up); `ik(q0, xyz)` gripper pointing down, ~3 mm accurate in x 0.18–0.32, |y| ≤ 0.08, z ≥ 0.02.
Signs: see the joint table in AGENTS.md (right-arm pan still unconfirmed).

## Lessons

- Leaving rest: raise shoulder_lift ≥ 10° first, then elbow, then wrist; blind moves drag the gripper on the table.
- `goto` from rest fails (first IK step jumps ~37°): jog to a pointing-down pose, `save ready_<side>`, `goto` from there.
- Never use LeRobot `max_relative_target`: it clips goals to present ±N°, so loaded joints (elbow) never get enough torque.
