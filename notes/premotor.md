# Ideas to borrow from Premotor / DeskCortex (later, when experiments show where we struggle)

Source: github.com/ErroujiOussama/Premotor- (colleague, MuJoCo sim of the same bimanual SO-101 desk rig, 2026-11-21).
Concept: skills are scripted/precomputed; a task request only picks the skill + object (decision → first motor command ~4 ms
vs ~1.3 s planning on demand). Our CLI is the layer below: the agent plans every 5° itself (pouch pick: ~30 steps, ~4 min).
Skills would sit on top of robotd/so101.py; the CLI stays the fallback for new or failed tasks.

| Take | From | Gives us | Adapt |
|---|---|---|---|
| Skill structure | `premotor.py` `MotorProgram`, `_plan_one`, `_deliver` | transit → descend → close → check_grasp → lift → present/set down → back off → ready; event markers trigger check + one retry | execute via robotd (we already run multi-point paths for goto) |
| Per-object grasp table | `GRASP = {kind: pitch, open, dz, yaw_aligned}` | approach angle, opening, height offset per object kind | values are for their soft TPU gripper; re-measure (pouch: ~30 open, neck-first) |
| IK + collisions | `desk_sim.py` `Kinematics` (`ik_best`, `desk_clearance`, `self_collision`) + MuJoCo model | IK with an approach angle, several seeds, folded-elbow penalty; table/self/arm-arm checks | radians ↔ our degrees + zero offsets (their `twin.py` is not in the repo: fit by matching FK poses); set `desk_config.json` to our real layout |
| Safety gate | `safety.py` `check_traj` | every path checked before it runs: limits, speed, table, self-collision, arm gap ≥ 7 cm | small, uses the row above; force monitor would use servo load (readable, but useless for thin fabric in the gripper) |
| Object memory | `premotor.py` `DeskMemory` / `ObjState` | per-object pose, version, visible/remembered; plans invalidated when the object moves | needs real perception (theirs uses sim segmentation) |

Skip: `app.py`, `voice.py`, `cortex.py` (our LLM agent parses), `benchmark.py`, `DeskPerception`.
We would build: scene-camera pixel → table xy calibration (touch ~4 points per arm, homography) + detector (colour/dark-blob
mask first, GroundingDINO later), the unit mapping, a small executor with markers and retry.
Order: unit mapping + kinematics/safety (improves ik/nudge on its own) → camera calibration → pick/place skills → memory.
