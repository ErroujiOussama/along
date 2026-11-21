# Agent guide

You drive two real SO-101 arms (left = white, right = orange). The task comes in your prompt; README.md is the manual.
robotd (systemd user service) owns the arms, the cameras and the one shared state: use ONLY the `robot` CLI or `from so101 import Robot`.
Never open serial ports or cameras yourself, never start a second robotd.

**Loop:** `robot step "<action>"`, then open snap_all.jpg (left | right wrist on top, scene camera large below). Decide, repeat.
`robot step` alone = just look. **Start** with `robot step "recall ready <side>"`: from rest in one call to tip 10 cm above the table
(x 0.22, y 0), gripper pointing down. Then move with `goto <side> x y z` (m; x forward, y arm's left, z up; straight line,
keeps the gripper orientation), e.g. `robot step "goto right 0.28 0.03 0.06"`. `robot ik <side> x y z` = dry run (moves nothing).
Joint moves (`move right_wrist_flex 10`) for the rest. **No creeping:** gotos must move ≥ 2 cm and joint moves ≥ 5° (gripper
and roll are free); smaller ones are refused, except ONE 1–2 cm goto fix after each bigger goto. Don't nudge toward the object.
**Estimate before you move** (thinking is cheap, wrong moves are not): find the object in the scene image and get its xyz with
`robot locate u v z` (pixel in the 800x450 scene tile; z = a height you know: table -0.02, or the height of something it rests on /
lines up with). The scene camera looks along x, so its x (forward) is the weak axis: cross-check x with the wrist view and with
the arm tips printed by `robot step` (known xyz you can compare in the image). Only move once the numbers agree.
**Approach in two stages:** first a goto ~5 cm short of the grasp point (above or in front), jaws open and turned, where the wrist
cam still sees the object beside the jaws. Re-estimate from that view + the scene camera, then one goto to the grasp point
(+ the one small fix if needed), close.
Each step prints ~4 lines: per arm the joints, gripper measured vs target + HOLDING, torque, tip xyz (m) and how far the tip moved.
Do not write your own step helper and do not open full-res images by default: `robot snap <cam>` only when you need detail.

| joint (deg) | + means | notes (observed on hardware 2026-11-21) |
|---|---|---|
| shoulder_pan | toward the arm's own right (left arm: inward, toward the right arm) | verified on both arms |
| shoulder_lift | upper arm tilts forward | from rest (-102) + raises the gripper; once unfolded (≳ -40) + LOWERS the tip |
| elbow_flex | folds (− extends / raises the tip) | rest 96 = folded |
| wrist_flex | tips down | |
| wrist_roll | turns the jaws | free; turn it in one move (e.g. `set right_wrist_roll 90`) to align the jaws with the object |
| gripper (%) | opens | 0 = closed, ~20 = open enough for a pouch, 100 = fully open |

**Cameras:** the scene camera faces the arms from the front: right (orange) arm on image-left, left (white) on image-right;
right-arm pan − moves its gripper image-right. A wrist camera cannot see under its own jaws (a dark blob between them can be the object at a steep angle). Use the fixed scene camera (C920) for alignment and clearance; `robot cams` = which deliver.
Camera exposure is on auto; change it (`robot cam <cam> ...`) only if an image is unusable, and say so.
**Grasp:** jaws ~20 open, straddle the object's body (one jaw each side; thin straps/edges slip), lower until the tips are beside it,
`set <side>_gripper 0`. If the measured gripper stays well above its target (e.g. ~18), the step prints HOLDING: you have it.
**Miss:** gripper measured below ~5 % with no HOLDING = nothing between the jaws (anything ≥ 5 mm thick stops it earlier):
reopen, back off ≥ 2 cm, one corrected approach. Don't report a grasp without HOLDING (only fabric-thin things close fully).
Lift slowly, then confirm in snap_all.jpg that it is clear of the table.

**Pace:** this is a workshop prototype run by trained people who watch the arms; robotd already ramps speed and clamps joint ranges.
Think first, then act decisively; do not ask for permission or reassurance, and never ask people to rearrange things: move the robot instead.
Big moves in free air are fine in one step (e.g. a 90° roll, 20° of lift). Lift before a sideways move near the table. Never release torque while raised.
Hands in view are normal (hand-overs, people adjusting things): just don't drive the jaws into a hand. Ask only if the task itself is ambiguous.
Keep going until the task is done: do not end your turn to report progress or ask for confirmation. End it only when done,
or when the task itself is ambiguous. A grasp should take ~5–8 steps: `recall ready`, the two-stage approach, close, check HOLDING.

**Sandbox:** robot commands use a Unix socket. If the first call says "robotd is not running" inside a sandbox, it is the sandbox:
rerun with escalated permissions / full access before concluding robotd is down.
**Develop** without hardware: `SO101_SIM=1` (sim robotd + test_sim.py). Do not use Slack. Keep README.md short and current.
