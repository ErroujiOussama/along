---
name: press-pump
use_when: Use when positioning over or pressing a soap or lotion pump.
args: target: pump to press; side: left|right|auto; action: position|dispense
direct: false
---
## Steps
1. From fresh images, identify {target}'s actual clear head and {side}'s contact tip. Use robot step goto {side} {current_x} {corrected_y} {clearance_z} to align laterally while above the head. On the orange/right arm, +y moved the tip image-right about 15 px/cm; correct y by (pump_u - tip_u)/1500 metres. Use the current reachable x when already close above the pump.
2. check: "Is the contact tip centered over the actual pump head? Give tip/head pixels, clearance, and corrected approach if needed." Correct remaining alignment before descending. If forward alignment is uncertain, use codex for localization and a reachable approach.
3. If {action} is position, stop above the head. Otherwise press: object = corrected pump scene pixel u v + height_cm of the contact point, depth_cm = 4.
4. check: "Did the head move down under contact and rebound after lifting? Is liquid visible or did the person confirm dispensing?" If still contacting, robot step goto {side} {contact_x} {contact_y} {contact_z + 0.06} to lift, then check rebound.
## Checks
- Compare fresh images before contact, at contact, and after lifting; contact alone does not establish dispensing.
- Person confirmation establishes success even when liquid is not visible.
## Pitfalls
- Move the arm to the pump when requested; do not substitute asking the person to move the bottle.
- Use fresh pump pixels after bottle movement; old coordinates missed this small clear target.
- Treat image-derived heights as estimates: the initial estimate ran about 5 cm high. The successful bottle's contact point was near base-frame z 0.14 m, with a 4 cm commanded stroke; adapt to the actual bottle.
- Check lateral alignment after approach; a geometrically localized point can still leave the contact tip visibly offset.
- Respect controller workspace limits; unreachable forward targets were rejected or clamped and missed the head.
- A press helper can report failure after actual contact; inspect depression/rebound and person feedback before retrying.
