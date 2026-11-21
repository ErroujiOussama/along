#!/usr/bin/env python3
"""along — voice helper around the arms. Idle: only a cheap detector watches the scene camera.
A new object (not in the idle baseline) for ~1.5 s wakes it: mic on (ElevenLabs speech-to-text), what you say goes to
one long-running Codex session (tmux "along", persona prompts/along.md) which acts with `robot` and talks with `along say`.

  along run              the daemon (vision + mic + Codex bridge)
  along idle             store what the scene shows now as the idle baseline (empty table + fixtures)
  along see              latest detections (label, conf, scene px in the 800x450 tile for `robot locate`)
  along say "text"       speak (ElevenLabs; espeak-ng without a key); the mic is muted meanwhile
  along tell "text"      send a line to the Codex session (what the daemon does with speech)
  along wake             force the running daemon awake (debugging)
Debug page: `robot stream` -> http://localhost:8101/along (boxes, state, conversation, Codex pane, type-as-voice).
Keys: ELEVENLABS_API_KEY in the environment or in .env (repo). Optional ALONG_VOICE (voice id), ALONG_MIC (pw source; default the C920 mic).
"""
import json, os, subprocess, sys, threading, time, wave, io
HERE = os.path.dirname(os.path.abspath(__file__))
SEEN, IDLE, MUTE = f"{HERE}/.along_seen.json", f"{HERE}/along_idle.json", f"{HERE}/.along_mute"
EVENTS = f"{HERE}/.along_events.jsonl"                    # conversation + state history (debug page /along)
TMUX, W, H = "along", 800, 450
CODEX = ["codex", "--dangerously-bypass-approvals-and-sandbox", "--disable", "hooks", "-C", HERE,
         "-m", "gpt-6.2-sol", "-c", "model_auto_compact_token_limit=200000"]


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
    event("say", text)
    key = env("ELEVENLABS_API_KEY")
    open(MUTE, "w").write(str(time.time() + 30))
    try:
        if key:
            import requests
            r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{env('ALONG_VOICE', 'JBFqnCBsd6RMkjVDRZzb')}/stream",
                              params={"output_format": "mp3_44100_128"}, headers={"xi-api-key": key},
                              json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=20, stream=True)
            r.raise_for_status()                           # play while it streams in
            p = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for chunk in r.iter_content(4096):
                p.stdin.write(chunk)
            p.stdin.close(); p.wait()
        else:
            subprocess.run(["espeak-ng", "-s", "165", text])
    finally:
        open(MUTE, "w").write(str(time.time() + 0.4))   # room echo


def chime(freq=660, dur=0.12):
    """Short beep: 'I heard you' (low) / 'listening now' (high)."""
    open(MUTE, "w").write(str(time.time() + dur + 0.3))
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={dur}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def transcribe(pcm, rate=16000):
    import requests
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    r = requests.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": env("ELEVENLABS_API_KEY")},
                   data={"model_id": "scribe_v1", "tag_audio_events": "false"},
                   files={"file": ("u.wav", buf.getvalue(), "audio/wav")}, timeout=30)
    r.raise_for_status()
    return r.json().get("text", "").strip()


def muted():
    try:
        return time.time() < float(open(MUTE).read())
    except (FileNotFoundError, ValueError):
        return False


class Mic:
    """Energy VAD on 16 kHz mono from PipeWire. Calls on_text(text) per utterance while self.on is set."""
    def __init__(self, on_text):
        self.on, self.on_text = threading.Event(), on_text
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        import numpy as np
        cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
        mic = env("ALONG_MIC") or next((l.split()[1] for l in subprocess.run(["pactl", "list", "short", "sources"],
              capture_output=True, text=True).stdout.splitlines() if "C920" in l and "input" in l), None)
        if mic:
            cmd += ["--target", mic]
        log("mic:", mic or "default source")
        p = subprocess.Popen(cmd + ["-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        floor, speech, quiet, pre = 300.0, [], 0, []
        while True:
            chunk = p.stdout.read(960)                     # 30 ms
            if not chunk:
                log("mic: pw-record ended"); return
            if not self.on.is_set() or muted():
                speech, quiet, pre = [], 0, []; continue
            rms = float(np.sqrt(np.mean(np.frombuffer(chunk, np.int16).astype(np.float32) ** 2)))
            loud = rms > max(3 * floor, 400)
            if not speech:
                floor = 0.95 * floor + 0.05 * min(rms, 3 * floor)
                pre = (pre + [chunk])[-10:]
                if loud:
                    speech, quiet = pre + [chunk], 0
                continue
            speech.append(chunk); quiet = 0 if loud else quiet + 1
            if quiet > 27 or len(speech) > 500:            # 0.8 s silence / 15 s max
                pcm, speech = b"".join(speech), []
                if len(pcm) > 16000 * 2 * 0.5:
                    chime(440)
                    threading.Thread(target=self.stt, args=(pcm,), daemon=True).start()

    def stt(self, pcm):
        try:
            t = transcribe(pcm)
        except Exception as e:
            log("stt error", e); event("error", f"stt: {e}"); return
        log("heard:", repr(t))
        if t:
            event("heard", t)
        if len(t.strip(" .,!?")) > 1 and not (t.startswith("(") and t.endswith(")")):   # "(music)" etc.
            self.on_text(t)


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
         *CODEX, "Read prompts/along.md and AGENTS.md, then wait for the first [along] or [voice] message. Don't speak yet.")
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

    def detect(self):
        r = self.m.predict(self.bot.thumb("scene", W, H), verbose=False, conf=self.conf)[0]
        return [{"label": r.names[int(c)], "conf": round(float(s), 2), "box": [int(v) for v in x],
                 "px": [int((x[0] + x[2]) / 2), int((x[1] + x[3]) / 2)]}
                for c, s, x in zip(r.boxes.cls, r.boxes.conf, r.boxes.xyxy)]


def novel(dets, base):
    return [d for d in dets if d["conf"] >= 0.35 and not any(b["label"] == d["label"] and iou(b["box"], d["box"]) > 0.3 for b in base)]


def held(bot):
    """Maybe holding something: HOLDING, or jaws commanded shut with the arm out (thin things close fully)."""
    try:
        tg = bot.state()["target"]
        return any(a.holding() or (tg[f"{s}_gripper"] <= 10 and a.tip()[2] > 0.12) for s, a in (("left", bot.left), ("right", bot.right)))
    except Exception:
        return True


def run():
    eyes = Eyes()
    if not os.path.exists(IDLE):
        sys.exit("no idle baseline: clear the table to its idle state and run `along idle` first")
    base = json.load(open(IDLE))
    active, hits, last_new, last_voice = False, [], 0.0, 0.0

    def heard(text):
        nonlocal last_voice
        last_voice = time.time(); tell(f"[voice] {text}")
    mic = Mic(heard)
    if not env("ELEVENLABS_API_KEY"):
        log("no ELEVENLABS_API_KEY: speech-to-text off, say uses espeak-ng")
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
        json.dump({"t": t, "active": active, "listening": mic.on.is_set(), "speaking": muted(), "all": dets, "new": new,
                   "hits": hits, "last_new": last_new, "last_voice": last_voice}, open(SEEN, "w"))
        hits = (hits + [bool(new)])[-4:]
        if new:
            last_new = t
        force = os.path.exists(f"{HERE}/.along_force")
        if force:
            os.remove(f"{HERE}/.along_force"); new = new or [{"label": "something", "px": [400, 225]}]
        if not active and (sum(hits) >= 3 or force):
            active, last_voice = True, t
            names = ", ".join(f"{d['label']} at scene px ({d['px'][0]},{d['px'][1]})" for d in new)
            log("WAKE:", names); event("wake", names)
            say(f"Hi, I'm Along. Need a hand with the {new[0]['label']}?")
            chime(880); mic.on.set()
            tell(f"[along] Woke up: new in the scene: {names}. I already greeted them and asked if they need a hand "
                 "with it; the mic is on. Wait for their [voice] request.")
        elif active and t - last_new > 60 and t - last_voice > 60 and not held(eyes.bot):
            active = False
            mic.on.clear()
            log("back to idle"); event("idle")
            tell("[along] The scene is back to idle (no new objects, nobody talking for a minute, nothing held). "
                 "Park the arms (robot step \"recall rest\"), silently.")
        time.sleep(max(0.0, 0.5 - (time.time() - t)))


def main():
    a = sys.argv[1:] or ["help"]
    if a[0] == "run":
        run()
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
