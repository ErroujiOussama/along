Pick up the {OBJECT} on the table with one of the two SO-101 arms. No demonstration, no prior pose: work it out from the cameras.

Read AGENTS.md first and follow it (the `robot step` loop, recall ready + nudge, joint directions, grasp recipe, safety rules). Start with `robot step` (just look) and choose the arm and approach from what you see.
Work in small steps; after each one view snap_all.jpg before deciding the next. Use the scene camera to check alignment and clearance.
If human hands are in view, pause until they are gone. If something looks unsafe or unclear, stop the arm and ask.

Done = the object is held clear of the table and a snap_all.jpg (scene) confirms it; the step output shows HOLDING for that arm (thin/soft objects may not trigger it — then the image decides). Then hold still and report.

For every step write one line: what you see → what you do (and why). Finish with the number of steps and grasp attempts.
