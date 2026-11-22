[refine] The episode above SUCCEEDED: the person confirmed it. Do not move the robot: no `robot`, no `along` commands
that act (reading files is all you need). Your only job now: keep what this episode taught as a reusable skill, so next
time the fast path does it in a few steps without you.

Skill used in this episode: {used}
Current skills (skills/<name>/SKILL.md):
{index}

Ignore ~/.codex/skills (Codex's own skills, a different format); this format is below. First read the relevant files: `ls skills/`, the SKILL.md of the skill that was used (if any) and of any skill that
covers a similar task. Re-read a file right before you edit it. Then do the first that applies:
1. Patch the skill that was used, if the episode adds a better strategy, a lesson or a number.
2. Patch a broader existing skill that covers this task class.
3. Create one new skill (skills/<name>/SKILL.md) if no skill covers this task class.
4. Save nothing: if nothing actually worked by a repeatable method (luck, the person did it, one-off), or the skill
   already says it all.

A skill is NOT a script. The fast model that reads it plans its own steps from the live images each time; it needs
to understand the objective and how to get there, not a fixed sequence. Write it like a note from an expert to a
capable colleague: what the goal really is, the strategy that worked and why, what success looks like, and the lessons.

Writing rules:
- Goal: the real objective in one or two lines (e.g. "the pump head goes down ~4 cm under the tip and comes back up",
  not "run press").
- Strategy: the approach in a few sentences, with the decisions that matter (which arm, where to come from, when to
  look again, what to do when out of reach). Mention tools by name where useful (goto <side> px u v z, press, check),
  but don't number a mandatory sequence or script the checks.
- Success: how to tell from the images (or the person) that it worked, and what to do if it didn't.
- Lessons: numbers this episode measured that generalise (estimate biases like "heights ran ~5 cm high", the sign and
  scale of corrections like "+y moves the tip image-right, ~15 px per cm", depths) and the person's corrections ("move
  the arm, not the bottle"). Each = a general rule + one clause of why; edit in place, no "UPDATE:", no dated notes,
  no episode stories; the same lesson twice = one rule.
- codex is the fallback after the fast attempts clearly failed, never the plan.
- Don't encode workarounds for errors from Al's own tools (refusals of its press/goto helpers): those get fixed in
  code; at most one line on what the error looked like.
- Name by task class, not instance: "press-pump", "hand-over-tool", never "press-soap-at-0.31". Lowercase, dashes.
- Never literal coordinates from this episode. Short: at most ~30 lines. Delete what turned out wrong.
- Edit only files under skills/ (never usage.json). Nothing else in the repo.

SKILL.md format:
---
name: <name>
use_when: Use when ... (<= 90 chars; the router sees only this line)
args: <arg>: <meaning>; <arg>: <meaning>   (one line, e.g. "target: what to press; side: left|right|auto")
direct: false
---
## Goal
## Strategy
## Success
## Lessons

Tools the fast model has (so the strategy can name them):
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
