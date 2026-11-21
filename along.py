#!/usr/bin/env python3
"""along — voice helper around the arms. Idle: only a cheap detector watches the scene camera.
Voice = GPT-Live (speech-to-speech; it hands robot/vision work to Codex) only while switched on (or, with --yolo, while an object of interest is there: a new object (not in
the idle baseline, 3 of 4 frames to start, kept 8 s after it was last seen) or an arm holding/offering. What you say goes to
one long-running Codex session (tmux "along", persona prompts/along.md) which acts with `robot`; its final message is spoken.

  along run              the daemon (mic + Codex bridge), switched by hand: along on / along off (or /along buttons)
  along run --yolo       instead wake on the scene camera (new thing in the centre of the table)
  along idle             store what the scene shows now as the idle baseline (empty table + fixtures)
  along see              latest detections (label, conf, scene px in the 800x450 tile for `robot locate`)
  along look "question"  ask GPT 6.1 terra about the current scene + wrist frames (~5 s): what is where, orientation,
                         is it between the jaws. Scene px answers fit `robot locate`.
  along say "text"       have GPT-Live say this (only while switched on)
  along tell "text"      send a line to the Codex session (what the daemon does with speech)
  along held left toothbrush | along held left none    record what an arm holds (keeps along active; shown on /along)
  along wake             force the running daemon awake (debugging)
Skills (one call per request; each ends with a robot step: state lines + snap_all.jpg):
  along hold <side> [what] [flat] [x y z]   offer + wait + close + record (+ speech); flat = jaws up/down
  along release <side>   open, then recall ready
  along turn <side> <deg> | along tilt <side> <deg>   wrist roll | wrist flex (+ tips down)
  along press <side> x y z [cm]   push straight down on a point (pump, button), default 4 cm past it, back up
  along park             arms holding nothing -> rest
Debug page: `robot stream` -> http://localhost:8101/along (boxes, state, conversation, Codex pane, type-as-voice).
Key: OPENAI_API_KEY in the environment or in .env (repo). Optional ALONG_MIC (pw source; default the C920 mic).
"""
import json, os, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
SEEN, IDLE, MUTE = f"{HERE}/.along_seen.json", f"{HERE}/along_idle.json", f"{HERE}/.along_mute"
HELD = f"{HERE}/.along_held.json"                        # {"left": "toothbrush"}: what each arm holds (set by the agent)
EVENTS = f"{HERE}/.along_events.jsonl"                    # conversation + state history (debug page /along)
TMUX, W, H = "along", 800, 450
ROI, CHANGE = (150, 60, 650, 430), 0.02                    # centre of the table (scene px) where new things wake Along
BG = f"{HERE}/along_idle_bg.npy"
CODEX = ["codex", "--dangerously-bypass-approvals-and-sandbox", "-C", HERE,
         "-m", os.environ.get("ALONG_MODEL", "gpt-6.1-terra"), "-c", "model_reasoning_effort=low",
         "-c", "model_auto_compact_token_limit=100000",
         "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',   # no AGENTS.md (the brief has what it needs), no web
         *[x for f in ("hooks", "plugins", "apps", "multi_agent", "image_generation", "browser_use", "computer_use", "goals",
                       "skill_search", "in_app_browser", "tool_suggest") for x in ("--disable", f)]]


def env(k, d=None):
    if k in os.environ:
        return os.environ[k]
    try:
        for line in open(f"{HERE}/.env"):
            a, _, b = line.strip().partition("=")
            if a.strip() == k:
                return b.strip().strip('"\'')
    except FileNotFoundError:
        pass
    return d


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def event(kind, text=""):
    """kind: wake idle heard say tell error. Shared by the daemon and `along say` (called by Codex)."""
    with open(EVENTS, "a") as f:
        f.write(json.dumps({"t": round(time.time(), 2), "kind": kind, "text": text}) + "\n")


# ---------------------------------------------------------------- speech
def say(text):
    """GPT-Live is the only voice: while a session is up it speaks this; otherwise it is only logged."""
    event("say", text)
    if os.path.exists(f"{HERE}/.along_live"):
        with open(f"{HERE}/.along_live_out.jsonl", "a") as f:
            f.write(json.dumps({"text": text}) + "\n")
    open(f"{HERE}/.along_spoke", "w").write(str(time.time()))


def chime(freq=660, dur=0.12):
    """Short beep: 'I heard you' (low) / 'listening now' (high)."""
    open(MUTE, "w").write(str(time.time() + dur + 0.3))
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={dur}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def muted():
    try:
        return time.time() < float(open(MUTE).read())
    except (FileNotFoundError, ValueError):
        return False


def spoke_left(window=20.0):
    """Seconds left in which a reply counts without saying "Along" (0 = closed)."""
    try:
        return max(0.0, round(window - (time.time() - float(open(f"{HERE}/.along_spoke").read())), 1))
    except (FileNotFoundError, ValueError):
        return 0.0



# ---------------------------------------------------------------- GPT-Live voice (speech-to-speech; Codex does the work)
LIVE_ON, LIVE_OUT = f"{HERE}/.along_live", f"{HERE}/.along_live_out.jsonl"
LIVE_PROMPT = """You are Along, the voice of two robot arms (left = white, right = orange) in a busy lab. You can't see
or move anything yourself: every request to do, look at, hold, turn, press, release or check something goes to your
hands (delegate it at once and say only "On it." or "Sure." while it runs: never claim it is done or happening before
your hands report it). Your hands' reports arrive as commentary: say each one right away, nearly word for word
(e.g. "Ready, put it between my jaws." is an instruction for the person and must be said).
Talk only when someone talks to you (they say "Along" or answer you); ignore other chatter in the room.
Be quick and casual, one short sentence. Never talk about safety, fingers or injuries, never ask "are you sure":
the people here know what they are doing. Just do it."""


def mic_source():
    return env("ALONG_MIC") or next((l.split()[1] for l in subprocess.run(["pactl", "list", "short", "sources"],
           capture_output=True, text=True).stdout.splitlines() if "C920" in l and "input" in l), None)


class LiveVoice:
    """GPT-Live session while switched on: C920 mic -> GPT-Live -> speaker. Its delegations go to Codex as [voice] lines;
    what Codex says (`along say`) comes back as commentary that GPT-Live speaks. Same fields as Mic for the state file."""
    RATE = 24000

    def __init__(self, on_text):
        self.on, self.on_text, self.phase, self.prob, self.stt_busy = threading.Event(), on_text, "off", 0.0, False
        self.ws, self.deleg, self.heard, self.speak_until = None, None, "", 0.0
        self.utt, self.utt_t = "", 0.0                    # current utterance, for the page log
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            self.on.wait()
            try:
                self.session()
            except Exception as e:
                log("live error", e); event("error", f"live: {e}"); time.sleep(2)
            self.ws, self.phase = None, "off"
            try:
                os.remove(LIVE_ON)
            except FileNotFoundError:
                pass

    def send(self, **e):
        import uuid
        self.ws.send(json.dumps({"event_id": str(uuid.uuid4()), **e}))

    def session(self):
        import base64
        from websockets.sync.client import connect
        ws = self.ws = connect("wss://api.openai.com/v1/live/sessions", open_timeout=15, max_size=None,
                               additional_headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"})
        self.send(type="session.start", session={"model": env("ALONG_LIVE_MODEL", "gpt-live-1.1"), "instructions": LIVE_PROMPT,
                  "audio": {"format": {"type": "audio/pcm", "rate": self.RATE}, "output": {"voice": env("ALONG_LIVE_VOICE", "marin")}},
                  "delegation": {"type": "client"},
                  "input": [{"type": "message", "role": "developer", "content": [{"type": "input_text",
                             "text": "Greet them now in a few words: you are Along and can lend a hand."}]}]})
        open(LIVE_ON, "w").write(str(os.getpid())); open(LIVE_OUT, "w").close()
        fmt = ["--raw", "--rate", str(self.RATE), "--channels", "1", "--format", "s16"]
        src = mic_source()
        mic = subprocess.Popen(["pw-record", *fmt, *(["--target", src] if src else []), "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        spk = subprocess.Popen(["pw-play", *fmt, "-"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stop = threading.Event()

        def pump_mic():                                   # 40 ms chunks; silence while we talk (no echo of ourselves)
            while not stop.is_set():
                b = mic.stdout.read(self.RATE * 2 // 25)
                if not b:
                    return
                if time.time() < self.speak_until + 0.3:
                    b = bytes(len(b))
                try:
                    self.send(type="session.input_audio.append", audio=base64.b64encode(b).decode())
                except Exception:
                    return

        def pump_out():                                   # `along say` lines -> spoken commentary
            pos = 0
            while not stop.is_set():
                time.sleep(0.1)
                try:
                    lines = open(LIVE_OUT).read()[pos:]
                except FileNotFoundError:
                    continue
                for l in lines.splitlines():
                    if l.strip():
                        self.send(type="session.commentary.append", delegation_id=self.deleg, content=json.loads(l)["text"])
                pos += len(lines)
        started, said = False, ""
        try:
            for raw in ws:
                if not self.on.is_set():
                    break
                e = json.loads(raw); k = e.get("type")
                if k == "session.started" and not started:
                    started = True; self.phase = "listening"; log("live session", e["session"]["id"])
                    threading.Thread(target=pump_mic, daemon=True).start()
                    threading.Thread(target=pump_out, daemon=True).start()
                elif k == "session.output_audio.delta":
                    b = base64.b64decode(e["delta"]); spk.stdin.write(b); spk.stdin.flush()
                    import numpy as np                    # the stream never stops (silence too): mute only on real speech
                    if len(b) >= 2 and float(np.sqrt(np.mean(np.frombuffer(b, np.int16).astype(np.float32) ** 2))) > 300:
                        self.speak_until = max(self.speak_until, time.time() + 0.6); self.phase = "muted"
                elif k == "session.output_transcript.delta":
                    said += e["delta"]
                elif k == "session.input_transcript.delta":
                    self.heard += e["delta"]; self.utt += e["delta"]; self.utt_t = time.time(); self.phase = "speech"
                elif k == "session.delegation.created":         # carries no text: hand over everything heard since the last one
                    if self.utt.strip():
                        event("heard", self.utt.strip()); self.utt = ""
                    self.deleg, req, self.heard, self.phase = e["delegation"]["id"], " ".join(self.heard.split())[-400:], "", "listening"
                    self.on_text(req or "(inaudible)")
                elif k == "error":
                    log("live:", e); event("error", f"live: {e.get('error', e)}")
                if self.utt.strip() and time.time() - self.utt_t > 1.0:     # utterance over: log it once
                    event("heard", self.utt.strip()); self.utt = ""
                if time.time() > self.speak_until + 0.3:
                    if self.phase == "muted":
                        self.phase = "listening"
                    if said.strip():
                        event("live", said.strip()); said = ""
        finally:
            stop.set()
            try:
                self.send(type="session.close"); ws.close()
            except Exception:
                pass
            mic.kill(); spk.stdin.close(); spk.kill(); log("live session closed")

# ---------------------------------------------------------------- codex bridge
def tmux(*a, **k):
    return subprocess.run(["tmux", *a], capture_output=True, text=True, **k)


TELL = threading.Lock()


def codex_up():
    """Start the one Codex session (once; it stays up across idle/active, auto-compacts) and wait until it has read the brief."""
    if tmux("has-session", "-t", TMUX).returncode == 0:
        return
    log("starting Codex in tmux session", TMUX)
    tmux("new-session", "-d", "-s", TMUX, "-x", "200", "-y", "50", "-c", HERE,
         *CODEX, open(f"{HERE}/prompts/along.md").read() + "\n\nReply \"ready\" now and end your turn; requests arrive as new turns.")
    t0, last = time.time(), ""
    while time.time() - t0 < 120:                          # idle = screen unchanged for 6 s after the first turn
        time.sleep(3)
        scr = tmux("capture-pane", "-p", "-t", TMUX).stdout
        if scr == last and "Worked for" in scr or time.time() - t0 > 90:
            break
        last = scr
    log("Codex ready")


def tell(text):
    if env("ALONG_DRY"):                                   # test the daemon without Codex
        return log("(dry) ->", text)
    with TELL:
        codex_up()
        tmux("send-keys", "-t", TMUX, "-l", text.replace("\n", " "))
        time.sleep(0.3)
        tmux("send-keys", "-t", TMUX, "Enter")
    log("->", text); event("tell", text)


# ---------------------------------------------------------------- vision
def iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x2 - x1) * max(0, y2 - y1)
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i + 1e-9)


class Eyes:
    def __init__(self, conf=0.2):
        from ultralytics import YOLOWorld
        from so101 import Robot
        self.m, self.bot, self.conf = YOLOWorld(f"{HERE}/along_world.pt"), Robot(), conf

    def arm_px(self):
        """The arms' links (FK at the measured pose) projected into the 800x450 scene image: the detector likes to call
        a moved arm 'scissors' or 'knife', so boxes on an arm don't count as new objects."""
        import numpy as np, collide
        c = json.load(open(f"{HERE}/scene.json"))["scene_camera"]
        f, up = np.array(c["forward"]), np.array(c["up"]); r = np.cross(f, up); fp = c["focal_px"]
        m, su, pts = self.bot.state()["measured"], collide.setup(), []
        for side in ("left", "right"):
            F = collide.frames(np.array([m[f"{side}_{j}"] for j in collide.J]))[0]
            o = [F[i, :3, 3] for i in range(1, F.shape[0])]
            o = o + [(a + b) / 2 for a, b in zip(o, o[1:])] + [(F[-1] @ [0, 0, -0.1, 1])[:3]]   # + midpoints, jaw tips
            B = collide.base_T(side, su)
            for q in o:
                d = (B @ np.r_[q, 1])[:3] - np.array(c["position"])
                pts.append((W / 2 + fp * d @ r / (d @ f), H / 2 - fp * d @ up / (d @ f)))
        return pts

    def change(self, img, arm):
        """Fraction of the centre ROI that differs from the idle background (class-agnostic: the detector misses a
        toothbrush in a hand). Arm links are masked; the background follows slow light changes while nothing changed."""
        import cv2, numpy as np
        g = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(img, (W // 4, H // 4)), cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
        if getattr(self, "bg", None) is None:
            self.bg = np.load(BG) if os.path.exists(BG) and not hasattr(self, "bg") else g.copy()
        d = g - self.bg; d = np.abs(d - np.median(d))          # minus the global shift (auto exposure)
        m = np.zeros_like(g, np.uint8); x0, y0, x1, y1 = (v // 4 for v in ROI); m[y0:y1, x0:x1] = 1
        for u, v in arm:
            cv2.circle(m, (int(u) // 4, int(v) // 4), 14, 0, -1)
        ch = (d > 28) & (m > 0); frac = float(ch.sum()) / max(1, int(m.sum()))
        if frac < CHANGE / 3:
            self.bg += 0.05 * (g - self.bg)
        ys, xs = np.nonzero(ch)
        self.blob = [int(xs.mean() * 4), int(ys.mean() * 4)] if len(xs) else None
        return frac

    def detect(self):
        img = self.bot.thumb("scene", W, H)
        r = self.m.predict(img, verbose=False, conf=self.conf)[0]
        try:
            arm = self.arm_px()
        except Exception:
            arm = []
        self.frac = round(self.change(img, arm), 3)
        out = []
        for c, s, x in zip(r.boxes.cls, r.boxes.conf, r.boxes.xyxy):
            x = [int(v) for v in x]
            out.append({"label": r.names[int(c)], "conf": round(float(s), 2), "box": x,
                        "px": [(x[0] + x[2]) // 2, (x[1] + x[3]) // 2],
                        "arm": any(x[0] - 15 <= u <= x[2] + 15 and x[1] - 15 <= v <= x[3] + 15 for u, v in arm)})
        self.arm = [[round(u), round(v)] for u, v in arm]
        return out


def in_roi(px):
    return ROI[0] <= px[0] <= ROI[2] and ROI[1] <= px[1] <= ROI[3]


def novel(dets, base):
    """Detections that count for waking: centre of the table only, any class (names are unreliable), not on an arm,
    not where the idle baseline already had a box."""
    return [d for d in dets if d["conf"] >= 0.15 and in_roi(d["px"]) and not d.get("arm")
            and not any(iou(b["box"], d["box"]) > 0.3 for b in base)]


def rd_held():
    try:
        return json.load(open(HELD))
    except (FileNotFoundError, ValueError):
        return {}


def engaged(bot):
    """Arms tied up with an object of interest (the detector often misses an object in the jaws):
    'holding' = HOLDING or jaws shut with the arm raised (thin things close fully), 'offering' = jaws open, arm raised."""
    try:
        tg, why = bot.state()["target"], []
        for side in ("left", "right"):
            a = getattr(bot, side); up = a.tip()[2] > 0.12; g = tg[f"{side}_gripper"]
            what = rd_held().get(side)
            if what or a.holding() or (up and g <= 10):
                why.append(f"{side} holding {what or 'something'}")
            elif up and g >= 25:
                why.append(f"{side} offering")
        return why
    except Exception as e:
        return [f"robot state unknown ({e})"]


ON = f"{HERE}/.along_on"                                    # manual switch: exists = Along is on (mic open)


BUSY = threading.Event()                                  # a Codex turn is running
STATE = {"text": "", "t": 0.0}                            # latest look-only robot step while switched on


def brief_state(txt):
    """'left grip 3 tip 0.22/+0.00/0.10, right ...' from robot step output."""
    import re
    out = []
    for ln in txt.splitlines():
        m = re.match(r"(left|right)\s.*grip ([\d.-]+).*?(HOLDING)?.*tip x([\d.-]+) y([+\d.-]+) z([\d.-]+)", ln)
        if m:
            out.append(f"{m[1]} grip {float(m[2]):.0f}{' HOLDING' if m[3] else ''} tip {m[4]}/{m[5]}/{m[6]}")
    return ", ".join(out)


def watch(mic):
    """While switched on and Codex is idle: refresh snap_all.jpg + arm state every 2 s, so a request starts with a fresh view."""
    while True:
        if mic.on.is_set() and not BUSY.is_set():
            txt = rstep()
            if txt:
                STATE.update(text=brief_state(txt), t=time.time())
        time.sleep(2.0)


def relay_replies():
    """Codex's final message of each turn is what Along says (GPT-Live voices it): no `along say` tool call needed."""
    import glob
    done, first, mt = set(), True, {}
    while True:
        fs = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")), key=os.path.getmtime)[-6:]
        for f in fs:
            if mt.get(f) == os.path.getmtime(f):
                continue
            mt[f] = os.path.getmtime(f)
            with open(f) as fh:
                if f'"cwd":"{HERE}"' not in fh.readline():
                    continue
                for ln in fh:
                    if '"task_complete"' not in ln:
                        continue
                    p = json.loads(ln).get("payload", {}); tid = p.get("turn_id")
                    if tid in done:
                        continue
                    done.add(tid); msg = (p.get("last_agent_message") or "").strip(); BUSY.clear()
                    if not first and msg and msg.lower().strip(".") != "ready":
                        say(msg[:400])
        first = False; time.sleep(0.3)


def run_manual():
    """Manual on/off (`along on` / `along off`, or the page buttons) instead of the scene detector."""
    on, last_voice = False, 0.0

    def heard(text):
        nonlocal last_voice
        last_voice = time.time()
        age = time.time() - STATE["t"]
        ctx = f"   || now: {STATE['text']}; held {json.dumps(rd_held())}; snap_all.jpg {age:.0f} s old" if age < 6 else ""
        BUSY.set(); tell(f"[voice] {text}{ctx}")
    if not env("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY missing (.env): GPT-Live is the voice")
    mic = LiveVoice(heard)
    threading.Thread(target=relay_replies, daemon=True).start()
    threading.Thread(target=watch, args=(mic,), daemon=True).start()
    if not env("ALONG_DRY"):
        codex_up()
    log("off (GPT-Live voice); waiting for `along on`")
    while True:
        t, want = time.time(), os.path.exists(ON)
        if want and not on:
            on = True; log("ON"); event("wake", "switched on")
            chime(880); mic.on.set()
        elif on and not want:
            on = False; mic.on.clear(); log("OFF"); event("idle", "switched off")
            threading.Thread(target=skill, args=(["park"],), daemon=True).start()   # no Codex turn needed
        json.dump({"t": t, "active": on, "why": ["switched on"] if on else [], "manual": True, "listening": mic.on.is_set(),
                   "mic": mic.phase, "vad": round(mic.prob, 2), "stt": mic.stt_busy, "reply_open": spoke_left(), "speaking": muted(),
                   "voice": "GPT-Live", "all": [], "new": [], "held": rd_held(), "hits": [], "last_voice": last_voice}, open(SEEN, "w"))
        time.sleep(0.2)


def run():
    eyes = Eyes()
    if not os.path.exists(IDLE):
        sys.exit("no idle baseline: clear the table to its idle state and run `along idle` first")
    base = json.load(open(IDLE))
    active, hits, last_new, last_voice, woke, GRACE = False, [], 0.0, 0.0, 0.0, 8.0

    def heard(text):
        nonlocal last_voice
        last_voice = time.time(); tell(f"[voice] {text}")
    mic = LiveVoice(heard)
    if not env("ALONG_DRY"):
        codex_up()                                         # warm, so waking up is instant
    log("idle; watching the scene")
    while True:
        t = time.time()
        try:
            dets = eyes.detect()
        except Exception as e:
            log("vision error", e); event("error", f"vision: {e}"); time.sleep(2); continue
        new = novel(dets, base)
        if not new and eyes.frac > CHANGE and eyes.blob:
            new = [{"label": "change", "conf": eyes.frac, "px": eyes.blob, "box": [eyes.blob[0] - 20, eyes.blob[1] - 20, eyes.blob[0] + 20, eyes.blob[1] + 20]}]
        hits = (hits + [bool(new)])[-4:]
        force = os.path.exists(f"{HERE}/.along_force")
        if force:
            os.remove(f"{HERE}/.along_force"); new = new or [{"label": "something", "px": [400, 225]}]
        if sum(hits) >= 3 or force or (active and new):    # object of interest in the scene (3 of 4 frames to start)
            last_new = t
        arms = engaged(eyes.bot)
        if active and not arms and new and new[0]["label"] == "change" and t - max(last_voice, woke) > 60:
            eyes.bg = None; log("change left alone 60 s: it is part of the scene now")   # re-learn the background
        why = ([f"new object {t - last_new:.0f}s ago"] if t - last_new < GRACE else []) + arms
        if not active and why:
            active, woke = True, t
            names = (", ".join(f"scene px ({d['px'][0]},{d['px'][1]})" for d in new)
                     and "something new at " + ", ".join(f"scene px ({d['px'][0]},{d['px'][1]})" for d in new)) or ", ".join(arms)
            log("WAKE:", names, [d.get("label") for d in new]); event("wake", names)
            say("Hi, I'm Along. Need a hand?")
            chime(880); mic.on.set()
            tell(f"[along] Woke up: {names} (the cheap detector only says where; look at the image yourself to see what it is). "
                 "I already greeted them and asked if they need a hand; the mic is on "
                 "while an object of interest is in the scene or in the jaws. Wait for their [voice] request.")
        elif active and not why:
            active = False
            mic.on.clear()                                 # nothing of interest: no voice commands go through
            log("back to idle"); event("idle")
            tell("[along] Back to idle: no new object in the scene and nothing in the jaws; the mic is off. "
                 "Park the arms (robot step \"recall rest\"), silently.")
        json.dump({"t": t, "active": active, "why": why, "listening": mic.on.is_set(), "mic": mic.phase, "vad": round(mic.prob, 2), "stt": mic.stt_busy,
                   "reply_open": spoke_left(), "speaking": muted(), "all": dets,
                   "new": new, "arm_px": getattr(eyes, "arm", []), "held": rd_held(), "hits": hits, "change": eyes.frac, "roi": ROI, "last_new": last_new, "last_voice": last_voice}, open(SEEN, "w"))
        time.sleep(max(0.0, 0.5 - (time.time() - t)))


def look(question, cams=("scene", "left", "right")):
    """Ask a vision model (GPT 6.1 terra through `codex exec`, ~5 s) about the newest frames. Scene is sent as the
    800x450 tile, so pixel answers go straight into `robot locate u v z`."""
    import cv2, tempfile
    from so101 import Robot
    bot, d, imgs = Robot(), tempfile.mkdtemp(prefix="along_look_"), []
    for c in cams:
        try:
            p = f"{d}/{c}.jpg"; cv2.imwrite(p, bot.thumb(c, *((W, H) if c == "scene" else (640, 360)))); imgs.append(p)
        except Exception as e:
            log("look: no", c, e)
    brief = ("Images: " + ", ".join(f"{os.path.basename(p)[:-4]} camera" for p in imgs) + ". The scene camera (800x450) faces two "
             "robot arms from the front: orange (right arm) on image-left, white (left arm) on image-right; left/right "
             "wrist cameras look out of each gripper between its jaws. Give pixel coordinates in the scene image when "
             "you mention where something is. Answer briefly and concretely.\n\nQuestion: " + question)
    event("look", question)
    if env("OPENAI_API_KEY"):                              # lean: just the images + question
        import base64, requests
        content = [{"type": "input_text", "text": brief}] + [
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()} for p in imgs]
        r = requests.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"},
                          json={"model": env("ALONG_LOOK_MODEL", "gpt-6.1-terra"), "reasoning": {"effort": "low"},
                                "input": [{"role": "user", "content": content}]}, timeout=60)
        r.raise_for_status()
        ans = "".join(c.get("text", "") for o in r.json().get("output", []) if o.get("type") == "message"
                      for c in o.get("content", [])).strip()
    else:                                                  # no key: Codex login; empty cwd so AGENTS.md isn't sent along
        r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-m", env("ALONG_LOOK_MODEL", "gpt-6.1-terra"),
                            "-c", "model_reasoning_effort=low", "-o", f"{d}/answer.txt", "-i", *imgs],
                           input=brief, capture_output=True, text=True, timeout=120, cwd=d)
        ans = open(f"{d}/answer.txt").read().strip() if os.path.exists(f"{d}/answer.txt") else r.stdout.strip()[-2000:]
    event("seen", ans)
    return ans




# ---------------------------------------------------------------- skills: one call per spoken request
def rstep(cmds=""):
    """`robot step` (moves, waits, writes snap_all.jpg) -> its text."""
    return subprocess.run(["robot", "step", cmds] if cmds else ["robot", "step"], capture_output=True, text=True).stdout.strip()


def say_bg(text):
    threading.Thread(target=say, args=(text,)).start()


def set_held(side, what):
    h = rd_held()
    h.pop(side, None) if what in ("", "none", "nothing") else h.update({side: what})
    json.dump(h, open(HELD, "w")); event("held", json.dumps(h))


HOLD_AT = {"left": (0.25, -0.03, 0.17), "right": (0.25, 0.03, 0.17)}    # in front of the arm, at hand height


def skill(a):
    """along hold|release|turn|press|park ...: whole requests in one call, no questions asked."""
    cmd, side = a[0], (a[1] if len(a) > 1 else "")
    if side and side not in ("left", "right"):
        sys.exit("side = left | right")
    if cmd == "hold":                               # along hold <side> [what] [flat] [x y z]
        rest = a[2:]; flat = "flat" in rest; rest = [r for r in rest if r != "flat"]
        nums = [float(r) for r in rest if r.replace(".", "", 1).lstrip("-").isdigit()]
        what = " ".join(r for r in rest if not r.replace(".", "", 1).lstrip("-").isdigit()) or "object"
        x, y, z = nums[:3] if len(nums) >= 3 else HOLD_AT[side]
        for attempt in (1, 2):
            ask = threading.Timer(4.0, say, args=("Ready, put it between my jaws.",)) if attempt == 1 else None
            ask and ask.start()                     # only if it is not already in there (offer closes once it sits still)
            r = json.loads(subprocess.run(["robot", "offer", side, str(x), str(y), str(z), "90" if flat else "0"],
                                          capture_output=True, text=True).stdout or '{"holding": false}')
            ask and ask.cancel()
            if r.get("holding"):
                set_held(side, what); break
            if attempt == 1:
                say_bg("Missed it, once more.")
        print(f"hold {side}: {r}"); print(rstep())
    elif cmd == "release":                          # along release <side>
        print(rstep(f"set {side}_gripper 45")); time.sleep(1.0); set_held(side, "none")
        print(rstep(f"recall ready {side}"))
    elif cmd == "turn":                             # along turn <side> <deg>   (wrist roll, + / -)
        print(rstep(f"move {side}_wrist_roll {float(a[2])}"))
    elif cmd == "tilt":                             # along tilt <side> <deg>   (wrist flex, + tips down)
        print(rstep(f"move {side}_wrist_flex {float(a[2])}"))
    elif cmd == "press":                            # along press <side> x y z [depth_cm]: push straight down on (x,y,z)
        x, y, z = map(float, a[2:5]); depth = float(a[5]) / 100 if len(a) > 5 else 0.04
        print(rstep(f"set {side}_gripper 0; goto {side} {x} {y} {z + 0.04:.3f}"))
        print(rstep(f"goto {side} {x} {y} {z - depth:.3f}"))      # aims below the surface: the servos push
        time.sleep(0.8)
        print(rstep(f"goto {side} {x} {y} {z + 0.05:.3f}"))
    elif cmd == "park":                             # along park: arms that hold nothing go to rest
        h = rd_held()
        print(rstep("; ".join(f"recall rest {sd}" for sd in ("left", "right") if sd not in h) or ""))


def main():
    a = sys.argv[1:] or ["help"]
    if a[0] == "run":
        run() if "--yolo" in a else run_manual()
    elif a[0] == "on":
        open(ON, "w").write("on")
    elif a[0] == "off":
        if os.path.exists(ON):
            os.remove(ON)
    elif a[0] == "held":                                  # along held left toothbrush | along held left none
        h = rd_held(); side, what = a[1], " ".join(a[2:])
        if what in ("", "none", "nothing"):
            h.pop(side, None)
        else:
            h[side] = what
        json.dump(h, open(HELD, "w")); event("held", json.dumps(h)); print(h)
    elif a[0] in ("hold", "release", "turn", "tilt", "press", "park"):
        skill(a)
    elif a[0] == "look":
        print(look(" ".join(a[1:])))
    elif a[0] == "say":
        say(" ".join(a[1:]))
    elif a[0] == "wake":                                  # debug: force the daemon awake (see .along_force)
        open(f"{HERE}/.along_force", "w").write("wake")
    elif a[0] == "tell":
        t = " ".join(a[1:])
        if t.startswith("[voice]"):
            event("heard", t[7:].strip() + "  (typed)")
        tell(t)
    elif a[0] == "idle":
        e = Eyes(conf=0.1); d = [x for _ in range(6) for x in e.detect()]   # union of a few frames: flicker-proof
        import numpy as np; np.save(BG, e.bg)
        json.dump(d, open(IDLE, "w"))
        print("idle baseline:", [x["label"] for x in d])
    elif a[0] == "see":
        if not os.path.exists(SEEN):
            sys.exit("along run is not running (no detections yet)")
        s = json.load(open(SEEN))
        print(f"{'active' if s['active'] else 'idle'}, {time.time() - s['t']:.1f}s old (scene px = 800x450 tile, for robot locate)")
        for d in s["all"]:
            print(f"  {'NEW ' if d in s['new'] else '    '}{d['label']:<16} {d['conf']:.2f}  px {d['px'][0]:>3} {d['px'][1]:>3}  box {d['box']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
