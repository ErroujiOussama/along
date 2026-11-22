"""Al's learned skills: skills/<name>/SKILL.md (frontmatter + ## Goal / ## Strategy / ## Success / ## Lessons), stdlib only.

Frontmatter (between `---` lines, `key: value`): name, use_when (<= 90 chars, "Use when ..."), args (one line, e.g.
"target: what to press; side: left|right|auto"), direct (true = run the steps as written, no model turn; default false).
The router sees index_text() (one line per skill); a picked skill's SKILL.md goes to the model, which returns steps.
Usage stats in skills/usage.json; before a refinement, snapshot() copies every SKILL.md to .along_skills_hist/<id>/,
changes_since(id) says what the refinement created/patched, undo(name) puts the previous version back.
"""
import fcntl, json, os, re, shutil, time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = f"{HERE}/skills"
HIST = f"{HERE}/.along_skills_hist"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

BUILTINS = [
    {"name": "hold", "use_when": "Use when a person holds something out (or will): reach out, jaws open, close once it is in",
     "args": "side: left|right|auto; what: the object; object: where it is in the scene image"},
    {"name": "release", "use_when": "Use when told to let go: side opens and goes back to ready", "args": "side"},
    {"name": "turn", "use_when": "Use when the held object should rotate: wrist roll by deg (90 = quarter turn)", "args": "side; deg"},
    {"name": "tilt", "use_when": "Use when the held object should tip: wrist flex by deg (+ tips down)", "args": "side; deg"},
    {"name": "press", "use_when": "Use when a point (button, pump) is to be pushed straight down and released",
     "args": "side; object: the point; depth_cm (default 4, harder 6-8)"},
    {"name": "open", "use_when": "Use when both grippers should open (let go of everything)", "args": ""},
    {"name": "park", "use_when": "Use when the arms are done: empty arms go to rest", "args": ""},
    {"name": "reset", "use_when": "Use when everything should stop and both arms go to rest", "args": ""},
    {"name": "stop", "use_when": "Use on stop / wait / halt / no: stop what runs, arms stay where they are", "args": ""},
    {"name": "step", "use_when": "Use for a short direct move: robot step cmds (goto, move, set, recall, nudge), ';'-chained",
     "args": "step: e.g. goto right 0.28 0.03 0.06; set right_gripper 20"},
    {"name": "check", "use_when": "Use to look again mid-plan: fresh images + a question, returns corrected remaining steps",
     "args": "question: e.g. is the orange tip right above the pump head?"},
    {"name": "answer", "use_when": "Use when only talk is needed: questions about the scene, the arms, greetings", "args": "say"},
    {"name": "codex", "use_when": "Use for multi-step tasks, grasps from the table, anything else (the slow agent)",
     "args": "codex_prompt; codex_fresh"},
    {"name": "none", "use_when": "Use when the speech is not addressed to Al (chatter, other conversations)", "args": ""},
    {"name": "skill", "use_when": "Use when a learned skill fits: skill:<name> plans from its SKILL.md (nests <= 3 deep)",
     "args": "the skill's own args"},
]


# ---------------------------------------------------------------- reading
def parse(text):
    """SKILL.md text -> (meta dict, body). A tiny frontmatter parser: `key: value` lines between the first two `---`."""
    meta, body = {}, text
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                body = "\n".join(lines[i + 1:]).lstrip("\n")
                break
            k, sep, v = ln.partition(":")
            if sep and k.strip():
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                meta[k.strip()] = v
        else:
            meta, body = {}, text                        # no closing ---: no frontmatter
    meta["direct"] = str(meta.get("direct", "false")).lower() in ("true", "yes", "1")
    return meta, body


def _path(name, root=None):
    return f"{root or SKILLS}/{name}/SKILL.md"


def _read(path):
    try:
        return open(path).read()
    except OSError:
        return None


def names(root=None):
    root = root or SKILLS
    try:
        return sorted(n for n in os.listdir(root) if NAME.match(n) and os.path.isfile(_path(n, root)))
    except FileNotFoundError:
        return []


def load(name):
    """dict(meta..., body, path) or None (unknown or malformed name)."""
    name = (name or "").removeprefix("skill:").strip()
    if not NAME.match(name):
        return None
    text = _read(_path(name))
    if text is None:
        return None
    meta, body = parse(text)
    return {**meta, "name": meta.get("name") or name, "body": body, "path": _path(name)}


def index():
    """Metas of the custom skills (no body)."""
    out = []
    for n in names():
        s = load(n)
        if s:
            out.append({k: v for k, v in s.items() if k != "body"})
    return out


def arg_names(args):
    """"target: what to press; side: left|right|auto" -> "target; side"."""
    return "; ".join(a.split(":")[0].strip() for a in (args or "").split(";") if a.strip())


def index_text():
    """Router-prompt lines: `- skill:press-pump (target; side): Use when ...`."""
    lines = [f"- skill:{m['name']} ({arg_names(m.get('args'))}): {m.get('use_when', '')}" for m in index()]
    return "\n".join(lines) or "(no learned skills yet)"


# ---------------------------------------------------------------- usage stats
def _usage_path():
    return f"{SKILLS}/usage.json"


@contextmanager
def _locked():
    os.makedirs(SKILLS, exist_ok=True)
    with open(_usage_path() + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def usage():
    try:
        return json.load(open(_usage_path()))
    except (FileNotFoundError, ValueError):
        return {}


def _bump(name, **inc):
    """Under the lock: add the counters in inc, set last_* times. Called with the lock held."""
    u = usage()
    now = time.time()
    s = {"uses": 0, "wins": 0, "fails": 0, "patches": 0, "last_used": None, "last_patch": None, "created": None,
         **u.get(name, {})}
    for k, v in inc.items():
        if k in ("created",):
            s["created"] = s["created"] or now
        else:
            s[k] = s.get(k, 0) + v
    if "uses" in inc:
        s["last_used"] = now
    if "patches" in inc:
        s["last_patch"] = now
    u[name] = s
    tmp = _usage_path() + ".tmp"
    json.dump(u, open(tmp, "w"), indent=1); os.replace(tmp, _usage_path())
    return s


def record(name, outcome):
    """outcome: "use" | "win" | "fail" (also "patch" / "created", used by changes_since)."""
    key = {"use": "uses", "win": "wins", "fail": "fails", "patch": "patches", "created": "created"}[outcome]
    with _locked():
        return _bump(name, **{key: 1})


# ---------------------------------------------------------------- history: snapshot / changes / undo
def snapshots():
    """Snapshot ids, oldest first (ids sort by time)."""
    try:
        return sorted(d for d in os.listdir(HIST) if not d.startswith(".") and os.path.isdir(f"{HIST}/{d}"))
    except FileNotFoundError:
        return []


def snapshot():
    """Copy every SKILL.md to .along_skills_hist/<id>/<name>/SKILL.md; returns the id (call before a refinement)."""
    sid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    while os.path.exists(f"{HIST}/{sid}"):
        sid += "x"
    os.makedirs(f"{HIST}/{sid}")
    for n in names():
        os.makedirs(f"{HIST}/{sid}/{n}")
        shutil.copy2(_path(n), _path(n, f"{HIST}/{sid}"))
    return sid


def changes_since(snap_id):
    """[(name, "created"|"patched")] of current skills vs the snapshot; records them in usage.json."""
    root = f"{HIST}/{snap_id}"
    out = []
    for n in names():
        old = _read(_path(n, root))
        if old is None:
            out.append((n, "created"))
        elif old != _read(_path(n)):
            out.append((n, "patched"))
    with _locked():
        for n, what in out:
            _bump(n, **({"created": 1} if what == "created" else {"patches": 1}))
    return out


def history(name):
    """Snapshot ids (newest first) holding a version of name different from the current one."""
    cur = _read(_path(name))
    return [sid for sid in reversed(snapshots()) if (old := _read(_path(name, f"{HIST}/{sid}"))) is not None and old != cur]


def undo(name):
    """Restore name from the newest snapshot that differs from it (remove it if it was created after that snapshot).
    The replaced version is kept in .along_skills_hist/.undone/. Returns a short message."""
    name = (name or "").removeprefix("skill:").strip()
    if not NAME.match(name):
        return f"no skill {name!r}"
    cur = _read(_path(name))
    for sid in reversed(snapshots()):
        old = _read(_path(name, f"{HIST}/{sid}"))
        if old == cur:
            continue
        if cur is not None:                              # keep what we throw away
            keep = f"{HIST}/.undone/{time.strftime('%Y%m%d-%H%M%S')}-{name}"
            os.makedirs(keep, exist_ok=True); open(f"{keep}/SKILL.md", "w").write(cur)
        if old is None:
            shutil.rmtree(f"{SKILLS}/{name}")
            return f"removed {name} (created after snapshot {sid})"
        os.makedirs(f"{SKILLS}/{name}", exist_ok=True)
        open(_path(name), "w").write(old)
        return f"restored {name} from snapshot {sid}"
    return f"nothing to undo for {name}"


# ---------------------------------------------------------------- dashboard
def listing():
    u = usage()
    skills = []
    for n in names():
        s = load(n)
        if s:
            skills.append({**s, "stats": u.get(n, {}), "history": history(n)})
    return {"builtins": BUILTINS, "skills": skills}
