[refine] The episode above SUCCEEDED: the person confirmed it. Do not move the robot: no `robot`, no `along` commands
that act (reading files is all you need). Your only job now: keep what this episode taught as a reusable skill, so next
time the fast path does it in a few steps without you.

Skill used in this episode: {used}
Current skills (skills/<name>/SKILL.md):
{index}

Ignore ~/.codex/skills (Codex's own skills, a different format); this format is below. First read the relevant files: `ls skills/`, the SKILL.md of the skill that was used (if any) and of any skill that
covers a similar task. Re-read a file right before you edit it. Then do the first that applies:
1. Patch the skill that was used, if the episode shows a better strategy or a sharper goal / success criterion.
2. Patch a broader existing skill that covers this task class.
3. Create one new skill (skills/<name>/SKILL.md) if no skill covers this task class.
4. Save nothing: if nothing actually worked by a repeatable method (luck, the person did it, one-off), or the skill
   already says it all.

A skill is NOT a script. Two readers use it: the fast model (plans its own steps from the live images each time) and
Codex (reads it with `along skill`, runs robot commands itself). Keep it general enough for both; it needs
to understand the objective and how to get there, not a fixed sequence. Write it like a note from an expert to a
capable colleague: what the goal really is, what success looks like, and the approach in brief. Short and concise: the
reader is as capable as you; it needs the goal and the success criteria, not a list of mistakes to avoid. No lessons:
not the mistakes made in this episode and how they were recovered (if it figured it out itself, it will again), not
camera/arm calibration (px per cm, height biases, axis signs), not how the robot tools behave or fail.

Writing rules:
- Goal: the real objective in one or two lines (e.g. "the pump head goes down ~4 cm under the tip and comes back up",
  not "run press").
- Strategy: the approach in a few sentences, with the decisions that matter (which arm, where to come from, when to
  look again, what to do when out of reach). Mention tools by name where useful (goto <side> px u v z, press, check),
  but don't number a mandatory sequence or script the checks.
- Success: how to tell from the images (or the person) that it worked, and what to do if it didn't.
- codex is the fallback after the fast attempts clearly failed, never the plan.
- Don't encode workarounds for errors or refusals of Al's tools (out of reach, blocked, "tip ended short", refused
  gotos): those get fixed in code. Not even one line.
- Name by task class, not instance: "press-pump", "hand-over-tool", never "press-soap-at-0.31". Lowercase, dashes.
- Never literal coordinates from this episode, and nothing that only holds for this bottle, this spot or this
  person: if it would not help the same task in a different setup, leave it out.
- A skill must not keep growing. Hard limits: Strategy <= 3 sentences, no Lessons section, the whole file <= 15 lines.
  A patch rewrites the skill as a whole: merge the new insight into an existing sentence, replace what it
  supersedes, delete what turned out wrong or is no longer needed (an old Lessons section goes entirely). A patch that
  only appends is wrong.
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

Tools the fast model has (so the strategy can name them):
- built-ins: hold, release, turn <deg>, tilt <deg>, press, open, park, reset, stop,
  answer (just talk), codex (hand the rest to the slow agent), none
- step: robot step cmds, ';'-chained: goto <side> x y z (>= 2 cm), goto <side> px u v z (scene pixel at height z: the
  engine locates it; the fast model cannot run `robot locate` itself), goto <side> base u v z (u v = where the object
  stands on the table: x, y from that, z as given; accurate for anything standing on the table), move <side>_<joint> <deg> (>= 5), set <side>_gripper
  <0-100>, recall ready|rest <side>, nudge
- press: object = scene pixel u v + height_cm of the point to press, depth_cm
- check: "<question about fresh images>" -> the model looks again and returns corrected remaining steps (closed loop)
- skill:<other> with args (nests <= 3 deep, no cycles)
- say "<line>" on any step: what Al says when that step is done.

End with exactly one line: `created <name>: <what changed>`, `patched <name>: <what changed>` or `nothing`.
<what changed> is spoken to the person, who says whether to keep it: one plain sentence, <= 15 words, no jargon
(e.g. "press pumps from straight above with the nearer arm").
