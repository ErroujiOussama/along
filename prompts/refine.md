[refine] The episode above SUCCEEDED: the person confirmed it. Do not move the robot: no `robot`, no `along` commands
that act (reading files is all you need). Your only job now: keep what this episode taught as a reusable skill, so next
time the fast path does it in a few steps without you.

Skill used in this episode: {used}
Current skills (skills/<name>/SKILL.md):
{index}

Ignore ~/.codex/skills (Codex's own skills, a different format); this format is below. First read the relevant files: `ls skills/`, the SKILL.md of the skill that was used (if any) and of any skill that
covers a similar task. Re-read a file right before you edit it. Then do the first that applies:
1. Patch the skill that was used, if the episode shows a better step, a missing check or a pitfall.
2. Patch a broader existing skill that covers this task class.
3. Create one new skill (skills/<name>/SKILL.md) if no skill covers this task class.
4. Save nothing: if nothing actually worked by a repeatable method (luck, the person did it, one-off), or the skill
   already says it all.

Writing rules:
- The skill turn already gets fresh scene + wrist images: step 1 acts on them (e.g. a `goto <side> px u v z` above the
  target). Use check only where precision matters: after the approach, after the risky step.
- Keep the numbers this episode measured that generalise: estimate biases ("height estimates ran ~5 cm high"), the
  sign and scale of corrections ("+y moves the tip image-right, ~15 px per cm"), working depths. These are the point.
- Keep the person's corrections as rules ("they said: move the arm, not the bottle").
- Don't encode workarounds for errors that came from Al's tools (refusals of its own press/goto helpers): those get
  fixed in code; at most one line saying what the error looked like.
- Name by task class, not instance: "press-pump", "hand-over-tool", never "press-soap-at-0.31". Lowercase, dashes.
- Steps take parameters (from `robot locate`, a check, the args), never literal coordinates from this episode. Numbers
  that generalise (offsets, px per cm, heights of a class of object) are fine with a word on where they come from.
- Each pitfall = a general rule + one clause of why. Put it at the step it affects, or under ## Pitfalls; edit it in
  place: no "UPDATE:", no dated notes, no appended episode stories. The same lesson twice = one rule.
- Short: at most ~35 lines per SKILL.md. Delete what turned out wrong.
- Edit only files under skills/ (never usage.json). Nothing else in the repo.

SKILL.md format:
---
name: <name>
use_when: Use when ... (<= 90 chars; the router sees only this line)
args: <arg>: <meaning>; <arg>: <meaning>   (one line, e.g. "target: what to press; side: left|right|auto")
direct: false   (true only if the steps run as written with no model turn: no locate, no check)
---
## Steps
1. ... numbered; {{arg}} for an argument.
## Checks
- how to tell from the images that a step worked.
## Pitfalls
- rule + why.

Step vocabulary (the engine runs these; nothing else):
- built-ins: hold, release, turn <deg>, tilt <deg>, press, open, park, reset, stop,
  answer (just talk), codex (hand the rest to the slow agent), none
- step: robot step cmds, ';'-chained: goto <side> x y z (>= 2 cm), goto <side> px u v z (scene pixel at height z: the
  engine locates it; the fast model cannot run `robot locate` itself), move <side>_<joint> <deg> (>= 5), set <side>_gripper
  <0-100>, recall ready|rest <side>, nudge
- press: object = scene pixel u v + height_cm of the point to press, depth_cm
- check: "<question about fresh images>" -> the model looks again and returns corrected remaining steps (closed loop)
- skill:<other> with args (nests <= 3 deep, no cycles)
- say "<line>" on any step: what Al says when that step is done.

End with exactly one line: `created <name>`, `patched <name>` or `nothing`.
