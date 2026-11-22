#!/usr/bin/env python3
"""along — voice helper around the arms. Idle: only a cheap detector watches the scene camera.
Voice = GPT-Live (speech-to-speech; it hands robot/vision work to Codex) only while switched on (or, with --yolo, while an object of interest is there: a new object (not in
the idle baseline, 3 of 4 frames to start, kept 8 s after it was last seen) or an arm holding/offering. What you say goes to
a fast router (one gpt-6.1-sol call: runs a skill itself) or, for anything else, a headless Codex turn (codex_agent.py,
brief prompts/along.md) which acts with `robot`; its final message is spoken.

  along attach           open Al's current Codex conversation in the Codex TUI (codex resume)
  along run              the daemon (mic + Codex bridge), switched by hand: along on / along off (or /along buttons)
  along run --yolo       instead wake on the scene camera (new thing in the centre of the table)
  along idle             store what the scene shows now as the idle baseline (empty table + fixtures)
  along see              latest detections (label, conf, scene px in the 800x450 tile for `robot locate`)
  along look "question"  ask GPT 6.1 sol (ultrafast) about the current scene + wrist frames (~2 s): what is where, orientation,
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
import json, os, re, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
SEEN, IDLE, MUTE = f"{HERE}/.along_seen.json", f"{HERE}/along_idle.json", f"{HERE}/.along_mute"
HELD = f"{HERE}/.along_held.json"                        # {"left": "toothbrush"}: what each arm holds (set by the agent)
EVENTS = f"{HERE}/.along_events.jsonl"                    # conversation + state history (debug page /along)
W, H = 800, 450                                         # scene tile (px)
ROI, CHANGE = (150, 60, 650, 430), 0.02                    # centre of the table (scene px) where new things wake Along
BG = f"{HERE}/along_idle_bg.npy"


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
    """kind: wake idle heard say live error (conversation) | tell codex look seen aim skill held sys (activity panel).
    Written by the daemon and by the skill processes Codex runs; the dashboard shows both."""
    with open(EVENTS, "a") as f:
        f.write(json.dumps({"t": round(time.time(), 2), "kind": kind, "text": text}) + "\n")


# ---------------------------------------------------------------- speech
def _norm(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def say(text):
    """GPT-Live is the only voice: while a session is up it speaks this; otherwise it is only logged.
    Skipped if GPT-Live itself just said the same ("You're welcome." twice)."""
    try:
        recent_live = [json.loads(l) for l in open(EVENTS).readlines()[-15:] if l.strip()]
    except FileNotFoundError:
        recent_live = []
    n = _norm(text)
    if n and any(e["kind"] == "live" and time.time() - e["t"] < 8 and (n in _norm(e["text"]) or _norm(e["text"]) in n)
                 and len(_norm(e["text"])) > 3 for e in recent_live):
        return event("sys", f"not repeating what Al just said: {text}")
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


def tone(freq, vol=0.3):
    """One short beep in the background: 880 Hz = Al is on, 660 Hz = Al is off. A 48 kHz wav (the speaker's native
    rate: no resampling) with 60 ms of silence first, so the device waking up does not clip it."""
    import numpy as np, wave
    path = f"{HERE}/.along_beep_{freq}.wav"
    if not os.path.exists(path):
        r = 48000; t = np.arange(int(r * 0.12)) / r
        env_ = np.minimum(1, np.minimum(t, t[-1] - t) / 0.01)        # 10 ms fade in/out: no clicks
        pcm = np.concatenate([np.zeros(int(r * 0.06)), np.sin(2 * np.pi * freq * t) * env_ * vol])
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(r)
            w.writeframes((pcm * 32767).astype(np.int16).tobytes())
    threading.Thread(target=subprocess.run, args=(["pw-play", path],),
                     kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}, daemon=True).start()


def muted():
    try:
        return time.time() < float(open(MUTE).read())
    except (FileNotFoundError, ValueError):
        return False


def spoke_left(window=20.0):
    """Seconds left in which a reply counts without saying "Al" (0 = closed)."""
    try:
        return max(0.0, round(window - (time.time() - float(open(f"{HERE}/.along_spoke").read())), 1))
    except (FileNotFoundError, ValueError):
        return 0.0



# ---------------------------------------------------------------- GPT-Live voice (speech-to-speech; Codex does the work)
LIVE_ON, LIVE_OUT = f"{HERE}/.along_live", f"{HERE}/.along_live_out.jsonl"
LIVE_PROMPT = """You are Al, the voice of two robot arms (left = white, right = orange) on a lab table. Quick and casual.
You cannot see or move anything, and you don't know what the arms are doing or did.
When someone talks to you (they say "Al", or reply to you) about anything but small talk, delegate it and say nothing: the answer comes back as commentary.
That includes tasks, corrections, "yes"/"no"/"ready" answers, and questions about the arms or the scene.
Commentary is your hands' report: say it word for word. It is the only thing you ever say about the arms.
Small talk (hi, thanks): one short sentence. Anything not meant for you: stay silent. Never talk about safety."""


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
        self.utt, self.utt_t, self.pend = "", 0.0, 0.0     # current utterance (page log); pending hand-off time
        self.orphan, self.said_t, self.routed_t = 0.0, 0.0, 0.0                 # addressed utterance GPT-Live did not hand off; Al last spoke
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
        llm.spend("GPT-Live session")
        ws = self.ws = connect("wss://api.openai.com/v1/live/sessions", open_timeout=15, max_size=None,
                               additional_headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"})
        self.send(type="session.start", session={"model": env("ALONG_LIVE_MODEL", "gpt-live-1.1"), "instructions": LIVE_PROMPT,
                  "audio": {"format": {"type": "audio/pcm", "rate": self.RATE}, "output": {"voice": env("ALONG_LIVE_VOICE", "marin")}},
                  "delegation": {"type": "client"}})   # no greeting: Al stays quiet until someone talks
        open(LIVE_ON, "w").write(str(os.getpid())); open(LIVE_OUT, "w").close()
        fmt = ["--raw", "--rate", str(self.RATE), "--channels", "1", "--format", "s16"]
        src = mic_source()
        mic = subprocess.Popen(["pw-record", *fmt, *(["--target", src] if src else []), "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        spk = subprocess.Popen(["pw-play", *fmt, "-"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stop = threading.Event()
        import queue
        spk_q = queue.Queue()

        def pump_spk():                                   # jitter buffer: deltas arrive at ~real-time pace with network
            bps, pre = self.RATE * 2, self.RATE * 2 * 0.15  # jitter; writing them straight through starves pw-play (stutter).
            buf, end, first, loud = bytearray(), 0.0, 0.0, False  # When the pipe has drained, re-prime with 150 ms first.
            while not stop.is_set():
                try:
                    b, l = spk_q.get(timeout=0.02)
                except queue.Empty:
                    b, l = b"", False
                if b:
                    if not buf:
                        first = time.time()
                    buf += b; loud |= l
                now = time.time()
                if buf and (end > now or len(buf) >= pre or now - first > 0.3):
                    try:
                        spk.stdin.write(buf); spk.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        return
                    end = max(end, now) + len(buf) / bps; buf.clear()
                    if loud:                              # keep the mic muted until buffered speech has actually played
                        self.speak_until = max(self.speak_until, end); loud = False

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
                lines = lines[:lines.rfind("\n") + 1]     # only whole lines: a half-written one is read next time
                for l in lines.splitlines():
                    if not l.strip():
                        continue
                    try:
                        text = json.loads(l)["text"]
                    except (ValueError, KeyError) as ex:
                        log("live: bad say line", ex); continue
                    try:
                        self.send(type="session.commentary.append", delegation_id=self.deleg, content=text)
                    except Exception:
                        return
                pos += len(lines)
        started, said, t_start = False, "", time.time()
        try:
            for raw in ws:
                if not self.on.is_set():
                    break
                if reset_t() > t_start:                   # Reset Al: reconnect with an empty context
                    event("sys", "voice context cleared (Reset Al)"); break
                e = json.loads(raw); k = e.get("type")
                if isinstance(e.get("usage"), dict) or isinstance((e.get("response") or {}).get("usage"), dict):
                    with open(f"{HERE}/.along_calls.jsonl", "a") as f_:   # GPT-Live's own cost: in the call log too
                        f_.write(json.dumps({"id": f"live{time.time():.0f}", "t": time.time(), "kind": "live", "model": "gpt-live",
                                             "usage": e.get("usage") or e["response"]["usage"]}) + "\n")
                if k == "session.started" and not started:
                    started = True; self.phase = "listening"; log("live session", e["session"]["id"]); event("sys", "voice session up (GPT-Live)")
                    threading.Thread(target=pump_mic, daemon=True).start()
                    threading.Thread(target=pump_out, daemon=True).start()
                    threading.Thread(target=pump_spk, daemon=True).start()
                elif k == "session.output_audio.delta":
                    b = base64.b64decode(e["delta"])
                    import numpy as np                    # the stream never stops (silence too): mute only on real speech
                    loud = len(b) >= 2 and float(np.sqrt(np.mean(np.frombuffer(b, np.int16).astype(np.float32) ** 2))) > 300
                    spk_q.put((b, loud))
                    if loud:
                        self.speak_until = max(self.speak_until, time.time() + 0.6); self.phase = "muted"
                elif k == "session.output_transcript.delta":
                    said += e["delta"]; self.said_t = time.time()
                    if self.heard.strip() and not self.pend and not self.orphan and time.time() - self.utt_t < 4:
                        self.orphan = time.time()             # it answered, so it was meant for Al: hand-off or we route it
                elif k == "session.input_transcript.delta":
                    self.heard += e["delta"]; self.utt += e["delta"]; self.utt_t = time.time(); self.phase = "speech"
                elif k == "session.delegation.created":         # carries no text: hand over what was heard, once it settles
                    self.deleg, self.pend, self.phase, self.orphan = e["delegation"]["id"], time.time(), "listening", 0.0
                    event("sys", "GPT-Live handed off a request")
                elif k == "error":
                    log("live:", e); event("error", f"live: {e.get('error', e)}")
                if self.pend and (time.time() - self.utt_t > 0.35 or time.time() - self.pend > 0.9):   # last words are in
                    if self.utt.strip():
                        event("heard", self.utt.strip()); self.utt = ""
                    req, self.heard, self.pend = " ".join(self.heard.split())[-400:], "", 0.0
                    if req or time.time() - self.routed_t > 60:   # a late hand-off of something we already routed: drop
                        threading.Thread(target=self.on_text, args=(req or "(inaudible)",), daemon=True).start()
                if self.utt.strip() and time.time() - self.utt_t > 1.0:     # utterance over: log it once
                    u = self.utt.strip(); event("heard", u); self.utt = ""
                    if re.search(r"\b(al|all|hal|al's)\b", u, re.I) or time.time() - self.said_t < 20:
                        self.orphan = time.time()             # meant for Al: if GPT-Live does not hand it off, we do
                if self.orphan and time.time() - self.orphan > 2.5:     # GPT-Live normally hands off within ~2 s
                    self.orphan = 0.0
                    if not self.pend and self.heard.strip():  # GPT-Live answered ("Sure.") but never delegated
                        event("sys", "GPT-Live did not hand this off: routing it ourselves")
                        req, self.heard, self.routed_t = " ".join(self.heard.split())[-400:], "", time.time()
                        threading.Thread(target=self.on_text, args=(req,), kwargs={"answered": True}, daemon=True).start()
                if time.time() > self.speak_until + 0.3:
                    if self.phase == "muted":
                        self.phase = "listening"
                if said.strip() and time.time() - self.said_t > 1.2 and time.time() > self.speak_until + 0.3:
                    event("live", " ".join(said.split())); said = ""      # one line per reply (deltas come word by word)
        finally:
            stop.set()
            try:
                self.send(type="session.close"); ws.close()
            except Exception:
                pass
            mic.kill()
            try:
                spk.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            spk.kill(); log("live session closed"); event("sys", "voice session closed")

# ---------------------------------------------------------------- codex (slow path; see codex_agent.py)
import codex_agent
codex_agent.API_KEY = env("OPENAI_API_KEY") if env("ALONG_CODEX_API") else None   # Codex: the ChatGPT subscription
import llm
llm.API_KEY = env("OPENAI_API_KEY")                       # every Responses call (router, skills, checks, looks)


def interrupt_codex():
    """Stop the running Codex turn and any skill in flight (an open-jawed hold stays where it is)."""
    codex_agent.interrupt()
    subprocess.run(["pkill", "-f", r"^\S*python\S* \S*(along\.py (hold|release|turn|tilt|press|look)|robot\.py (offer|step))"], capture_output=True)
    BUSY.clear(); event("sys", "Codex interrupted (running turn and skills stopped)")


RESET = f"{HERE}/.along_reset"                           # mtime = last "Reset Al": everything before it is forgotten


def reset_t():
    try:
        return os.path.getmtime(RESET)
    except FileNotFoundError:
        return 0.0


def codex_fresh():
    """Reset Al: Codex back to its brief; the router's conversation/activity history, the open episode and the voice
    session (GPT-Live reconnects with an empty context) all start over. Arms stay put."""
    codex_agent.fresh(); open(RESET, "w").close()
    event("sys", "Al reset: Codex, router history, episode and voice context cleared")


def tell(text, fresh=False, wait=False):
    """Hand a task to Codex (headless turn; a running one is replaced). Its final message is spoken."""
    if env("ALONG_DRY"):                                   # test the daemon without Codex
        return log("(dry) ->", text)
    log("->", text); event("tell", ("[fresh] " if fresh else "") + text)
    reply = (lambda m: CODEX_HELD.append(m[:400])) if wait else (lambda m: say(m[:400]))   # wait: the caller speaks it
    return codex_agent.run(text, fresh, on_event=event, on_reply=reply, wait=wait)


CODEX_HELD = []                                          # Codex's reply in a plan: spoken together with "Did that work?"


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


BUSY = threading.Event()                                  # a skill the router started is running
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
        if mic.on.is_set() and not BUSY.is_set() and not codex_agent.busy():
            txt = rstep()
            if txt:
                STATE.update(text=brief_state(txt), t=time.time())
        time.sleep(2.0)


# ---------------------------------------------------------------- router: one fast vision+decision call per request
ROUTE_PROMPT = """You are the fast router of Al, two SO-101 robot arms (left = white, right = orange) on a lab table, run by
voice. A voice model talks to people and hands you each request (speech-to-text: may be garbled or carry room chatter
before it; act on the last thing said to Al). You get the camera images, the arm state and the recent conversation.
Return actions = the steps in order: usually one; a sequence when the request needs several ("hold this and turn it" =
hold, turn; "take it and put it in the cup" = hold, codex). Each step starts when the previous one succeeded.
This is a conversation: later turns of it may give you (a) a learned skill's SKILL.md to expand into concrete steps,
(b) a check: fresh images and a question, (c) a failed step and why. Each time return the steps from that point on
(after a failure: a corrected retry, or a say). You see the images and the scene camera is calibrated: do tasks
yourself, grasps included (e.g. "pick up the cup" = goto above it, open, check, down, close). Ask the person in a say
when it helps ("Hold it a bit closer to me."). Prefer a learned skill when one fits. Fill each
step's fields; unused fields = null. Be decisive: unclear = the most likely action. People here are trained: never warn,
never refuse, no safety talk.

Actions:
- hold: an arm reaches out to what a person holds out (or will), jaws open, closes once it is put in. side = the arm the
  person names, else "auto" (nearer arm). what = the object ("toothbrush"). object = where it is now in the scene image
  (if visible; else null and the arm goes to a default spot).
- release: side opens and goes back to ready (drops what it holds).
- turn: wrist roll of side by deg (+/-; 90 = quarter turn). tilt: wrist flex by deg (+ tips down).
- press: push straight down on a point (button, pump): object = that point, depth_cm (default 4, harder 6-8).
- open: both grippers open (let go of everything). park: empty arms go to rest. reset: everything stops, both arms to rest.
- stop: stop whatever is running now, arms stay where they are ("stop", "wait", "halt", "no").
- step: a short direct move sequence for robot step, ';'-chained: goto <side> x y z (m, that arm's base frame: x forward,
  y to the arm's left, z up, table z -0.02; >= 2 cm), move <side>_<joint> <deg> (relative, >= 5), set <side>_gripper
  <0-100> (0 closed), recall ready|rest <side>, goto <side> px u v z (scene pixel u v at height z m: located for you,
  use it for anything you see), goto <side> base u v z (u v = where the object stands ON THE TABLE (bottom of a bottle):
  x, y from that, z = the height you give: far more accurate than px for anything standing on the table, because a
  wrong height guess with px shifts x a lot (5 cm too high = ~6 cm too far)). Joints: shoulder_pan (+ toward the arm's own right), shoulder_lift,
  elbow_flex, wrist_flex (+ tips down), wrist_roll, gripper. E.g. "open the left gripper" = set left_gripper 100.
  A target a little past the arm's reach goes to the nearest point it reaches; one > 4 cm past it fails (then use the
  other arm, or ask the person to bring it closer). Pick the arm nearer to the object (image-left = orange/right).
- check: question = what to verify in fresh images (e.g. "is the orange tip right above the pump head?"); you then get
  a turn with new images and return the corrected / remaining steps. Use it where precision matters, before the risky step.
- skill: a learned skill (list below): skill = its name, args = the values its args line asks for (free text). You
  then get a turn with its SKILL.md and return its concrete steps.
- answer: only talk: questions about the scene, the arms. say = the answer. Greetings, thanks and small talk: a short reply,
  or "" if Al's last line in the conversation already answered it
  (the voice model already answers those itself; saying it again doubles it).
- codex: only a long multi-step task the actions above can't express ("tidy the table"): codex_prompt = a
  self-contained instruction for the slower agent with all you know (object, where it is in px, which arm).
  codex_fresh = true when the task has nothing to do with what Codex did recently (it then starts from a clean session,
  faster and not confused by old context); false to continue its context ("now do the same with the other arm").
- none: not addressed to Al (chatter, other conversations, other languages not meant for Al): actions = [none], say = "".
  Also none for a fragment or reaction that asks for nothing ("What", "The grippers", "Oh", "left" after Al already acted
  on it) while a plan or Codex is running: a new action interrupts the running one, so only act on a clear new request.
Al's lines in the recent conversation come from the voice model, which cannot see the arms and sometimes makes things up
("Pressed." before anything moved, "put it between my jaws" for a press): trust the arm state, images and activity, and
correct it in say when it told the person something wrong.

episode = how the request relates to the open episode (the goal being worked on, given in the context): "new" (a new
goal, or none is open), "continue" (same goal: retry, a correction, "now the other arm"), "confirm" (the person says it
worked: "yes", "perfect", "thanks, that worked"), "deny" (it didn't: "no", "you missed"),
"none" (chatter, questions, small talk). In later turns of the conversation: "continue".

say = what Al says when that step is done: ONE short sentence ("Done.", "Here you go.", an answer of at most ~12 words), or
"" for nothing (in a sequence usually only the last step speaks; a last step with "" says "Done."). For hold leave it ""
(the hold reports itself). object: u, v = pixel in the scene image (800x450) of the point to grab (a long thing: the
middle of its handle, not the bristles/head), dist_cm = its distance from the scene camera, height_cm = its height above
the table, pose = "upright" or "flat" (held horizontally), axis_deg = its long axis in the scene image (0 horizontal,
90 vertical). Scale: the scene camera is 33 cm above the table and 70 cm from each arm base (bases 21 cm apart); the
scene camera faces the arms: the orange/right arm is on image-left. Wrist images look out between each arm's jaws."""

STEP_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["action", "side", "what", "deg", "depth_cm", "step", "object", "skill", "args", "question", "say",
                 "codex_prompt", "codex_fresh"],
    "properties": {
        "action": {"type": "string", "enum": ["hold", "release", "turn", "tilt", "press", "open", "park", "reset", "stop",
                                              "step", "check", "skill", "answer", "codex", "none"]},
        "side": {"type": ["string", "null"], "enum": ["left", "right", "auto", None]},
        "what": {"type": ["string", "null"]}, "deg": {"type": ["number", "null"]}, "depth_cm": {"type": ["number", "null"]},
        "step": {"type": ["string", "null"]},
        "object": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
            "required": ["u", "v", "dist_cm", "height_cm", "pose", "axis_deg"],
            "properties": {"u": {"type": "number"}, "v": {"type": "number"}, "dist_cm": {"type": ["number", "null"]},
                           "height_cm": {"type": ["number", "null"]}, "pose": {"type": "string", "enum": ["upright", "flat"]},
                           "axis_deg": {"type": ["number", "null"]}}}]},
        "skill": {"type": ["string", "null"]}, "args": {"type": ["string", "null"]}, "question": {"type": ["string", "null"]},
        "say": {"type": "string"}, "codex_prompt": {"type": ["string", "null"]}, "codex_fresh": {"type": ["boolean", "null"]}}}

ROUTE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["episode", "actions"],
                "properties": {"episode": {"type": "string", "enum": ["new", "continue", "confirm", "deny", "none"]},
                               "actions": {"type": "array", "items": STEP_SCHEMA}}}

RUN = {"p": None, "what": ""}                               # the skill process the router started (stop kills it)
PLAN = {"gen": 0}                                           # bumped by every new plan: an older sequence stops at its next step
MAX_TURNS, MAX_DEPTH = 8, 3
HEARD = {"t": 0.0}                                           # when the last request was heard                                 # model turns per request; skills inside skills
EP_STALE = 600                                               # s: an episode nobody touched for this long is closed
EP = {"id": None, "request": "", "status": "", "codex": False, "skills": []}   # the episode: one goal until confirmed/dropped


def route_prompt():
    """ROUTE_PROMPT + the one-line index of the learned skills (their SKILL.md only comes in a skill turn)."""
    try:
        import skills
        idx = skills.index_text()
    except Exception as e:
        idx = f"(skills unavailable: {e})"
    return ROUTE_PROMPT + "\n\nLearned skills (action skill; skill = the name, args = what its args line asks for):\n" + idx


def frames(cams=("scene", "left", "right")):
    """Live camera thumbs as (label, data URL) (scene = the 800x450 tile, so pixels go straight into locate)."""
    from so101 import Robot
    bot, out = Robot(), []
    for c in cams:
        try:
            out.append((f"{c} camera", llm.image_url(bot.thumb(c, *((W, H) if c == "scene" else (640, 360))))))
        except Exception as e:
            log("frames: no", c, e)
    return out


def recent():
    """Recent conversation and recent activity (router decisions, Codex tasks + commands + turn ends, skill results)."""
    try:
        ev = [e for e in (json.loads(l) for l in open(EVENTS).readlines()[-150:] if l.strip()) if e["t"] > reset_t()]
    except FileNotFoundError:
        return "", ""
    lab = {"heard": "person", "say": "Al", "live": "Al"}
    conv = "\n".join(f"{lab[e['kind']]}: {e['text'][:160]}" for e in ev if e["kind"] in lab)[-900:]
    acts = [f"{time.time() - e['t']:.0f} s ago {e['kind']}: {e['text'][:200]}" for e in ev
            if e["kind"] in ("route", "turn", "tell", "skill") or e["kind"] == "codex" and not e["text"].startswith("→")]
    return conv, "\n".join(acts[-10:])


def summary(steps):
    return " → ".join(f"skill:{s.get('skill')}" if s["action"] == "skill" else s["action"] for s in steps) or "[]"


def episode(status, **k):
    EP.update(status=status, touched=time.time(), **k)
    event("episode", json.dumps({"id": EP["id"], "status": status, "request": EP["request"][:200], "codex": EP["codex"],
                                 "skills": EP["skills"]}))


def episode_ctx():
    if EP["id"] and EP.get("t", 0) < reset_t():           # Reset Al since it opened: forgotten
        EP.update(id=None, request="", status="", codex=False, skills=[])
    if (EP["id"] and EP["status"] not in ("confirmed", "dropped") and not BUSY.is_set() and not codex_agent.busy()
            and time.time() - EP.get("touched", 0) > EP_STALE):    # nobody touched it for 10 min: a new request starts fresh
        episode("dropped"); event("sys", f"episode \"{EP['request'][:80]}\" closed: untouched for {EP_STALE // 60} min")
    if LEARN.get("names") and time.time() - LEARN["t"] < LEARN_WAIT:
        return (f"Al just asked whether to remember a new way of doing it (\"{LEARN['summary']}\"): yes / keep it = "
                f"episode confirm, no / don't = episode deny.")
    if not EP["id"] or EP["status"] in ("confirmed", "dropped"):
        return "Open episode: none."
    return (f"Open episode: \"{EP['request'][:200]}\" ({EP['status']}{', Al asked whether it worked' if EP['status'] == 'asked' else ''}"
            f"; Codex needed: {'yes' if EP['codex'] else 'no'}; skills used: {', '.join(EP['skills']) or 'none'}).")


def route(text):
    """Turn 1 of a request's conversation: transcript + state + 3 images -> {episode, actions: [...]} (+ "_rid")."""
    t0 = time.time()
    run = f"running now: {RUN['what']}" if RUN["p"] and RUN["p"].poll() is None else "nothing running"
    conv, acts = recent(); cx = codex_agent.state()
    ctx = (f"Request (last thing heard): {text}\n\n{episode_ctx()}\n\nArm state: {STATE['text'] or 'unknown'} "
           f"({time.time() - STATE['t']:.0f} s old); held: {json.dumps(rd_held())}; {run}; Codex "
           f"{'is busy with: ' + cx['task'][:200] if cx['pid'] else 'idle'}"
           f"{'; its last task: ' + cx['last']['task'][:200] + ' -> ' + repr(cx['last']['reply'][:120]) if cx['last'] else ''}."
           f"\n\nRecent conversation:\n{conv}\n\nRecent activity (router/turn = you, tell = task sent to Codex, codex = its "
           f"commands and turns):\n{acts}")
    out, rid = llm.call("route", [ctx] + frames(), instructions=route_prompt(), schema=ROUTE_SCHEMA, timeout=30)
    event("route", f"({time.time() - t0:.1f} s) {out['episode']}: {summary(out['actions'])} "
          + json.dumps([{k: v for k, v in s.items() if v not in (None, "")} for s in out["actions"]]))
    return {**out, "_rid": rid}


def turn(kind, msg, rid):
    """A later turn of the same conversation (skill expansion, check, failure): fresh state + images -> steps."""
    t0 = time.time()
    STATE.update(text=brief_state(rstep()), t=time.time())
    out, rid = llm.call(kind, [f"{msg}\n\nArm state now: {STATE['text']}; held {json.dumps(rd_held())}."] + frames(),
                        instructions=route_prompt(), schema=ROUTE_SCHEMA, prev=rid, episode=EP["id"], timeout=30)
    steps = [s for s in out["actions"] if s["action"] != "none"]
    event("turn", f"{kind} ({time.time() - t0:.1f} s): {summary(steps)} "
          + json.dumps([{k: v for k, v in s.items() if v not in (None, "")} for s in steps]))
    return steps, rid


def robot_errs(out):
    """`robot step` reports refused commands (unreachable, outside the safe box, creeping) as ERR lines, exit code 0.
    "too small" ones are left out: for the engine's own steps they mean the arm is already there."""
    return [e for e in re.findall(r"^ERR\s+(.*)$", out or "", re.M) if "too small" not in e]


def err_kind(e):
    """robotd error -> blocked (something stopped the arm) | reach | system (hardware) | other (a bad command)."""
    if "tip ended" in e:
        return "blocked"
    if "outside safe box" in e:
        return "reach"
    if "torque is OFF" in e or "camera" in e:
        return "system"
    return "other"


def err_why(what, errs):
    """What the model is told about robotd errors: the truth per kind, not a generic "refused"."""
    out = []
    for e in errs:
        k = err_kind(e)
        if k == "blocked":
            cm = re.search(r"tip ended ([\d.]+) cm", e)
            out.append(f"the arm was BLOCKED {cm.group(1) if cm else 'some'} cm short of the goal: it touched something "
                       f"(the target, or something in front of it, e.g. a person's hand). Look at fresh images before moving "
                       f"again; never repeat the same move or the same pixel at another height")
        else:
            out.append(f"robotd refused it: {e}")
    return f"`{what}`: " + "; ".join(out)


def act(plan, text, answered=False):
    """Run the router's plan in its own thread (latest request wins: a newer plan interrupts this one); keeps the episode.
    answered = GPT-Live already replied itself (it never handed off): no extra talk."""
    steps = [st for st in plan["actions"] if st["action"] != "none"]   # GPT-Live only says "Let me check." itself
    ep, real = plan.get("episode", "new"), any(st["action"] != "answer" for st in steps)
    if LEARN.get("names") and ep in ("confirm", "deny"):  # the answer to "Should I remember that?"
        learn_answer(ep == "confirm")
        if not real:
            return
        ep = "new"
    if ep == "confirm" and EP["id"] and EP["status"] not in ("confirmed", "dropped"):
        confirm_episode()
    elif ep == "deny" and EP["id"]:
        episode("open")
    elif real and (ep == "new" or not EP["id"] or EP["status"] in ("confirmed", "dropped")):
        if EP["id"] and EP["status"] not in ("confirmed", "dropped", "done"):
            episode("dropped")
        EP.update(id=f"e{time.time():.0f}", request=text, codex=False, skills=[], t=time.time()); episode("open")
    if ep != "none" and EP["id"]:
        EP["touched"] = time.time()
    if plan.get("_rid") and ep != "none" and EP["id"]:
        llm.tag(plan["_rid"], EP["id"])
    if not steps:
        return
    if all(st["action"] == "answer" for st in steps):     # only talk: never interrupts what runs
        return [say(st["say"].strip()) for st in steps if (st.get("say") or "").strip()]   # "" = GPT-Live answered it
    if (RUN["p"] and RUN["p"].poll() is None and RUN["what"].startswith("along hold")
            and all(st["action"] in ("hold", "answer") or st["action"] == "step"
                    and re.fullmatch(r"\s*set (left|right)_gripper 0\s*", st.get("step") or "") for st in steps)):
        open(f"{HERE}/.along_grab", "w").close()          # "it's in": the waiting hold closes now and checks HOLDING itself
        event("skill", "hold: told the object is in, closing"); return
    PLAN["gen"] += 1; gen = PLAN["gen"]
    if codex_agent.busy() or RUN["p"] and RUN["p"].poll() is None or BUSY.is_set():
        interrupt_codex()                                   # a new request replaces whatever runs

    def go():
        BUSY.set()
        try:
            run_plan(steps, text, plan.get("_rid"), gen)
        except Exception as e:
            event("error", f"plan: {type(e).__name__}: {e}"); say("Something went wrong.")
        finally:
            if PLAN["gen"] == gen:
                BUSY.clear()
    threading.Thread(target=go, daemon=True).start()


def run_plan(steps, text, rid, gen, cli=False):
    """Execute steps as a stack of frames (the request, then one per expanded skill). skill / check / a failed step =
    a new model turn in the same conversation. cli = `along skill` (run by Codex): a codex step ends it with exit 1.
    Returns True when everything ran."""
    stack, done, turns, codex_ran = [{"skill": None, "steps": list(steps), "failed": False}], [], 0, False

    def replan(kind, msg):
        nonlocal rid, turns
        turns += 1
        if turns > MAX_TURNS:
            event("sys", f"{MAX_TURNS} model turns for one request: giving up")
            for f in stack:
                if f["skill"] and not f["failed"]:
                    f["failed"] = True; skills_record(f["skill"], "fail")
            return None
        t0 = time.time()
        new, rid = turn(kind, f"{msg}\n\nDone so far: {'; '.join(done) or 'nothing'}.", rid)
        if new and HEARD["t"] > t0:                       # the person spoke meanwhile: that request answers itself,
            stale = [st for st in new if st["action"] == "answer"]   # this turn's talk is out of date
            if stale:
                event("sys", "dropped (the person spoke meanwhile): " + " / ".join(st.get("say") or "" for st in stale))
            new = [st for st in new if st["action"] != "answer"]
        return new

    while stack:
        if PLAN["gen"] != gen:
            return event("sys", "plan replaced by a newer request")
        fr = stack[-1]
        if not fr["steps"]:
            stack.pop()
            if fr["skill"] and not fr["failed"]:
                skills_record(fr["skill"], "win")
            continue
        st = fr["steps"].pop(0)
        a, where = st["action"], " › ".join(f["skill"] for f in stack if f["skill"])
        last = all(not f["steps"] for f in stack)
        if a == "skill":
            name, chain = (st.get("skill") or "").strip().removeprefix("skill:"), [f["skill"] for f in stack if f["skill"]]
            import skills
            sk = skills.load(name)
            why = ("no such skill" if not sk else "it is already running (a cycle)" if name in chain
                   else f"skills nest at most {MAX_DEPTH} deep" if len(chain) >= MAX_DEPTH else "")
            if why:
                new = replan("fail", f"skill:{name} can't run: {why}. Return what to do instead.")
                if new is None:
                    return say("Hm, I'm stuck on this one.")
                fr["steps"] = new; continue
            skills_record(name, "use"); EP["skills"].append(name)
            event("skill", f"skill:{name} ({st.get('args') or 'no args'}){' inside ' + where if where else ''}")
            new = replan("skill", f"Expand skill:{name} (args: {st.get('args') or 'none'}; chain: "
                         f"{' › '.join(chain + [name])}) into concrete steps for the current scene (fresh images below): "
                         f"plan them yourself from its goal, strategy and lessons. Your say lines are spoken.\n\nSKILL.md:\n{sk['body']}")
            if new is None:
                return say("Hm, I'm stuck on this one.")
            stack.append({"skill": name, "steps": new, "failed": False}); continue
        if a == "check":
            event("check", st.get("question") or "")
            new = replan("check", f"Check{' in skill:' + where if where else ''}: {st.get('question')}\nFresh images below. "
                         f"Still planned after this check: {summary(fr['steps'])}. Return the steps from here: a correction "
                         f"first if needed, then the rest (as planned or adjusted); [] if this part is complete.")
            if new is None:
                return say("Hm, I'm stuck on this one.")
            fr["steps"] = new; continue
        if a == "codex":
            if cli:                                       # Codex ran this skill: it takes over itself
                print(f"skill needs the slow path here ({where}): {st.get('codex_prompt')}. Done: {'; '.join(done)}")
                return False
            codex_ran = True; EP["codex"] = True
            for f in stack:                               # the skills it ran inside did not manage it alone
                if f["skill"] and not f["failed"]:
                    f["failed"] = True; skills_record(f["skill"], "fail")
        ok, info = do_step(st, text, done, last, cli)
        if ok:
            continue
        if info is None or PLAN["gen"] != gen:            # stopped / replaced: say nothing more
            return False
        event("skill", f"failed{' in skill:' + where if where else ''}: {info[:240]}")
        new = replan("fail", f"Step failed{' in skill:' + where if where else ''}: {summary([st])} "
                     f"{json.dumps({k: v for k, v in st.items() if v not in (None, '')})}\nWhy: {info}\n"
                     f"Return what to do now: a corrected retry, or a say.")
        if new is None:
            say("Hm, I'm stuck on this one."); return False
        if cli and any(s["action"] == "codex" for s in new):
            print(f"skill failed ({where}): {info}. Done: {'; '.join(done)}"); return False
        fr["steps"] = new
    if codex_ran and EP["id"] and EP["codex"] and not cli:               # Codex had to do it: only the person can say whether it worked
        last = codex_agent.state().get("last") or {}
        if last.get("ok") and EP["status"] != "asked":
            say("Did that work?"); episode("asked")
    elif EP["id"] and not cli and EP["status"] == "open":
        episode("done")
    return True


def skills_record(name, outcome):
    try:
        import skills
        skills.record(name, outcome)
    except Exception as e:
        log("skills.record", e)


def confirm_episode():
    """The person said it worked. If Codex had to solve it, a forked Codex thread turns it into a skill (or patches one)."""
    episode("confirmed")
    if not EP["codex"] or not codex_agent.state()["thread"]:
        return
    import skills
    snap, used = skills.snapshot(), ", ".join(dict.fromkeys(EP["skills"])) or "none"
    task = open(f"{HERE}/prompts/refine.md").read().format(used=used, index=skills.index_text()) + "\n\n" + episode_log()
    event("refine", f"learning from \"{EP['request'][:120]}\" (skills used: {used})")

    def finished(reply):
        ch = skills.changes_since(snap)
        event("refine", "; ".join(f"skill {n} {k}" for n, k in ch) if ch else f"no skill change: {(reply or '')[:160]}")
        for n, _ in ch:                                   # skills must not keep growing (prompts/refine.md limits)
            try:
                body = open(f"{HERE}/skills/{n}/SKILL.md").read()
            except FileNotFoundError:
                continue
            if len([l for l in body.splitlines() if l.strip()]) > 14 or "## Lessons" in body:
                event("error", f"skill {n} breaks the limits (> 14 lines or a Lessons section): check it, "
                               f"or undo with along skill-undo {n}")
        if ch:                                            # nothing is kept unless the person says yes
            m = re.search(r"^(?:created|patched) [\w-]+:\s*(.+)$", reply or "", re.M)
            summary = (m.group(1).strip() if m else f"how to {ch[0][0].replace('-', ' ')}")[:160]
            LEARN.update(names=[n for n, _ in ch], summary=summary, t=time.time())
            json.dump(LEARN, open(LEARN_F, "w"))          # a restart before the answer undoes it (learn_pending)
            say(f"I'd remember this for next time: {summary.rstrip('.')}. Should I keep it?")
            t_ = LEARN["t"]; threading.Timer(LEARN_WAIT, lambda: LEARN.get("t") == t_ and learn_answer(None)).start()
    codex_agent.fork(task, on_event=event, on_done=lambda reply: threading.Thread(
        target=finished_when_idle, args=(finished, reply), daemon=True).start(), fresh=True)


def finished_when_idle(finished, reply, wait=300):
    """Ask "keep it?" only when nothing runs and no "Did that work?" is open; after `wait` s ask anyway."""
    t0 = time.time()
    while time.time() - t0 < wait and (BUSY.is_set() or codex_agent.busy() or RUN["p"] and RUN["p"].poll() is None
                                       or EP.get("status") == "asked"):    # one question at a time: "Did that work?" first
        time.sleep(1)
    finished(reply)


def episode_log(limit=6000):
    """What happened in the confirmed episode, from the event log: the refine reads this, not the whole Codex thread."""
    keep = ("heard", "route", "turn", "check", "skill", "tell", "codex", "say", "episode", "error")
    try:
        ev = [json.loads(l) for l in open(EVENTS).readlines()[-600:] if l.strip()]
    except FileNotFoundError:
        ev = []
    lines = [f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['kind']}: {' '.join(str(e['text']).split())[:300]}"
             for e in ev if e.get("t", 0) >= EP.get("t", 0) - 1 and e.get("kind") in keep]
    text = "\n".join(lines)
    import glob
    th = codex_agent.state().get("thread") or ""
    roll = (glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*{th}.jsonl")) or [""])[0] if th else ""
    t0 = time.strftime("%H:%M:%S", time.localtime(EP.get("t", 0)))
    return (f"Episode log (request: \"{EP['request'][:200]}\"):\n" + (text[-limit:] if len(text) > limit else text) +
            f"\n\nNeed a detail the log lacks (Codex's commands and their full output, its reasoning)? Search, don't read whole: "
            f".along_codex.log (readable transcript of every Codex turn; this episode starts at {t0})"
            + (f" and the raw Codex thread {roll}" if roll else "") + ".")


LEARN = {}                                                # a learned skill change waiting for the person's yes / no
LEARN_WAIT = 120                                          # s: no answer by then = not kept


LEARN_F = f"{HERE}/.along_learn.json"


def learn_pending():
    """At start: a skill change still waiting for its yes (along restarted meanwhile) is undone."""
    try:
        LEARN.update(json.load(open(LEARN_F)))
    except (FileNotFoundError, ValueError):
        return
    learn_answer(None)


def learn_answer(keep):
    """keep: True = the person said yes; False = no; None = no answer in time (not kept either)."""
    names = LEARN.pop("names", None)
    try:
        os.remove(LEARN_F)
    except FileNotFoundError:
        pass
    if not names:
        return
    if keep:
        event("refine", f"kept: {', '.join(names)}"); say("Okay, I'll remember that.")
        return
    import skills
    for n in names:
        event("refine", f"{'not confirmed' if keep is None else 'declined'}: {skills.undo(n)}")
    if keep is False:
        say("Okay, I won't.")


def geo_off(x, y, z):
    """How far (m) a point is outside robotd's safe box (x 0.08..0.38, |y| <= 0.2, z 0..0.25); 0 = inside. Orientation-free:
    the IK search in reachable() keeps the current gripper orientation and fails from poses like rest."""
    return (max(0.08 - x, 0, x - 0.38) ** 2 + max(abs(y) - 0.2, 0) ** 2 + max(-z, 0, z - 0.25) ** 2) ** 0.5


def out_of_reach(side, x, y, z, detail, say, cli):
    """A target > 4 cm past the arm's reach. The other arm reaches it -> the model is told to use it (exact command);
    else Al asks for it to be brought closer and the plan ends: a lower height on the same pixel only slides the target
    along the camera ray onto whatever is in front of it (a hand)."""
    import collide, numpy as np
    s_ = collide.setup(); other = "left" if side == "right" else "right"
    w = collide.base_T(side, s_) @ np.r_[x, y, z, 1.0]
    ox, oy, oz = (np.linalg.inv(collide.base_T(other, s_)) @ w)[:3]
    event("skill", detail)
    if geo_off(ox, oy, oz) < 0.02:                        # the other arm: inside its box (it turns first)
        return False, (f"{detail}. The {other} arm reaches that point: redo it with the {other} arm "
                       f"(goto {other} {ox:.3f} {oy:.3f} {oz:.3f}); not the same arm at another height.")
    if cli:
        return False, f"{detail}, and the other arm's too: ask the person to bring it closer."
    say("That's out of my reach. Can you bring it closer?")
    return False, None


def arm_pose():
    """Tips + grippers now (to tell whether Codex moved anything)."""
    try:
        from so101 import Robot
        st = Robot().state()
        return {k: v for k, v in st["measured"].items()}
    except Exception:
        return None


def arm_moved(before):
    after = arm_pose()
    return before is None or after is None or any(abs(after[k] - before.get(k, after[k])) > 3 for k in after)


PX_FAR = {}                                               # plan generation -> a px goto was already told it is out of box


def gen_now():
    return PLAN["gen"]


def do_step(r, text, done, last, cli=False):
    """One built-in step, blocking. -> (True, None) = go on; (False, None) = stopped (say nothing more);
    (False, why) = it failed (the caller asks the model what now). done: what earlier steps did (for Codex)."""
    a, side, say_done = r["action"], r.get("side") or "auto", (r.get("say") or "").strip()
    speak = say_done or ("Done." if last else "")

    def say(t):                                           # what the person was told goes to Codex too
        globals()["say"](t); done.append(f'Al told the person: "{t}"')
    if a == "answer":
        say(say_done or "Hm?"); return True, None
    if a == "stop":
        interrupt_codex(); say(say_done or "Stopped."); return True, None
    if a == "codex":
        if done:                                          # the arms moved: fresh state for Codex
            STATE.update(text=brief_state(rstep()), t=time.time())
        age = time.time() - STATE["t"]
        now = f"now: {STATE['text']}; snap_all.jpg {age:.0f} s old" if age < 30 else "arm state: run robot step"
        before = f"; already done for this request: {'; '.join(done)}" if done else ""
        tips0 = arm_pose()
        tell(f"[task] {r.get('codex_prompt') or text}   || heard: {text}{before}; {now}; held {json.dumps(rd_held())}",
             fresh=bool(r.get("codex_fresh")), wait=True)
        last_ = codex_agent.state().get("last") or {}
        rep = " ".join(CODEX_HELD).strip(); CODEX_HELD.clear()
        moved = arm_moved(tips0)                          # Codex only talked (asked for something): no "Did that work?"
        if not moved:
            EP["codex"] = False
        if last and last_.get("ok") and moved and EP["id"] and not cli:   # one line: two in a row and GPT-Live drops the second
            say(f"{rep} Did that work?".strip()); episode("asked")
        elif rep:
            say(rep)
        done.append(f"codex: {last_.get('reply', '')[:200]}")
        return (True, None) if last_.get("ok") else (False, None)
    o = r.get("object")
    if a == "hold":
        if o:
            json.dump({"t": time.time(), **o}, open(AIM + ".tmp", "w")); os.replace(AIM + ".tmp", AIM)
        else:
            json.dump({"t": time.time(), "u": None}, open(AIM, "w"))
        args = ["hold"] + ([side] if side in ("left", "right") else []) + ([r["what"]] if r.get("what") else [])
    elif a in ("release", "turn", "tilt"):
        if side not in ("left", "right"):
            h = rd_held(); side = next(iter(h)) if len(h) == 1 else "right"
        args = [a, side] + ([str(r.get("deg") or 90)] if a != "release" else [])
    elif a == "press":
        if not o:
            return False, "press needs object (the point to press, as scene pixel + height)"
        from so101 import Robot
        h = (o.get("height_cm") or 0) / 100 - 0.02
        loc = Robot().locate(o["u"], o["v"], h)
        side = side if side in ("left", "right") else min(("left", "right"), key=lambda sd: loc[sd][0] ** 2 + loc[sd][1] ** 2)
        x, y, z = loc[side]
        if geo_off(x, y, z + 0.04) > 0.04:
            return out_of_reach(side, x, y, z, f"press at {x:.2f} {y:+.2f} {z:.2f} is past the {side} arm's reach", say, cli)
        args = ["press", side, *(f"{v:.3f}" for v in loc[side]), str(r.get("depth_cm") or 4)]
    elif a in ("open", "park", "reset"):
        args = [a]
    elif a == "step":
        cmds = [c.strip() for c in (r.get("step") or "").split(";") if c.strip()]
        if not cmds or any(c.split()[0] not in ("goto", "move", "set", "recall", "nudge") for c in cmds):
            return False, f"step allows only goto/move/set/recall/nudge, got: {r.get('step')!r}"
        args = None
    else:
        return True, None
    what = "along " + " ".join(args) if args else "robot step " + "; ".join(cmds)
    if args is None:
        RUN.update(p=None, what="step"); out = ""
        for c in cmds:                                    # one robot step each: a ';' chain sums into one target
            g = c.split()
            src, c0 = (g[2] if g[0] == "goto" and len(g) == 6 else "xyz"), c
            if g[0] == "goto" and len(g) == 6 and g[2] == "px":   # goto <side> px u v z: the scene pixel at height z
                from so101 import Robot
                g = ["goto", g[1], *(f"{v:.3f}" for v in Robot().locate(float(g[3]), float(g[4]), float(g[5]))[g[1]])]
                c = " ".join(g)
            elif g[0] == "goto" and len(g) == 6 and g[2] == "base":   # goto <side> base u v z: x, y from where the object
                from so101 import Robot                   # stands on the table (known height: accurate), z as given
                bx, by, _ = Robot().locate(float(g[3]), float(g[4]))[g[1]]
                g = ["goto", g[1], f"{bx:.3f}", f"{by:.3f}", g[5]]; c = " ".join(g)
            if g[0] == "goto" and len(g) == 5:            # coarse guesses: the nearest point the arm really reaches
                x, y, z = map(float, g[2:5]); rx, ry, rz = reachable(g[1], x, y, z, drop=0.02)   # pull back, barely lower
                off = ((rx - x) ** 2 + (ry - y) ** 2 + (rz - z) ** 2) ** 0.5 if rx != float("inf") else 0.0  # none found: robotd decides
                far = geo_off(x, y, z)
                if far > 0.04 and src == "px" and not PX_FAR.get(gen_now()):   # a pixel is a ray: the height guess may be wrong
                    PX_FAR[gen_now()] = True
                    return False, (f"{c0} -> {x:.2f} {y:+.2f} "
                                   f"{z:.2f} is {far * 100:.0f} cm outside the arm's box. A scene pixel at a guessed height "
                                   f"slides along the camera ray: a too-high height puts it too far forward. Locate it from "
                                   f"where the object stands instead: goto <side> base u v z (u v = its foot on the table). "
                                   f"Do not retry the same pixel at another height.")
                if far > 0.04 or off > 0.04 and far > 0:  # far out of reach: decided here, not by the model
                    return out_of_reach(g[1], x, y, z, f"{c} is {max(far, off) * 100:.0f} cm past the {g[1]} arm's reach", say, cli)
                if 0.005 < off <= 0.04:
                    c = f"goto {g[1]} {rx:.3f} {ry:.3f} {rz:.3f}"; event("skill", f"{' '.join(g)} is past the arm's reach: {c}")
            o_ = rstep(c)
            if g[0] == "goto" and len(g) == 5 and any("ik fails at waypoint" in e for e in robot_errs(o_)):
                from so101 import Robot                   # the straight line flips the wrist: go via a point above
                tx, ty, tz = map(float, c.split()[2:5]); cx, cy, cz = getattr(Robot(), g[1]).tip()
                mid = f"goto {g[1]} {(cx + tx) / 2:.3f} {(cy + ty) / 2:.3f} {max(cz, tz) + 0.03:.3f}"
                event("skill", f"{c}: straight line refused, going via {mid}")
                o_ = rstep(mid) + "\n" + rstep(c)
            out += o_ + "\n"
            if robot_errs(o_):
                break
        ok, killed = bool(out.strip()), not out.strip()    # empty = the step was killed (stop / newer request)
    else:
        p = subprocess.Popen([sys.executable, __file__, *args], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        RUN.update(p=p, what=what); out = p.communicate()[0]; ok, killed = p.returncode == 0, p.returncode < 0
    if killed:                                            # stop / a newer request: say nothing, end the plan
        event("skill", f"stopped: {what}"); return False, None
    errs = robot_errs(out)
    if errs:                                              # never "Done."
        if any(err_kind(e) == "system" for e in errs) and not cli:   # hardware: say it, no model retry
            e = next(e for e in errs if err_kind(e) == "system")
            event("skill", f"{what}: {e}"); say(f"I can't right now: {e.split(' (')[0]}."); return False, None
        if any(err_kind(e) == "reach" for e in errs) and not cli:
            say("That's out of my reach. Can you bring it closer?"); event("skill", f"{what}: outside the safe box")
            return False, None
        return False, err_why(what, errs)
    event("skill", ("done: " if ok else "failed: ") + " ".join(out.split())[-240:])
    if a == "hold":
        if re.search(r"""['"]holding['"]: (True|true)""", out):
            say(say_done or "Got it."); done.append(f"{what}: holding it"); return True, None
        return False, f"`{what}`: {'robot error' if 'FAILED' in out else 'missed (nothing between the jaws)'}: " + " ".join(out.split())[-200:]
    if not ok:
        return False, f"`{what}` failed: " + " ".join(out.split())[-300:]
    if speak:
        say(speak)
    done.append(f"{what}: done"); return True, None


def run_manual():
    """Manual on/off (`along on` / `along off`, or the page buttons) instead of the scene detector."""
    on, last_voice = False, 0.0

    def heard(text, answered=False):
        nonlocal last_voice
        last_voice = HEARD["t"] = time.time()
        age = time.time() - STATE["t"]
        ctx = f"   || now: {STATE['text']}; held {json.dumps(rd_held())}; snap_all.jpg {age:.0f} s old" if age < 6 else ""
        if env("ALONG_ROUTER", "1") != "0":
            try:
                return act(route(text), text, answered)
            except Exception as e:
                log("router failed", e); event("error", f"router: {e}; Codex takes it")
        tell(f"[voice] {text}{ctx}")
    if not env("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY missing (.env): GPT-Live is the voice")
    mic = LiveVoice(heard)
    threading.Thread(target=watch, args=(mic,), daemon=True).start()
    log("off (GPT-Live voice); waiting for `along on`")
    while True:
        t, want = time.time(), os.path.exists(ON)
        if want and not on:
            on = True; log("ON"); event("wake", "switched on")
            tone(880); mic.on.set()
        elif on and not want:
            on = False; mic.on.clear(); log("OFF"); event("idle", "switched off"); tone(660)
            interrupt_codex()                                         # off = interrupt Codex + no voice; arms stay put
        json.dump({"t": t, "active": on, "why": ["switched on"] if on else [], "manual": True, "listening": mic.on.is_set(),
                   "mic": mic.phase, "vad": round(mic.prob, 2), "stt": mic.stt_busy, "reply_open": spoke_left(), "speaking": muted(),
                   "voice": "GPT-Live", "all": [], "new": [], "held": rd_held(), "hits": [], "last_voice": last_voice,
                   "busy": BUSY.is_set() or codex_agent.busy(), "state_t": STATE["t"], "state": STATE["text"]}, open(SEEN, "w"))
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
            say("Hi, I'm Al. Need a hand?")
            tone(880); mic.on.set()
            tell(f"[along] Woke up: {names} (the cheap detector only says where; look at the image yourself to see what it is). "
                 "I already greeted them and asked if they need a hand; the mic is on "
                 "while an object of interest is in the scene or in the jaws. Wait for their [voice] request.")
        elif active and not why:
            active = False
            mic.on.clear()                                 # nothing of interest: no voice commands go through
            log("back to idle"); event("idle"); tone(660)
            tell("[along] Back to idle: no new object in the scene and nothing in the jaws; the mic is off. "
                 "Park the arms (robot step \"recall rest\"), silently.")
        json.dump({"t": t, "active": active, "why": why, "listening": mic.on.is_set(), "mic": mic.phase, "vad": round(mic.prob, 2), "stt": mic.stt_busy,
                   "reply_open": spoke_left(), "speaking": muted(), "all": dets,
                   "new": new, "arm_px": getattr(eyes, "arm", []), "held": rd_held(), "hits": hits, "change": eyes.frac, "roi": ROI, "last_new": last_new, "last_voice": last_voice}, open(SEEN, "w"))
        time.sleep(max(0.0, 0.5 - (time.time() - t)))


def look(question, cams=("scene", "left", "right")):
    """Ask a vision model (GPT 6.1 sol ultrafast via the API; `codex exec` without a key) about the newest frames. Scene is sent as the
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
    t0, model = time.time(), env("ALONG_LOOK_MODEL", "gpt-6.1-sol" if env("OPENAI_API_KEY") else "gpt-6.1-terra")
    event("look", f"[{model}{' ' + env('ALONG_LOOK_TIER', 'ultrafast') if env('OPENAI_API_KEY') else ''} · {', '.join(cams)}] {question}")
    if env("OPENAI_API_KEY"):                              # lean: just the images + question
        ans, _ = llm.call("look", [brief] + [(f"{os.path.basename(p)[:-4]} camera", llm.image_url(p)) for p in imgs],
                          model=env("ALONG_LOOK_MODEL", "gpt-6.1-sol"), episode=EP["id"])
    else:                                                  # no key: Codex login; empty cwd so AGENTS.md isn't sent along
        r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-m", env("ALONG_LOOK_MODEL", "gpt-6.1-terra"),
                            "-c", "model_reasoning_effort=low", "-o", f"{d}/answer.txt", "-i", *imgs],
                           input=brief, capture_output=True, text=True, timeout=120, cwd=d)
        ans = open(f"{d}/answer.txt").read().strip() if os.path.exists(f"{d}/answer.txt") else r.stdout.strip()[-2000:]
    event("seen", f"({time.time() - t0:.1f} s) {ans}")
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


HOLD_AT = {"left": (0.30, -0.03, 0.15), "right": (0.30, 0.03, 0.15)}    # reaching out in front of the arm, hand height
AIM = f"{HERE}/.along_aim.json"                               # latest "what is held out" vision answer (prefetched)


def aim_seen(what="object"):
    """One fast scene-only vision call: where is the object a person holds out -> {"u", "v", "height_cm", "pose"} or {"u": None}.
    Cached in AIM (the daemon prefetches it the moment a request arrives, while Al is still thinking)."""
    import re
    q = (f'A person holds out {"a " + what if what != "object" else "an object"} for one of the robot arms to take, or is about '
         'to. Geometry for scale: the scene camera sits 33 cm above the table, 70 cm from each arm base (both bases are on '
         'the table, 21 cm apart); an arm is ~40 cm long when stretched out. Use the wrist views too where they show it. '
         'Reply ONLY JSON: {"u": px, "v": px, "dist_cm": distance of the object centre from the scene camera, "height_cm": '
         'height of the object centre above the table, "size_cm": its longest dimension, "pose": "upright" or "flat", '
         '"axis_deg": angle of its long axis in the scene image (0 = horizontal, 90 = vertical)} (u, v = the object centre in '
         'the scene image; flat = held horizontally). If nothing is held out: {"u": null}.')
    def put(d):
        json.dump(d, open(AIM + ".tmp", "w")); os.replace(AIM + ".tmp", AIM)     # atomic: hold may read it meanwhile
    put({"t": time.time(), "pending": True})
    try:
        j = json.loads(re.search(r"\{.*\}", look(q, cams=("scene", "left", "right")), re.S).group(0))
    except Exception as e:
        j = {"u": None, "error": str(e)}
    put({"t": time.time(), **j})
    return j


def aim(side, what, roll):
    """-> (side, x, y, z, roll): the arm nearer to the held-out object (unless a side was given) reaches out toward it,
    jaws ~3 cm before it, in the extended band (28-36 cm out, 10-20 cm up: closer = a crumpled, folded pose).
    Falls back to HOLD_AT."""
    import math
    try:
        j = json.load(open(AIM)) if os.path.exists(AIM) else {}
        while j.get("pending") and time.time() - j["t"] < 10:      # the daemon's prefetch is still looking
            time.sleep(0.2); j = json.load(open(AIM))
        if j.get("pending") or time.time() - j.get("t", 0) > 15:
            event("aim", "no fresh prefetched look: looking now"); j = aim_seen(what)
        else:
            event("aim", f"using the prefetched look ({time.time() - j['t']:.0f} s old)")
        if j.get("u") is None:
            raise ValueError("nothing held out")
        import numpy as np
        c = json.load(open(f"{HERE}/scene.json"))["scene_camera"]           # the pixel fixes a ray; height and distance
        f, up = np.array(c["forward"]), np.array(c["up"]); rr = np.cross(f, up)  # each pick a point on it: average the two
        ray = f + rr * (float(j["u"]) - W / 2) / c["focal_px"] - up * (float(j["v"]) - H / 2) / c["focal_px"]
        ray /= np.linalg.norm(ray)
        zs = {"height": float(j["height_cm"]) / 100 - 0.02} if j.get("height_cm") is not None else {}
        if j.get("dist_cm"):
            zs["distance"] = c["position"][2] + ray[2] * float(j["dist_cm"]) / 100
        z = min(max(sum(zs.values()) / len(zs) if zs else 0.13, 0.01), 0.33)
        h = z + 0.02
        event("aim", "height above table: " + ", ".join(f"{k} says {v * 100 + 2:.0f} cm" for k, v in zs.items())
                     + f" -> {h * 100:.0f} cm" + (f"; size {j['size_cm']} cm, axis {j.get('axis_deg')} deg" if j.get("size_cm") else ""))
        from so101 import Robot
        loc = Robot().locate(float(j["u"]), float(j["v"]), z)
        if side not in ("left", "right"):          # the arm whose base is closer
            side = min(("left", "right"), key=lambda sd: math.hypot(*loc[sd][:2]))
        ox, oy, oz = loc[side]
        r = math.hypot(ox, oy); ux, uy = ox / r, oy / r
        r = min(max(r - 0.03, 0.28), 0.36)
        roll = 90 if j.get("pose") == "flat" else roll
        out = side, round(ux * r, 3), round(uy * r, 3), round(min(max(oz, 0.10), 0.20), 3), roll
        event("aim", f"object at px {j['u']},{j['v']} ({j.get('pose', '?')}, {h * 100:.0f} cm up) = {side} arm {ox:.2f}/{oy:+.2f}/{oz:.2f} m"
                     f" -> reach to {out[1]:.2f}/{out[2]:+.2f}/{out[3]:.2f}, roll {roll}")
        return out
    except Exception as e:
        log("aim: default spot,", e); event("aim", f"default spot instead ({e})")
        side = side if side in ("left", "right") else ("right" if "left" in rd_held() else "left")
        return side, *HOLD_AT[side], roll


def reachable(side, x, y, z, drop=0.06):
    """(x, y, z), or the nearest point the arm really reaches (IK within 1.5 cm, inside robotd's box): up to `drop` lower,
    then 1 cm at a time closer to the arm base."""
    from so101 import Robot
    arm = getattr(Robot(), side)
    z = min(max(z, 0.0), 0.25)
    for k in range(30):                              # shrink the horizontal distance 1 cm at a time, then go lower
        r = (x * x + y * y) ** 0.5; f = max(0.0, r - 0.01 * k) / r
        px, py = min(x * f, 0.38), max(min(y * f, 0.2), -0.2)
        for pz in [z - d / 100 for d in range(0, int(drop * 100) + 1)]:
            if px >= 0.08 and pz >= 0.0 and arm.ik(px, py, pz)["error_m"] < 0.015:
                return px, py, pz
    return (float("inf"),) * 3                           # nowhere near: every distance check fails


def skill(a):
    """along hold|release|turn|press|park ...: whole requests in one call, no questions asked."""
    cmd = a[0]
    if cmd == "hold" and (len(a) < 2 or a[1] not in ("left", "right")):
        a = [a[0], "auto", *a[1:]]                  # along hold [what]: the arm nearer to the object
    side = a[1] if len(a) > 1 else ""
    event("skill", "along " + " ".join(a))
    if side and side not in ("left", "right", "auto") and cmd != "fresh":
        sys.exit("side = left | right")
    if cmd == "hold":                               # along hold <side> [what] [flat] [x y z]
        rest = a[2:]; flat = "flat" in rest; rest = [r for r in rest if r != "flat"]
        nums = [float(r) for r in rest if r.replace(".", "", 1).lstrip("-").isdigit()]
        what = " ".join(r for r in rest if not r.replace(".", "", 1).lstrip("-").isdigit()) or "object"
        roll = 90 if flat else 0
        if len(nums) >= 3:
            side = "right" if side == "auto" else side; x, y, z = nums[:3]
        else:                                       # aim: one coarse move toward what the person holds out, turned to match
            side, x, y, z, roll = aim(side, what, roll)
        print(f"offer {side} at {x:.3f} {y:.3f} {z:.3f} roll {roll}")
        event("skill", f"hold: {side} arm reaches out to {x:.2f}/{y:+.2f}/{z:.2f}, roll {roll}, jaws open; waits for the object")
        for attempt in (1, 2):
            ask = threading.Timer(4.0, say, args=("Ready, put it between my jaws.",)) if attempt == 1 else None
            ask and ask.start()                     # only if it is not already in there (offer closes once it sits still)
            o = subprocess.run(["robot", "offer", side, f"{x:.3f}", f"{y:.3f}", f"{z:.3f}", str(roll)], capture_output=True, text=True)
            ask and ask.cancel()
            if "unreachable" in (o.stderr + o.stdout) and (x, y, z) != HOLD_AT[side]:
                x, y, z = HOLD_AT[side]                 # aimed out of reach: the default spot instead
                event("skill", "hold: aimed point out of reach, default spot instead")
                o = subprocess.run(["robot", "offer", side, *map(str, HOLD_AT[side]), str(roll)], capture_output=True, text=True)
            try:
                r = json.loads(o.stdout)
            except ValueError:                      # robotd down etc.: not a miss, say what broke
                event("error", f"hold {side}: robot error {(o.stderr or o.stdout).strip()[-200:]}")
                sys.exit(f"hold {side} FAILED (robot error, not a miss): {(o.stderr or o.stdout).strip()[-300:]}")
            event("skill", f"hold {side}: {'holding' if r.get('holding') else 'missed'} {json.dumps(r)[:200]}")
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
        rx, ry, rz = reachable(side, x, y, z + 0.04, drop=0.03)       # approach >= 1 cm above the point
        if rx == float("inf"):
            sys.exit(f"press {side} FAILED: {x:.2f}/{y:+.2f}/{z:.2f} is out of the {side} arm's reach")
        far = abs(rx - x) + abs(ry - y) > 0.03                         # beyond the arm (often: held out too far)
        if far or rz < z + 0.04:
            event("skill", f"press: {x:.2f}/{y:+.2f}/{z:.2f} past the arm's reach, pressing at {rx:.2f}/{ry:+.2f} from {rz:.2f}")
        from so101 import Robot
        tip = getattr(Robot(), side).tip()
        near = sum((a - b) ** 2 for a, b in zip(tip, (rx, ry, rz))) ** 0.5 < 0.02   # already there: a goto < 2 cm is refused
        o = rstep(f"set {side}_gripper 0" + ("" if near else f"; goto {side} {rx:.3f} {ry:.3f} {rz:.3f}")); print(o)
        if robot_errs(o):
            sys.exit(f"press {side} FAILED: {'; '.join(robot_errs(o))}")
        if far:                                     # a moment at our limit: the person can bring it under the gripper
            time.sleep(2.0)
        o = rstep(f"goto {side} {rx:.3f} {ry:.3f} {max(z - depth, 0.0):.3f}")   # aims below the surface: the servos push
        bad = [e for e in robot_errs(o) if err_kind(e) != "blocked"]           # stopping short = contact: the press itself
        print(re.sub(r"^ERR .*tip ended.*$", "", o, flags=re.M))
        tz = getattr(Robot(), side).tip()[2]
        print(f"press: contact, the tip stopped at z {tz:.3f} (aimed {max(z - depth, 0.0):.3f})"
              if len(robot_errs(o)) > len(bad) else f"press: went the full depth to z {tz:.3f} (a pump head that gives way looks the same as a miss)")
        time.sleep(0.8)
        print(rstep(f"goto {side} {rx:.3f} {ry:.3f} {rz:.3f}"))             # always lift clear again
        if bad:
            sys.exit(f"press {side} FAILED: {'; '.join(bad)}")
    elif cmd == "open":                             # along open: both grippers open, nothing held (dashboard button)
        print(rstep("set left_gripper 45; set right_gripper 45")); json.dump({}, open(HELD, "w")); event("held", "{}")
    elif cmd == "reset":                            # along reset: stop everything, open, both arms back to rest (dashboard)
        interrupt_codex()
        print(rstep("set left_gripper 45; set right_gripper 45")); json.dump({}, open(HELD, "w")); event("held", "{}")
        print(rstep("recall ready left; recall ready right")); print(rstep("recall rest left; recall rest right"))
    elif cmd == "fresh":                            # along fresh: Codex back to just its brief (dashboard "Reset Al")
        codex_fresh(); print("Codex reset: the next task starts from only the brief")
    elif cmd == "park":                             # along park: arms that hold nothing go to rest
        h = rd_held()
        print(rstep("; ".join(f"recall rest {sd}" for sd in ("left", "right") if sd not in h) or ""))


def main():
    a = sys.argv[1:] or ["help"]
    if a[0] == "run":
        learn_pending()
        run() if "--yolo" in a else run_manual()
    elif a[0] == "attach":                                # watch / type into the Codex session
        th = codex_agent.state()["thread"]
        if not th:
            sys.exit("no Codex conversation yet (or it was reset)")
        os.chdir(HERE); os.execvp("codex", ["codex", "resume", th])
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
    elif a[0] in ("hold", "release", "turn", "tilt", "press", "park", "open", "reset", "fresh"):
        skill(a)
    elif a[0] == "look":
        print(look(" ".join(a[1:])))
    elif a[0] == "skill":                                 # along skill <name> ['<args>']: a learned skill, fast (Codex uses it)
        import skills
        if len(a) < 2 or not skills.load(a[1]):
            sys.exit("skills: " + (", ".join(m["name"] for m in skills.index()) or "none yet"))
        STATE.update(text=brief_state(rstep()), t=time.time())
        st = {k: None for k in STEP_SCHEMA["required"]}
        st.update(action="skill", skill=a[1], args=" ".join(a[2:]) or None, say="")
        ok = run_plan([st], f"skill {a[1]} {' '.join(a[2:])}", None, PLAN["gen"], cli=True)
        print(f"skill {a[1]}: {'done' if ok else 'not done (see above): take over'}"); sys.exit(0 if ok else 1)
    elif a[0] == "skill-undo":                            # along skill-undo <name>: back to the version before the last change
        import skills
        msg = skills.undo(a[1]); event("refine", msg); print(msg)
    elif a[0] == "say":
        say(" ".join(a[1:]))
    elif a[0] == "wake":                                  # debug: force the daemon awake (see .along_force)
        open(f"{HERE}/.along_force", "w").write("wake")
    elif a[0] == "tell":
        t = " ".join(a[1:])
        if t.startswith("[voice]"):
            event("heard", t[7:].strip() + "  (typed)")
            if env("ALONG_ROUTER", "1") != "0":           # typed on the page: same path as speech (router first)
                txt = rstep(); STATE.update(text=brief_state(txt), t=time.time())
                try:
                    act(route(t[7:].strip()), t[7:].strip())
                    for th in threading.enumerate():
                        if th is not threading.main_thread():
                            th.join()
                    return
                except Exception as e:
                    event("error", f"router: {e}; Codex takes it")
        tell(t, wait=True)
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
    llm.SPEND = True                                      # the along process itself: real calls are intended
    main()
