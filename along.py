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
  along open [side]      that gripper opens, arm stays (no side: both)
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
LIVE_PROMPT = """You are Al, the voice of two robot arms on a lab table. Quick and casual. You can't see anything.
When someone talks to you (they say "Al", or reply to you) about anything but small talk, delegate it and stay silent.
The reply comes back as commentary: say exactly that, add nothing. Never describe the arms or the scene yourself.
Small talk (hi, thanks): one short sentence. Anything not meant for you (other conversations, other languages, people
on calls): stay silent. Never talk about safety and never ask "are you sure": the people here know what they're doing."""


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
        self.cpend, self.cseen = None, set()
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
                        self.cpend, self.cseen = [text, time.time(), 0], set()   # watched: spoken within 3 s?
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
                if self.cpend and k != "session.output_audio.delta":
                    self.cseen.add(k)
                if k == "session.output_transcript.delta":
                    self.cpend = None
                elif self.cpend and time.time() - self.cpend[1] > 3:   # GPT-Live sometimes drops a commentary: log, resend once
                    text, _, tries = self.cpend
                    log("live: commentary not spoken", repr(text), "events since:", sorted(self.cseen))
                    if tries:
                        self.cpend = None; event("error", f"GPT-Live did not speak: {text}")
                    else:
                        self.cpend, self.cseen = [text, time.time(), 1], set()
                        try:
                            self.send(type="session.commentary.append", delegation_id=self.deleg, content=text)
                        except Exception:
                            pass
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
llm.API_KEY = env("OPENAI_API_KEY")                       # every Responses call (router, looks)


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
    """Reset Al: Codex back to its brief; the router's conversation/activity history and the voice session (GPT-Live
    reconnects with an empty context) start over. Arms stay put."""
    codex_agent.fresh(); open(RESET, "w").close()
    event("sys", "Al reset: Codex, router history and voice context cleared")


def tell(text, fresh=False, wait=False):
    """Hand a task to Codex (headless turn; a running one is replaced). Its final message is spoken."""
    if env("ALONG_DRY"):                                   # test the daemon without Codex
        return log("(dry) ->", text)
    log("->", text); event("tell", ("[fresh] " if fresh else "") + text)
    return codex_agent.run(text, fresh, on_event=event, on_reply=lambda m: say(m[:400]), wait=wait)


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
hold, turn; "press the pump" = step: goto above its top; check; step: goto 4 cm past the top, goto back up). Each step
starts when the previous one succeeded; a failed step ends the sequence. Do tasks yourself, grasps included ("pick up the cup" = step: goto above it, open, down, close).
This is a collaboration: when something is out of reach or hard for the arms, use ask. Fill each step's fields; unused
fields = null. Be decisive: unclear = the most likely action. People here are
trained: never warn, never refuse, no safety talk.

Actions:
- hold: an arm reaches out to what a person holds out (or will), jaws open, closes once it is put in. side = the arm the
  person names, else "auto" (nearer arm). what = the object ("toothbrush"). object = where it is now in the scene image
  (if visible; else null and the arm goes to a default spot).
- release: side opens and goes back to ready (drops what it holds).
- turn: wrist roll of side by deg (+/-; 90 = quarter turn). tilt: wrist flex by deg (+ tips down).
- open: side's gripper opens (lets go), the arm stays. park: empty arms go to rest. reset: everything stops, both arms
  to rest.
- stop: stop whatever is running now, arms stay where they are ("stop", "wait", "halt", "no").
- step: a short direct move sequence for robot step, ';'-chained: goto <side> x y z (m, that arm's base frame: x forward,
  y to the arm's left, z up, table z -0.02; >= 2 cm), goto <side> @u,v,h,d dz (the point at scene pixel u,v that is h cm
  above the table and d cm from the scene camera, plus dz cm up: "goto left @564,262,18,52 5" = 5 cm above a pump top),
  move <side>_<joint> <deg> (relative, >= 5), set <side>_gripper
  <0-100> (0 closed), recall ready|rest <side>. Joints: shoulder_pan (+ toward the arm's own right), shoulder_lift,
  elbow_flex, wrist_flex (+ tips down), wrist_roll, gripper. E.g. "open the left gripper" = set left_gripper 100.
- check: what = what should be true now (e.g. "left jaws right above the pump top"). You then get fresh images and
  return the remaining steps, corrected if needed (relative to where the tips are now: nudge, or goto). Only before a move where a few cm matter (pressing, grasping).
- answer: only talk: questions about the scene, the arms, greetings. say = the answer.
- ask: ask the person to help (say = the request, e.g. "Can you bring it a bit closer?"); the sequence ends there.
- codex: only when the person asks Al to take its time / go slow / think it through: codex_prompt = a
  self-contained instruction for the slower agent with all you know (object, where it is in px, which arm).
  codex_fresh = true when the task has nothing to do with what Codex did recently (it then starts from a clean session,
  faster and not confused by old context); false to continue its context ("now do the same with the other arm").
- none: not addressed to Al (chatter, other conversations, other languages not meant for Al): actions = [none], say = "".

say = what Al says when that step is done: ONE short sentence ("Done.", "Here you go.", an answer of at most ~12 words), or
"" for nothing (in a sequence usually only the last step speaks; a last step with "" says "Done."). For hold leave it ""
(the hold reports itself). object: u, v = pixel in the scene image (800x450) of the point to grab (a long thing: the
middle of its handle, not the bristles/head), dist_cm = its distance from the scene camera, height_cm = its height above
the table, pose = "upright" or "flat" (held horizontally), axis_deg = its long axis in the scene image (0 horizontal,
90 vertical). The scene image marks each gripper tip (L/R cross with its xyz): known points to measure against.
Scale: the scene camera is 33 cm above the table and 70 cm from each arm base (bases 21 cm apart); the scene camera
faces the arms: the orange/right arm is on image-left. Wrist images look out between each arm's jaws."""

STEP_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["action", "side", "what", "deg", "step", "object", "say", "codex_prompt", "codex_fresh"],
    "properties": {
        "action": {"type": "string", "enum": ["hold", "release", "turn", "tilt", "check", "open", "park", "reset", "stop",
                                              "step", "answer", "ask", "codex", "none"]},
        "side": {"type": ["string", "null"], "enum": ["left", "right", "auto", None]},
        "what": {"type": ["string", "null"]}, "deg": {"type": ["number", "null"]},
        "step": {"type": ["string", "null"]},
        "object": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
            "required": ["u", "v", "dist_cm", "height_cm", "pose", "axis_deg"],
            "properties": {"u": {"type": "number"}, "v": {"type": "number"}, "dist_cm": {"type": "number"},
                           "height_cm": {"type": "number"}, "pose": {"type": "string", "enum": ["upright", "flat"]},
                           "axis_deg": {"type": ["number", "null"]}}}]},
        "say": {"type": "string"}, "codex_prompt": {"type": ["string", "null"]}, "codex_fresh": {"type": ["boolean", "null"]}}}

ROUTE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["actions"],
                "properties": {"actions": {"type": "array", "items": STEP_SCHEMA}}}

MAX_FAILS = 3                                               # failed steps per request; then the model only talks
RUN = {"p": None, "what": ""}                               # the skill process the router started (stop kills it)
PLAN = {"gen": 0}                                           # bumped by every new plan: an older sequence stops at its next step


def frames(cams=("scene", "left", "right")):
    """Live camera thumbs as (label, data URL) (scene = the 800x450 tile, so pixels go straight into locate). The scene
    image marks each gripper tip (FK + calibration) with its xyz: a known reference to judge offsets against."""
    from so101 import Robot
    bot, out = Robot(), []
    for c in cams:
        try:
            img = bot.thumb(c, *((W, H) if c == "scene" else (640, 360)))
            if c == "scene":
                img = img.copy()
                try:
                    mark_tips(bot, img)
                except Exception as e:
                    log("frames: no tip marks", e)
            out.append((f"{c} camera", llm.image_url(img)))
        except Exception as e:
            log("frames: no", c, e)
    return out


def mark_tips(bot, img):
    import cv2, numpy as np, calib, collide
    s, c = collide.setup(), calib.cam_now()
    for side, col in (("left", (255, 255, 255)), ("right", (0, 140, 255))):
        t = getattr(bot, side).tip()
        u, v = calib.project(c, [(collide.base_T(side, s) @ np.r_[t, 1.0])[:3]])[0]
        if 0 <= u < W and 0 <= v < H:
            p = (int(u), int(v)); txt = f"{side[0].upper()} tip {t[0]:.2f} {t[1]:+.2f} {t[2]:.2f}"
            cv2.drawMarker(img, p, (0, 0, 0), cv2.MARKER_CROSS, 18, 4); cv2.drawMarker(img, p, col, cv2.MARKER_CROSS, 18, 2)
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
            x0 = min(p[0] + 8, W - tw - 4); y0 = p[1] + (22 if side == "left" else -10)   # labels apart: never overlap
            cv2.rectangle(img, (x0 - 2, y0 - th - 3), (x0 + tw + 2, y0 + 3), (0, 0, 0), -1)
            cv2.putText(img, txt, (x0, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1)


def pixel_xyz(side, u, v, h_cm, d_cm=None):
    """Scene pixel + height above the table (+ distance from the scene camera, if given: the two are averaged) -> xyz in
    side's base frame (m)."""
    import numpy as np, calib
    from so101 import Robot
    z = h_cm / 100 - 0.02
    if d_cm:
        c = calib.cam_now(); f, up = np.array(c["forward"]), np.array(c["up"]); r = np.cross(f, up)
        d = f + r * (u - W / 2) / c["focal_px"] - up * (v - H / 2) / c["focal_px"]
        z = (z + c["position"][2] + d[2] / np.linalg.norm(d) * d_cm / 100) / 2
    return Robot().locate(u, v, z)[side]


def recent():
    """Recent conversation and recent activity (router decisions, Codex tasks + commands + turn ends, skill results)."""
    try:
        ev = [e for e in (json.loads(l) for l in open(EVENTS).readlines()[-150:] if l.strip()) if e["t"] > reset_t()]
    except FileNotFoundError:
        return "", ""
    lab = {"heard": "person", "say": "Al", "live": "Al"}
    conv = "\n".join(f"{lab[e['kind']]}: {e['text'][:160]}" for e in ev if e["kind"] in lab)[-900:]
    acts = [f"{time.time() - e['t']:.0f} s ago {e['kind']}: {e['text'][:200]}" for e in ev
            if e["kind"] in ("route", "tell", "skill") or e["kind"] == "codex" and not e["text"].startswith("→")]
    return conv, "\n".join(acts[-10:])


def route(text, failed=None, final=False, check=None):
    """One gpt-6.1-sol ultrafast call: transcript + state + 3 images -> {actions: [{action, params, object, say, codex_prompt}]}.
    failed = what went wrong with the last plan for this request: the model decides what now (retry, ask, answer)."""
    t0 = time.time()
    run = f"running now: {RUN['what']}" if RUN["p"] and RUN["p"].poll() is None else "nothing running"
    conv, acts = recent(); cx = codex_agent.state()
    ctx = (f"Request (last thing heard): {text}\n\nArm state: {STATE['text'] or 'unknown'} ({time.time() - STATE['t']:.0f} s old); "
           f"held: {json.dumps(rd_held())}; {run}; Codex {'is busy with: ' + cx['task'][:200] if cx['pid'] else 'idle'}"
           f"{'; its last task: ' + cx['last']['task'][:200] + ' -> ' + repr(cx['last']['reply'][:120]) if cx['last'] else ''}."
           f"\n\nRecent conversation:\n{conv}\n\nRecent activity (router = you, tell = task sent to Codex, codex = its "
           f"commands and turns):\n{acts}")
    if failed:
        ctx += (f"\n\nWhat you planned for this request failed: {failed}\n" + (
            "Return what to tell the person now: answer (why), or ask (if they can help)." if final else
            "Return what to do now: a corrected retry, ask (if the person can help), or answer (tell them why)."))
    if check:
        ctx += (f"\n\nYour plan for this request is running. Check now (fresh images and state): {check}\n"
                "Return the remaining steps: as planned if the check holds, corrected if not, or ask / answer.")
    out, _ = llm.call("route", [ctx] + frames(), instructions=ROUTE_PROMPT, schema=ROUTE_SCHEMA, timeout=30)
    event("route", f"({time.time() - t0:.1f} s) " + json.dumps({k: v for k, v in out.items() if v not in (None, "")}))
    return out


def robot_errs(out):
    """`robot step` reports refused commands (unreachable, outside the safe box, creeping) as ERR lines, exit code 0."""
    return re.findall(r"^ERR\s+(.*)$", out or "", re.M)


def act(plan, text, answered=False):
    """Run the router's plan: its steps in order, each after the previous one succeeded (latest request wins: a newer plan
    interrupts this one). answered = GPT-Live already replied itself (it never handed off): no extra talk."""
    steps = [st for st in plan["actions"] if st["action"] != "none" and not (answered and st["action"] == "answer")]
    if answered and any(st["action"] == "answer" for st in plan["actions"]):
        event("sys", "GPT-Live already answered this itself: not saying it twice")
    if not steps:
        return
    if all(st["action"] == "answer" for st in steps):     # only talk: never interrupts what runs
        return [say((st.get("say") or "").strip() or "Hm?") for st in steps]
    PLAN["gen"] += 1; gen = PLAN["gen"]
    if codex_agent.busy() or RUN["p"] and RUN["p"].poll() is None or BUSY.is_set():
        interrupt_codex()                                   # a new request replaces whatever runs

    def go():
        BUSY.set(); done, todo, fails, checks = [], list(steps), 0, 0
        try:
            while todo:
                if PLAN["gen"] != gen:
                    return event("sys", f"plan replaced by a newer request after {len(done)} steps")
                st = todo.pop(0)
                if st["action"] == "check":                       # fresh look: the model keeps or corrects the rest
                    checks += 1
                    if checks > MAX_FAILS:
                        event("sys", f"check skipped (already {MAX_FAILS} this request): {st.get('what')}"); continue
                    STATE.update(text=brief_state(rstep()), t=time.time())
                    rest = "; ".join(json.dumps({k: v for k, v in x.items() if v not in (None, "")}) for x in todo) or "nothing"
                    new = route(text, check=f"{st.get('what')} (done so far: {'; '.join(done) or 'nothing'}; planned after "
                                f"the check: {rest})")["actions"]
                    todo = [x for x in new if x["action"] != "none"]
                    continue
                r = do_step(st, text, done, todo, not todo)
                if r is True:
                    continue
                if not r:
                    return
                fails += 1; final = fails >= MAX_FAILS             # failed: the model sees why and decides what now
                event("sys", f"step failed ({fails}): {r[:300]}")
                STATE.update(text=brief_state(rstep()), t=time.time())
                ran = f" (steps before it that worked: {'; '.join(done)})" if done else ""
                new = route(text, failed=r + ran, final=final)["actions"]
                todo = [x for x in new if x["action"] != "none" and (not final or x["action"] in ("answer", "ask"))]
        except Exception as e:
            log("plan failed", e); event("error", f"plan: {e}")
        finally:
            if PLAN["gen"] == gen:
                BUSY.clear()
    threading.Thread(target=go, daemon=True).start()


def do_step(r, text, done, rest, last):
    """One plan step, blocking. True = go on with the next step, False = stop (stopped / asked the person), a string =
    it failed (why, for the model to decide what now). done: what earlier steps did (for Codex)."""
    a, side, say_done = r["action"], r.get("side") or "auto", (r.get("say") or "").strip()
    speak = say_done or ("Done." if last else "")

    def say(t):                                           # what the person was told goes to Codex too
        globals()["say"](t); done.append(f'Al told the person: "{t}"')
    if a == "answer":
        say(say_done or "Hm?"); return True
    if a == "ask":
        say(say_done or "Can you help me with this?"); return False
    if a == "stop":
        interrupt_codex(); say(say_done or "Stopped."); return True
    if a == "codex":
        if done:                                          # the arms moved: fresh state for Codex
            STATE.update(text=brief_state(rstep()), t=time.time())
        age = time.time() - STATE["t"]
        now = f"now: {STATE['text']}; snap_all.jpg {age:.0f} s old" if age < 30 else "arm state: run robot step"
        before = f"; already done for this request: {'; '.join(done)}" if done else ""
        tell(f"[task] {r.get('codex_prompt') or text}   || heard: {text}{before}; {now}; held {json.dumps(rd_held())}",
             fresh=bool(r.get("codex_fresh")), wait=True)
        return True
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
    elif a == "open":
        args = [a] + ([side] if side in ("left", "right") else [])
    elif a in ("park", "reset"):
        args = [a]
    elif a == "step":
        cmds = [c.strip() for c in (r.get("step") or "").split(";") if c.strip()]
        if not cmds or any(c.split()[0] not in ("goto", "move", "set", "recall", "nudge") for c in cmds):
            return f"step '{r.get('step')}': only goto, move, set, recall, nudge"
        for i, c in enumerate(cmds):                      # goto <side> @u,v,h dz: scene pixel -> xyz (robot locate)
            m = re.fullmatch(r"goto (left|right) @([\d.]+),([\d.]+),(-?[\d.]+)(?:,([\d.]+))?(?:\s+([+-]?[\d.]+))?", c)
            if m:
                x, y, z = pixel_xyz(m[1], float(m[2]), float(m[3]), float(m[4]), m[5] and float(m[5]))
                cmds[i] = f"goto {m[1]} {x:.3f} {y:.3f} {z + float(m[6] or 0) / 100:.3f}"
        args = None
    else:
        return True
    what = "along " + " ".join(args) if args else "robot step " + "; ".join(cmds)
    if args is None:
        RUN.update(p=None, what="step"); out = ""
        for c in cmds:                                    # one robot step each: a ';' chain sums into one target
            o_ = rstep(c); out += o_ + "\n"
            if robot_errs(o_):
                break
        ok, killed = bool(out.strip()), False
    else:
        p = subprocess.Popen([sys.executable, __file__, *args], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        RUN.update(p=p, what=what); out = p.communicate()[0]; ok, killed = p.returncode == 0, p.returncode < 0
    if killed:                                            # stop / a newer request: say nothing, end the plan
        event("skill", f"stopped: {what}"); return False
    errs = robot_errs(out)
    if errs:                                              # refused by robotd: never "Done."
        event("skill", "refused: " + "; ".join(errs)[:240])
        return f"`{what}` was refused by the robot: {'; '.join(errs)[:300]}"
    event("skill", ("done: " if ok else "failed: ") + " ".join(out.split())[-240:])
    if a == "hold":
        if re.search(r"""['"]holding['"]: (True|true)""", out):
            say(say_done or "Got it."); done.append(f"{what}: holding it"); return True
        return f"`{what}` did not end up holding it: {' '.join(out.split())[-300:]}"
    if not ok:
        return f"`{what}` failed: {' '.join(out.split())[-300:]}"
    if speak:
        say(speak)
    done.append(f"{what}: done"); return True


def run_manual():
    """Manual on/off (`along on` / `along off`, or the page buttons) instead of the scene detector."""
    on, last_voice = False, 0.0

    def heard(text, answered=False):
        nonlocal last_voice
        last_voice = time.time()
        age = time.time() - STATE["t"]
        ctx = f"   || now: {STATE['text']}; held {json.dumps(rd_held())}; snap_all.jpg {age:.0f} s old" if age < 6 else ""
        if env("ALONG_ROUTER", "1") != "0":
            try:
                return act(route(text), text, answered)
            except Exception as e:
                log("router failed", e); event("error", f"router: {e}"); return say("Sorry, I glitched. Say that again?")
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
        ans, _ = llm.call("look", [brief] + [(f"{os.path.basename(p)[:-4]} camera", llm.image_url(p)) for p in imgs])
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


def skill(a):
    """along hold|release|turn|tilt|open|park ...: whole requests in one call, no questions asked."""
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
            if "unreachable" in (o.stderr + o.stdout):   # back to the model (retry, ask, or say why)
                sys.exit(f"hold {side} FAILED at {x:.2f}/{y:+.2f}/{z:.2f}: {(o.stderr or o.stdout).strip()[-200:]}")
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
    elif cmd == "open":                             # along open [side]: that gripper (no side: both, dashboard) opens
        sides = [side] if side in ("left", "right") else ["left", "right"]
        print(rstep("; ".join(f"set {sd}_gripper 45" for sd in sides)))
        for sd in sides:
            set_held(sd, "none")
    elif cmd == "reset":                            # along reset: stop everything, open, both arms back to rest (dashboard)
        interrupt_codex()
        print(rstep("set left_gripper 45; set right_gripper 45")); json.dump({}, open(HELD, "w")); event("held", "{}")
        print(rstep("recall ready left; recall ready right")); print(rstep("recall rest left; recall rest right"))
    elif cmd == "fresh":                            # along fresh: Codex back to just its brief (dashboard "Reset Al")
        codex_fresh(); print("Al reset: Codex, router history and voice start over")
    elif cmd == "park":                             # along park: arms that hold nothing go to rest
        h = rd_held()
        print(rstep("; ".join(f"recall rest {sd}" for sd in ("left", "right") if sd not in h) or ""))


def main():
    a = sys.argv[1:] or ["help"]
    llm.SPEND = True                                      # the along process itself: real calls are intended
    if a[0] == "run":
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
    elif a[0] in ("hold", "release", "turn", "tilt", "park", "open", "reset", "fresh"):
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
            if env("ALONG_ROUTER", "1") != "0":           # typed on the page: same path as speech (router first)
                txt = rstep(); STATE.update(text=brief_state(txt), t=time.time())
                try:
                    act(route(t[7:].strip()), t[7:].strip())
                    for th in threading.enumerate():
                        if th is not threading.main_thread():
                            th.join()
                    return
                except Exception as e:
                    event("error", f"router: {e}"); sys.exit(f"router: {e}")
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
    main()
