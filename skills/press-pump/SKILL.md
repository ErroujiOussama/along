---
name: press-pump
use_when: Use when asked to depress a bottle pump to dispense liquid.
args: target: pump to press; side: left|right|auto
direct: false
---
## Goal
Depress the actual pump head enough to dispense, then lift clear so it returns.
## Strategy
Use the requested arm, or the nearer arm, and identify the movable pump head separately from the bottle and receiving palm in fresh scene and wrist images. Lift clear, approach above the head with a closed gripper through a raised intermediate pose if needed, then check alignment before descending; move the arm while keeping the bottle in place. Estimate contact height from the table and bottle proportions, use goto with the head pixel and that height for alignment, and reassess height and approach if unreachable instead of repeating a clamped target. Use press on the verified head with about 4 cm depth, then lift clear and check the result even if press exits before withdrawing. Escalate to codex only after fast attempts clearly fail.
## Success
The tip contacts the head, the head moves down and returns after withdrawal, and liquid dispenses or the person confirms success. A still image without visible liquid cannot disprove dispensing. If contact missed, correct alignment before another attempt.
