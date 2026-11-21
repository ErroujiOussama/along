Grasp the held object without moving it. You drive the two real SO-101 arms through robotd; AGENTS.md has the loop, commands and pace rules.
A person holds an object up in front of the arms. Grab it with the better-placed arm (closer one); it must not move, in position or angle.
Recipe that worked (2026-11-21, toothbrush, 15 steps / 2 min): `recall ready <side>` + `set <side>_gripper 20` in one step → find the object
in the scene camera → approach in free air in big moves → `set <side>_wrist_roll <angle>` so the jaws match it → final ≤ 1 cm nudges until it is
centered between the jaws (both jaws touch at once) → `set <side>_gripper 0`. Then keep the arm at its last position: no tracking, no re-adjusting.
Done = jaws closed on it and the arm holding still; report in two lines: steps used and what happened.
