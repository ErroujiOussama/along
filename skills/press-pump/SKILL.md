---
name: press-pump
use_when: Use when asked to depress a bottle pump to dispense liquid.
args: target: pump to press; side: left|right|auto
direct: false
---
## Goal
Depress the actual pump head enough to dispense, then lift clear so it returns.
## Strategy
Use the requested arm, or the nearer arm. Identify the clear movable head separately from the bottle body in fresh images. Approach above it with a closed gripper, then check the contact tip against the head before descending. Correct lateral alignment using fresh feedback; pixel localization depends strongly on the supplied height.
Use press with the head pixel, estimated contact height, and about 4 cm depth. Lift clear after contact and check the result, including when press exits before withdrawing. If a target exceeds the workspace, reassess the height and reachable approach from fresh images; do not repeat a clamped target. Approach a distant head through a raised intermediate pose, then extend and align before lowering; this gives room to change posture. Move the arm to align, keeping the bottle in place. Escalate to codex only after fast attempts clearly fail.
## Success
The tip contacts the head, the head moves down and returns after withdrawal, and liquid dispenses or the person confirms success. A still image without visible liquid cannot disprove dispensing. If contact missed, correct alignment before another attempt.
## Lessons
For the orange/right arm in this scene view, increasing y moved the tip image-right, roughly 12–15 pixels per centimetre; use this as an initial correction and verify visually.
Visual pump-height estimates ran about 5 cm high; an overestimated height placed the localized target too far forward. Check against the table and bottle proportions before declaring it unreachable.
A tip centered slightly above the head in the scene image can be ready for a vertical press; distinguish the vertical gap from lateral error.
A descent can contact and dispense even if the controller reports the tip stopped about 1–2.5 cm short; lift clear and assess the head and person's confirmation before retrying, since pump resistance can prevent reaching the commanded depth.
