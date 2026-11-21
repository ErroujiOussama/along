Grasp the held object without moving it. You drive the two real SO-101 arms through robotd; AGENTS.md has the loop, commands and pace rules.
A person holds an object up in front of the arms. Grab it with the better-placed arm (closer one); it must not move, in position or angle.
Be bold and fast: one `robot step` to look, then estimate the object's xyz and angle (scene camera pose in scene.json, wrist camera),
write the whole approach as ONE plan and run it with `robot plan "..."`: e.g. `recall ready right; set right_gripper 25;
set right_wrist_roll <angle>; goto right <x-4cm> <y> <z>; goto right <x> <y> <z>`. Then at most 1–2 small `nudge` steps to center it
between the jaws (both jaws touch at once), `set <side>_gripper 0`. Replan only if a result shows it is going to fail.
After the grasp keep the arm at its last position: no tracking, no re-adjusting.
Done = jaws closed on it and the arm holding still; report in two lines: steps used, time, and what happened.
(First run 2026-11-21, toothbrush: 15 steps / 2 min with nudges only.)
