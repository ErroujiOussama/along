[refine] The episode in the log below SUCCEEDED: the person confirmed it. Do not move the robot (no `robot`, no acting
`along` commands). Your job: keep what it taught as a skill, so next time the fast model does it in a few steps.

Skill used in this episode: {used}
Current skills (skills/<name>/SKILL.md):
{index}

Read `ls skills/` and the used or closest SKILL.md first. Then patch the used skill, or create one new skill if none
covers this task class, or save nothing if the skill already says it or nothing repeatable worked.

A skill says what the task is, not how to do it step by step: the reader is as capable as you and sees live images.
Write only the goal, what success looks like and, if the episode showed one, the single decision that mattered (e.g.
"the nearer arm", "from straight above"). No procedures: no checks, looks, alignment, approach stages or
verification steps; no mistakes from this episode; no coordinates, calibration or tool behavior; nothing that only
holds for this bottle, spot or person. Name by task class ("press-pump"), lowercase with dashes.
A patch rewrites the whole file and makes it shorter or keeps it as short. Hard limit: 14 lines. Edit only skills/.

Format:
---
name: <name>
use_when: Use when ... (<= 90 chars; the router sees only this line)
args: <arg>: <meaning>; <arg>: <meaning>
direct: false
---
## Goal
<one or two lines>
## Success
<one or two lines: how to tell it worked>
## Strategy (optional)
<one sentence>

Tools the fast model has, for naming: press (goes above the point, down, back up by itself), hold, release, turn, tilt,
goto <side> px|base u v z, codex (fallback only).

End with exactly one line: `created <name>: <what changed>`, `patched <name>: <what changed>` or `nothing`.
<what changed> is spoken to the person, who says whether to keep it: one plain sentence, <= 15 words.
