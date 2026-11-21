# Scene onboarding / calibration (first run 2026-11-21)

Idea: a frontier agent with arm access calibrates a new setup by itself; no tags, no tape, nothing hard-coded.
Run: clear Codex (GPT-6.2 Sol), paste prompts/calibrate.md. Output: scene.json (+ scene.html viewer). collide.py reads scene.json.

First run: 6 min, unattended. What it did: moved both tips through 13 poses visible in the scene camera and fitted the scene
camera pose + right-base xy/yaw to the tip projections (2.6 px RMS); table points back-projected to the table plane;
mounted wrist cameras from features across poses; everything else (laptops, bottles, tripod, cables) as boxes/capsules by eye.

| scene.json item | used for | quality |
|---|---|---|
| right_base_pose (0.0, -0.213, yaw 0.06°) | arm-vs-arm collision | ±2.5 cm / 5° (claimed); not checked yet |
| scene_camera pose + focal | collision box; later: scene pixel → table xy (one `goto` instead of many nudges) | ±5 cm; rest-pose tips project 27–36 px off |
| mounted (wrist) cameras, per arm, gripper frame | self + arm-arm collision | ±1.8 cm; left and right placed differently |
| table_z -0.02 | table check | inherited from collide.py default, not measured |
| objects (≈50) | obstacles | rough boxes; most far from the arms; cables/pouch are not obstacles |

Make it more useful next time:
- verify instead of trusting: tip-to-tip test for the base offset (both tips to one predicted world point), touch the table at 3 points for table_z
  (block detection = touch sensor), move a tip 3 cm from each mounted camera estimate and look.
- fix the rest-pose mismatch: include rest/ready poses in the camera fit; fit lens distortion.
- ask only for what collision needs (arms, mounted cams, things within reach); mark graspable vs fixed objects.
- emit uncertainties the checker can use as margins; re-run a short `--check` sweep when the setup may have moved.
