# Agent guide

You drive two real SO-101 arms (left = white, right = orange). The task comes in your prompt; README.md is the manual.
Use ONLY the `robot` CLI (or `from so101 import Robot`): robotd owns the arms and cameras. Never open serial ports or cameras, never start a second robotd.

**Look around, plan once, follow through. No nudges** (`nudge` is disabled; every look-and-adjust round costs ~10 s).
1. Look: `robot scan right` (or `left`, the arm nearer the object): its wrist cam from 4 poses near ready -> scan.jpg
   (each view labelled with the camera xyz/direction, + scene camera). `robot locate u v [z]` turns a scene-image pixel
   (800x450) into xyz in both arm frames (m: x forward, y arm's left, z up). Fix the object's xyz and angle confidently.
2. Run the whole motion as ONE `robot plan "set right_gripper 25; set right_wrist_roll 90; goto right x y z+0.05; goto right x y z"`.
   It is checked against the collision model on the sim first (goto/recall detour around obstacles); only then it runs for real.
3. Check once. Off by > 1 cm: one corrective `goto`, then close: `robot step "set right_gripper 0"`. A grasp = ~4–6 commands.

**Collisions:** `refused: …` (plan check) = that move would hit; `BLOCKED <joint>` = it hit something and the arm is held: back off, never push.
`handover on|off` lets the arms touch: only for real hand-overs.
**Joints:** pan + = toward the arm's own right · wrist_roll turns the jaws (one move, e.g. `set right_wrist_roll 90`) · gripper % (0 closed, ~25 open).
**Cameras:** scene camera faces the arms: right (orange) arm on image-left. A wrist cam cannot see under its own jaws.
**Grasp:** straddle the object's body, one jaw each side, close. HOLDING = gripper stopped well above its target: you have it.
Thin things close without HOLDING: lift 1–2 cm and check the object follows in the scene camera.

**Pace:** workshop prototype, trained people watching; robotd ramps every move. Be decisive, never ask people to rearrange things.
Big moves in one command are fine. Never release torque while raised. Keep going until done; ask only if the task itself is ambiguous.
**Sandbox:** "robotd is not running" inside a sandbox = rerun with full access. **Develop:** `SO101_SIM=1` (sim + test_sim.py). No Slack.
