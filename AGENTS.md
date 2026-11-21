# Agent guide

You drive two real SO-101 arms (left = white, right = orange). The task comes in your prompt; README.md is the manual.
Use ONLY the `robot` CLI (or `from so101 import Robot`): robotd owns the arms and cameras. Never open serial ports or cameras, never start a second robotd.

**Rough plan → get closer → refine → go. No nudges** (`nudge` is disabled; every look-and-adjust round costs ~10 s).
1. Rough plan: `robot step`, open snap_all.jpg (wrist cams on top, scene camera below). `robot locate u v z` turns a scene
   pixel (800x450) into xyz in both arm frames (m: x forward, y arm's left, z up) at a height you choose. Estimate xyz and angle.
2. Get closer: ONE `robot plan` to a better viewpoint ~5 cm above (or in front of) the object, jaws open and turned:
   `robot plan "set <side>_gripper 30; set <side>_wrist_roll 90; goto <side> x y z+0.05"`. The sim checks it first.
   Every goto adds the wrist view from where it ended (with the camera pose) to the bottom strip of snap_all.jpg.
3. Refine the estimate from that close view (at most one more viewpoint if still unsure), then go:
   `robot plan "goto <side> x y z; set <side>_gripper 0"`. Never creep in with small moves.
4. Gripper closed below ~5 % = missed (nothing between the jaws): reopen, one corrected goto, close. A grasp = ~4–6 commands.

**Collisions:** `refused: …` (plan check) = that move would hit; `BLOCKED <joint>` = it hit something and the arm is held: back off, never push.
`handover on|off` lets the arms touch: only for real hand-overs.
**Joints:** pan + = toward the arm's own right · wrist_roll turns the jaws (one move, e.g. `set <side>_wrist_roll 90`) · gripper % (0 closed, ~25 open).
**Cameras:** scene camera faces the arms: right (orange) arm on image-left. A wrist cam cannot see under its own jaws.
**Grasp:** straddle the object's body, one jaw each side, close. HOLDING = gripper stopped well above its target: you have it.

**Pace:** workshop prototype, trained people watching; robotd ramps every move. Be decisive, never ask people to rearrange things.
Big moves in one command are fine. Never release torque while raised. Keep going until done; ask only if the task itself is ambiguous.
**Sandbox:** "robotd is not running" inside a sandbox = rerun with full access. **Develop:** `SO101_SIM=1` (sim + test_sim.py). No Slack.
