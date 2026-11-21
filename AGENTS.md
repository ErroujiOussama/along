# Agent guide

You drive two real SO-101 arms (left = white, right = orange). The task comes in your prompt; README.md is the manual.
robotd (systemd user service) owns the arms, the cameras and the one shared state: use ONLY the `robot` CLI or `from so101 import Robot`.
Never open serial ports or cameras yourself, never start a second robotd.

**Loop:** `robot step "<one small action>"`, then open snap_all.jpg (left wrist | right wrist | scene, labelled). Decide, repeat.
`robot step` alone = just look. Its ~4 lines give per arm the joints, gripper measured vs target + HOLDING, torque and tip xyz (m).
Do not write your own step helper and do not open full-res images by default: `robot snap <cam>` only when you need detail.

| joint (deg) | + means | notes (observed on hardware 2026-11-21) |
|---|---|---|
| shoulder_pan | toward the arm's own right (left arm: inward, toward the right arm) | verified on the left arm; confirm the right arm with a 3° move |
| shoulder_lift | upper arm tilts forward | rest -102 (folded back): + lifts the gripper off the table |
| elbow_flex | folds (− extends / raises the tip) | rest 96 = folded |
| wrist_flex | tips down | |
| wrist_roll | – | leave it |
| gripper (%) | opens | 0 = closed, ~20 = open enough for a pouch, 100 = fully open |

**Cameras:** a wrist camera cannot see under its own jaws. Use the fixed scene camera (C920) for alignment and clearance; `robot cams` = which deliver.
Camera exposure is on auto; change it (`robot cam <cam> ...`) only if an image is unusable, and say so.
**Grasp:** jaws ~20 open, straddle the object's body (one jaw each side; thin straps/edges slip), lower until the tips are beside it,
`set <side>_gripper 0`. If the measured gripper stays well above its target (e.g. ~18), the step prints HOLDING: you have it.
Thin/soft things (fabric) can close fully without HOLDING: lift a few degrees and check the scene camera before calling it a miss.
Lift slowly, then confirm in snap_all.jpg that it is clear of the table.

**Safety:** steps ≤ 5°; lift before any sideways move; first `goto` slow and small to confirm signs; never release torque while raised.
Humans may hand-guide (torque off) or use the panel at the same time: read the state before each move. If hands are in view, stop and wait.

**Sandbox:** robot commands use a Unix socket. If the first call says "robotd is not running" inside a sandbox, it is the sandbox:
rerun with escalated permissions / full access before concluding robotd is down.
**Develop** without hardware: `SO101_SIM=1` (sim robotd + test_sim.py). Do not use Slack. Keep README.md short and current.
