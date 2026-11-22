---
name: press-pump
use_when: Use when asked to press a pump, dispenser or button from above (soap, sanitizer, lotion)
args: target: what to press; side: left|right|auto; depth_cm: 4, harder 6-8
direct: false
---
## Steps
1. Find the top of {target} in the scene image (u, v) and its top height h (m above the table; vision estimates run
   ~5 cm high: a pump bottle top is usually 0.13-0.15). side auto = the arm nearer to it.
2. step: `set <side>_gripper 0; goto <side> px u v h+0.03` (the engine locates the pixel; past the reach = clamped).
3. check: "Is the <side> tip right above the {target} top in the scene image? how many px left/right?" If off: one
   goto fixing y only (+y moves the tip image-right, ~15 px per cm at pump height). At most 2 fixes, then go on.
4. press: object = the pump top (u, v, height_cm = h in cm), depth_cm (it approaches 4 cm above, pushes, comes back).
5. check: "Did the tip touch the {target} top?" Missed: back to 3 once; else say "Here you go."

## Checks
- Alignment: compare the tip in the scene image with the pump head, not the printed xyz (x from the scene camera is weak).
- Contact: the tip sits on the pump head at the bottom of the press; dispensing itself is rarely visible.

## Pitfalls
- Take x/y from `robot locate`, never from `along look`: its xyz guesses are uncalibrated (it said x 0.48, ~17 cm off).
- Out of reach: move the arm as far as it goes and align in y; never ask the person to move the bottle (they expect
  the robot to come to it).
- Fix alignment with one decisive goto from the measured px offset, not nudges: each nudge costs a step and creeps.
