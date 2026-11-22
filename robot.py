#!/usr/bin/env python3
"""robot — CLI for the SO-101 arms (talks to robotd through so101.py).

  robot                                  state table (target vs measured, both arms)
  robot watch                            live state table (Ctrl-C to quit)
  robot move left elbow -5               relative (deg; gripper in %)
  robot set right lift -40               absolute
  robot open left | close left | grip right 30
  robot goto left 0.2 0 0.08             straight line to that tip xyz (m, arm base frame), keeps the gripper orientation
  robot nudge right 1 0 -0.5             tip dx dy dz cm, ≤ 3 cm (discouraged: estimate well and goto instead)
  robot offer left 0.25 0 0.15 [roll]   hand-over from a person: gripper there, pointing at them horizontally, jaws open
                                         (roll 0 = jaws left/right, 90 = up/down); waits until something sits still between
                                         the jaws, closes, checks HOLDING (reopens on a miss); -> {"holding", "tries"}
  robot tip left                         gripper tip position (m) from the measured pose
  robot ik right 0.30 0.05 0.06          dry run: joint targets for that tip xyz (pointing down), moves nothing
  robot save ready | recall ready [left|right] | poses
  robot torque off left | torque on [all]   limp for hand-guiding (HOLD THE ARM) / hold the new pose
  robot snap [left|right|scene] [path]   camera frames (default all connected: snap_<cam>.jpg)
  robot cams                             which cameras deliver frames (replug-safe: they come back by themselves)
  robot cam scene [k=v ...] [reset]      list / set camera controls, saved + re-applied on replug
                                         (auto_exposure=1 exposure_time_absolute=120 · auto_exposure=3 = auto)
  robot view [cam ...]                   live video window, cameras side by side (default all)
  robot stream [port [cam ...]]          the same as a web page (MJPEG, default port 8101); /inspect = rollout inspector
  robot stop | home | limits | speed 30
  robot step ["move right_elbow_flex -10; goto right 0.25 0 0.08"]   agent loop: raw cmds, wait until settled,
                                         then a few lines (joints, HOLDING = gripper closed on something,
                                         tip xyz m + how far it moved since the last step)
                                         + ONE image snap_all.jpg (left | right wrist 400x225 on top, scene 800x450 below)
  robot raw "move left_elbow_flex -5; wait; snap"   raw robotd protocol, ';' chains, wait = until settled
  robot shutdown [here]                  release torque after going home (or right here)

Joints: pan lift elbow wrist roll gripper. Sides: left/l (white), right/r (orange).
Real arms: gotos move >= 2 cm (+ one 1-2 cm fix per approach), joint moves >= 5 deg (gripper, roll free): no creeping.
Flags: --json machine-readable output, --no-wait return before the motion finishes.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from so101 import Robot, RobotError, JOINTS, tile

SIDE = {"l": "left", "left": "left", "r": "right", "right": "right"}
CAM = {**SIDE, "s": "scene", "scene": "scene"}
SHORT = ["pan", "lift", "elbow", "wrist", "roll"]


def table(st):
    tq = " · ".join(f"{s} torque {'on' if v else 'OFF'}" for s, v in st["torque"].items())
    out = [f"loop {st['hz']} Hz · vel {st['vel']}°/s · P {st['p_gain']} · errors {st['errors']} · {tq}"
           + (f" · goto: {st['plan']} waypoints left" if st["plan"] else ""),
           f"{'':14s}{'LEFT (white)':>21s}   {'RIGHT (orange)':>21s}",
           f"{'joint':14s}{'target':>10s}{'measured':>11s}   {'target':>10s}{'measured':>11s}"]
    for j in JOINTS:
        row = f"{j:14s}"
        for side in ("left", "right"):
            t, m = st["target"][f"{side}_{j}"], st["measured"][f"{side}_{j}"]
            row += f"{t:10.1f}{m:10.1f}{'!' if abs(t - m) > 3 else ' '}   "
        out.append(row.rstrip())
    if any(abs(st["target"][k] - st["measured"][k]) > 3 for k in st["target"]):
        out.append("! = more than 3° from target (moving or blocked)")
    return "\n".join(out)


def side(a, i=1):
    try:
        return SIDE[a[i].lower()]
    except (IndexError, KeyError):
        sys.exit("say which arm: left or right")


class Mosaic:
    """Robot.mosaic for view/stream: survives camera replugs (grey tile) and robotd restarts (reconnects)."""
    def __init__(self, names=None):
        self.bot, self.names = None, names

    def frame(self):
        try:
            self.bot = self.bot or Robot()
            return self.bot.mosaic(self.names)
        except Exception as e:              # socket gone: robotd restarting
            self.bot = None
            return tile(f"robotd: {e}", n=len(self.names or ["left", "right", "scene"]))


def step_text(r):
    out = [f"{'ok' if not isinstance(rep, str) else 'ERR':4s} {c}" + (f"   {rep[7:]}" if isinstance(rep, str) else
           f"   off target: {rep['off_target']}" if c == "wait" and rep.get("off_target") else "") for c, rep in r["ran"]]
    for s, a in r["state"].items():
        js = " ".join(f"{n} {a['measured'][j] + 0:.1f}{'!' if j in a['off'] else ''}" for n, j in zip(SHORT, JOINTS[:5]))
        g = f"grip {a['measured']['gripper'] + 0:.1f}{'!' if 'gripper' in a['off'] else ''} (target {a['target']['gripper']:.1f})"
        d = a.get("tip_delta")
        d = f" (moved {' '.join(f'{v * 100:+.1f}' for v in d)} cm)" if d and any(abs(v) >= 0.002 for v in d) else ""
        out.append(f"{s:6s} {js} {g}{' HOLDING' if a['holding'] else ''}   torque {'on' if a['torque'] else 'OFF'}"
                   f"   tip x{a['tip'][0]:.2f} y{a['tip'][1]:+.2f} z{a['tip'][2]:.2f} m{d}")
    for e in r.get("blocked", []):
        out.append(f"BLOCKED {e['joint']} stuck at {e['measured']} (target {e['target']}): something is in the way, "
                   "that arm is held where it is; back off / take another path, do not push")
    if r.get("near"):
        out.append("near   " + " · ".join(r["near"]))
    cams = " | ".join(n + (" MISSING" if n in r["missing_cams"] else "") for n in ("left", "right", "scene"))
    return "\n".join(out + [f"image  {r['image']}  (top: {' | '.join(cams.split(' | ')[:2])}; below, large: {cams.split(' | ')[2]})"])


def _scene(objects=()):
    """Static collision geometry for the inspector (world = left base frame)."""
    import collide
    s = collide.setup()
    return {"table_z": s["table_z"], "obstacles": s["obstacles"], "objects": list(objects)}


def _caps(qd):
    """{key: deg} both arms -> {"left upper_arm": [a, b, r], ...} capsules in the world frame (collide.py model)."""
    import collide, numpy as np
    s, out = collide.setup(), {}
    for sd in ("left", "right"):
        q = np.array([[qd[f"{sd}_{j}"] for j in collide.J]])
        for n, (a, b, r) in collide.capsules(q, sd, s).items():
            out[f"{sd} {n}"] = [a[0].round(4).tolist(), b[0].round(4).tolist(), r]
        out[f"{sd} tip"] = collide.base_T(sd, s)[:3, :3].dot(collide.tip(q)[0]).round(4).tolist()
        out[f"{sd} tip"] = (np.array(out[f"{sd} tip"]) + collide.base_T(sd, s)[:3, 3]).round(4).tolist()
    return out


def along_api(h, u):
    """Debug page for the along voice helper: /along, /along.json (state, detections, history, Codex state + turn log),
    /along/scene.jpg (scene camera with the detector's boxes), /along/do?cmd=tell|say|wake|idle&text=..."""
    import subprocess
    from urllib.parse import parse_qsl
    here = os.path.dirname(os.path.abspath(__file__))
    rd = lambda f, d=None: json.load(open(f"{here}/{f}")) if os.path.exists(f"{here}/{f}") else d
    def send(body, ctype):
        h.send_response(200); h.send_header("Content-Type", ctype); h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(body))); h.end_headers(); h.wfile.write(body)
    if u.path == "/along":
        return send(open(f"{here}/along.html", "rb").read(), "text/html")
    if u.path == "/along.json":
        ev = []
        if os.path.exists(f"{here}/.along_events.jsonl"):
            ev = [json.loads(l) for l in open(f"{here}/.along_events.jsonl").readlines()[-800:] if l.strip()]
        cx = rd(".along_codex.json")
        log = open(f"{here}/.along_codex.log").readlines()[-80:] if os.path.exists(f"{here}/.along_codex.log") else []
        daemon = subprocess.run(["pgrep", "-f", "along.py run"], capture_output=True).returncode == 0
        arms = {}
        try:
            with Robot() as r:
                st = r.state()
                for side in ("left", "right"):
                    a = getattr(r, side)
                    arms[side] = {"gripper_target": st["target"][f"{side}_gripper"], "holding": a.holding(), "tip": a.tip()}
        except Exception as e:
            arms = {"error": str(e)}
        return send(json.dumps({"now": time.time(), "daemon": daemon, "seen": rd(".along_seen.json"),
                                "idle": rd("along_idle.json", []), "events": ev, "arms": arms,
                                "codex": cx, "codex_log": "".join(log)}).encode(), "application/json")
    if u.path == "/along/scene.jpg":
        import cv2
        try:
            with Robot() as r:
                img = r.thumb("scene", 800, 450).copy()
        except Exception as e:
            img = tile(str(e), 800, 450)
        seen = rd(".along_seen.json", {}) or {}
        if seen.get("roi"):                                                  # wake zone: new things here wake Along
            x0, y0, x1, y1 = seen["roi"]; ch = seen.get("change", 0)
            cv2.rectangle(img, (x0, y0), (x1, y1), (60, 220, 60) if ch > 0.02 else (230, 200, 60), 1)
            cv2.putText(img, f"wake zone  change {ch * 100:.1f}% (>2% wakes)", (x0 + 4, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 200, 60), 1)
        for b in ([] if seen.get("manual") else rd("along_idle.json", [])):
            cv2.rectangle(img, tuple(b["box"][:2]), tuple(b["box"][2:]), (120, 120, 120), 1)
        for u_, v_ in seen.get("arm_px", []):
            cv2.circle(img, (int(u_), int(v_)), 3, (200, 60, 200), -1)     # arm links (FK): boxes there are ignored
        new = [d["box"] for d in seen.get("new", [])]
        for d in seen.get("all", []):
            c = (60, 220, 60) if d["box"] in new else (200, 60, 200) if d.get("arm") else (60, 170, 230)   # BGR: green new, magenta on an arm, orange known
            cv2.rectangle(img, tuple(d["box"][:2]), tuple(d["box"][2:]), c, 2)
            txt = f"{d['label']} {d['conf']:.2f}"
            cv2.putText(img, txt, (d["box"][0], max(14, d["box"][1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
            cv2.putText(img, txt, (d["box"][0], max(14, d["box"][1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 1)
        return send(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes(), "image/jpeg")
    if u.path == "/along/do":
        q = dict(parse_qsl(u.query)); cmd = q.get("cmd", "")
        if cmd not in ("tell", "say", "wake", "idle", "on", "off", "open", "reset", "fresh"):
            return send(b'{"error": "cmd?"}', "application/json")
        args = [f"{here}/.venv/bin/python", f"{here}/along.py", cmd] + ([q["text"]] if q.get("text") else [])
        subprocess.Popen(args, cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return send(json.dumps({"started": cmd}).encode(), "application/json")
    h.send_error(404)


def inspect_api(h, u):
    """Rollout inspector: /inspect page, /rollouts.json list, /rollout.json?id= replay, /live.json, /r/<date>/<id>.jpg."""
    from urllib.parse import parse_qsl
    q, here = dict(parse_qsl(u.query)), os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(here, "rollouts")
    day = q.get("date") or (sorted(os.listdir(root))[-1] if os.path.isdir(root) and os.listdir(root) else "")
    try:
        if u.path == "/inspect":
            return h.send(open(os.path.join(here, "inspect.html"), "rb").read(), "text/html")
        if u.path.startswith("/r/"):
            f = os.path.normpath(os.path.join(root, u.path[3:]))
            if not f.startswith(root) or not os.path.exists(f):
                h.send_error(404); return
            return h.send(open(f, "rb").read(), "image/jpeg")
        if u.path == "/live.json":
            with Robot() as r:
                st = r.state()
            m = st["measured"]
            return h.send(json.dumps({"caps": _caps(m), "near": st.get("near", []), "plan": st.get("plan", 0),
                                      "scene": _scene(st.get("collide", {}).get("objects", []))}).encode(), "application/json")
        recs = []
        f = os.path.join(root, day, "log.jsonl")
        if os.path.exists(f):
            recs = [json.loads(l) for l in open(f) if l.strip()]
        if u.path == "/rollouts.json":
            days = sorted(os.listdir(root)) if os.path.isdir(root) else []
            lst = [{k: v for k, v in x.items() if k != "traj"} for x in recs[-400:]]
            return h.send(json.dumps({"date": day, "days": days, "steps": lst[::-1]}).encode(), "application/json")
        rec = next(x for x in recs if x["id"] == q["id"])
        tr, frames = rec["traj"], []
        idx = range(0, len(tr["t"]), max(1, len(tr["t"]) // 200))
        for i in idx:
            frames.append({"t": round(tr["t"][i] - rec["t0"], 2), "caps": _caps(dict(zip(tr["keys"], tr["q"][i])))})
        rec = {k: v for k, v in rec.items() if k != "traj"}
        return h.send(json.dumps({"rec": rec, "frames": frames, "scene": _scene(rec.get("objects", [])), "date": day}).encode(),
                      "application/json")
    except Exception as e:
        return h.send(json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def stream(names, port):
    """Serve the cameras (through robotd) as MJPEG at http://0.0.0.0:port/."""
    import cv2, threading
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    latest, lock, mosaic = [b""], threading.Lock(), Mosaic(names)

    def grab():                             # one robotd reader, shared by all viewers
        while True:
            try:
                jpg = cv2.imencode(".jpg", mosaic.frame(), [cv2.IMWRITE_JPEG_QUALITY, 75])[1].tobytes()
                with lock:
                    latest[0] = jpg
            except Exception as e:
                print("stream:", e, flush=True)
            time.sleep(1 / 12)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype):
            self.send_response(200); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            from urllib.parse import urlparse, parse_qsl
            u = urlparse(self.path)
            if u.path == "/ctl":            # camera settings, through robotd (camctl): ?cam=scene&k=v | &reset=1
                q = dict(parse_qsl(u.query))
                try:
                    with Robot() as r:
                        if "cam" in q:
                            cam = q.pop("cam")
                            out = r.camctl(cam, reset="reset" in q, **{k: int(v) for k, v in q.items() if k != "reset"})
                        else:
                            out = {}
                            for c in r.cams():
                                try:
                                    out[c] = r.camctl(c)
                                except RobotError as e:
                                    out[c] = {"error": str(e)}
                except Exception as e:
                    out = {"error": str(e)}
                return self.send(json.dumps(out).encode(), "application/json")
            if u.path == "/frame.jpg":      # newest mosaic as one JPEG; the page polls this (works in every browser)
                with lock:
                    jpg = latest[0]
                self.send_response(200); self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(jpg)))
                self.end_headers(); self.wfile.write(jpg); return
            if u.path.startswith("/along"):
                return along_api(self, u)
            if u.path in ("/inspect", "/rollouts.json", "/rollout.json", "/live.json") or u.path.startswith("/r/"):
                return inspect_api(self, u)
            if u.path != "/stream":
                return self.send(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream.html"), "rb").read(), "text/html")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f"); self.end_headers()
            t0, n = time.time(), 0
            print(f"viewer + {self.client_address[0]}", flush=True)
            try:
                while True:
                    n += 1
                    with lock:
                        jpg = latest[0]
                    if jpg:
                        self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
                    time.sleep(1 / 12)
            except Exception as e:
                print(f"viewer - after {time.time() - t0:.0f} s, {n} frames: {type(e).__name__}", flush=True)

    threading.Thread(target=grab, daemon=True).start()
    print(f"streaming on http://127.0.0.1:{port}/  (share: tailcat serve {port})", flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv):
    as_json, wait = "--json" in argv, "--no-wait" not in argv
    a = [x for x in argv if x not in ("--json", "--no-wait")]
    cmd = a[0] if a else "state"
    if cmd in ("-h", "--help", "help"):
        print(__doc__); return
    bot = None if cmd in ("view", "stream") else Robot()     # view/stream (re)connect by themselves
    off = None
    if cmd == "state":
        st = bot.state(); print(json.dumps(st) if as_json else table(st)); return
    if cmd == "watch":
        try:
            while True:
                print("\033[H\033[J" + table(bot.state()), flush=True); time.sleep(0.2)
        except KeyboardInterrupt:
            return
    if cmd == "view":
        import subprocess
        mosaic = Mosaic([CAM[x] for x in a[1:]] or None)
        img = mosaic.frame()
        p = subprocess.Popen(["ffplay", "-loglevel", "error", "-window_title", "so101 cameras",
                              "-f", "rawvideo", "-pixel_format", "bgr24", "-video_size", f"{img.shape[1]}x{img.shape[0]}",
                              "-framerate", "12", "-fflags", "nobuffer", "-i", "-"], stdin=subprocess.PIPE)
        try:
            while p.poll() is None:
                f = mosaic.frame()
                if f.shape != img.shape:         # camera count changed: keep the window size
                    import cv2; f = cv2.resize(f, (img.shape[1], img.shape[0]))
                p.stdin.write(f.tobytes()); p.stdin.flush()
                time.sleep(1 / 12)
        except (BrokenPipeError, KeyboardInterrupt):
            pass
        return
    if cmd == "stream":
        stream([CAM[x] for x in a[2:]] or None, int(a[1]) if len(a) > 1 else 8101); return
    if cmd == "cams":
        for n, c in bot.cams().items():
            print(f"{n:6s} {'ok' if c['ok'] else 'MISSING':8s} age {c['age_s']} s  {c['dev']}")
        return
    if cmd == "cam":
        r = bot.call(" ".join(["camctl", CAM.get(a[1], a[1]) if len(a) > 1 else "?"] + a[2:]))
        for k, c in r["controls"].items():
            print(f"{k:32s} {c['value']:>6} [{c['min']}..{c['max']}] default {c['default']}{'  (auto)' if c['inactive'] else ''}")
        print("saved:", r["saved"] or "-"); return
    if cmd == "step":
        r = bot.step(" ".join(a[1:])); print(json.dumps(r) if as_json else step_text(r)); return
    if cmd == "plan":
        dry = "--dry" in a
        r = bot.plan(" ".join(x for x in a[1:] if x != "--dry"), dry=dry)
        if as_json:
            print(json.dumps(r)); return
        sim = r["sim"]
        bad = [f"  {c}: {rep[7:]}" for c, rep in sim["ran"] if isinstance(rep, str)]
        bad += [f"  BLOCKED {e['joint']} at {e['measured']} (target {e['target']})" for e in sim["blocked"]]
        tips = " · ".join(f"{s} x{a['tip'][0]:.2f} y{a['tip'][1]:+.2f} z{a['tip'][2]:.2f}" for s, a in sim["state"].items())
        print(f"sim: {'OK' if r['ok'] else 'FAILED, real arms not moved'} ({len(sim['ran'])} cmds; end tips {tips})")
        print("\n".join(bad + ([f"near at the end: {' · '.join(sim['near'])}"] if sim["near"] else [])))
        if r["real"]:
            print(step_text(r["real"]))
        elif dry and r["ok"]:
            print("dry run: real arms not moved")
        return
    if cmd == "raw":
        for c in " ".join(a[1:]).split(";"):
            c = c.strip()
            if c == "wait":
                print(json.dumps({"off_target": bot.wait()}))
            elif c:
                try:
                    print(json.dumps(bot.call(c)))
                except RobotError as e:
                    print(json.dumps({"ok": False, "error": str(e)}))
        return
    if cmd in ("move", "set"):
        if len(a) != 4:
            sys.exit(f"usage: robot {cmd} <left|right> <joint> <deg>")
        arm = getattr(bot, side(a))
        off = getattr(arm, cmd)(wait=wait, **{a[2]: float(a[3])})
    elif cmd in ("open", "close"):
        off = getattr(getattr(bot, side(a)), cmd)(wait=wait)
    elif cmd == "grip":
        off = getattr(bot, side(a)).grip(float(a[2]), wait=wait)
    elif cmd == "goto":
        off = getattr(bot, side(a)).goto(*map(float, a[2:5]), wait=wait)
    elif cmd == "ik":
        print(json.dumps(getattr(bot, side(a)).ik(*map(float, a[2:5])))); return
    elif cmd == "nudge":
        print(json.dumps(getattr(bot, side(a)).nudge(*map(float, a[2:5]), wait=wait))); return
    elif cmd == "offer":
        v = list(map(float, a[2:6])); print(json.dumps(bot.offer(side(a), *v))); return
    elif cmd == "locate":
        print(json.dumps(bot.locate(*map(float, a[1:4])))); return
    elif cmd == "tip":
        print([round(v, 3) for v in getattr(bot, side(a)).tip()]); return
    elif cmd == "recall":
        off = bot.recall(a[1], SIDE.get(a[2]) if len(a) > 2 else None, wait=wait)
    elif cmd == "torque":
        if len(a) < 2 or a[1] not in ("on", "off"):
            sys.exit("usage: robot torque on|off [left|right|all]")
        bot.torque(a[1] == "on", SIDE.get(a[2], a[2]) if len(a) > 2 else "all")
    elif cmd == "home":
        off = bot.home(wait=wait)
    elif cmd == "save":
        bot.save(a[1]); print(f"saved {a[1]}"); return
    elif cmd == "poses":
        print(" ".join(bot.poses()) or "(none)"); return
    elif cmd == "limits":
        print(json.dumps(bot.limits(), indent=1)); return
    elif cmd == "snap":
        if len(a) > 1:
            print(bot.snap(CAM.get(a[1], a[1]), a[2] if len(a) > 2 else None))
        else:
            print("\n".join(f"{s}: {p}" for s, p in bot.snap().items()))
        return
    elif cmd in ("collide", "handover", "object"):
        r = bot.call(" ".join(a if cmd == "object" else a[:2])); r.pop("ok", None)
        print(json.dumps(r, indent=1, ensure_ascii=False)); return
    elif cmd == "speed":
        print(f"{bot.speed(float(a[1]))} deg/s"); return
    elif cmd == "stop":
        bot.stop(); print("stopped"); return
    elif cmd == "shutdown":
        bot.shutdown(here=len(a) > 1 and a[1] == "here"); print("releasing torque"); return
    else:
        sys.exit(f"unknown command {cmd!r}; try: robot help")
    st = bot.state()
    if as_json:
        print(json.dumps({"off_target": off, "state": st})); return
    if wait:
        print("settled" if not off else "NOT on target: " + ", ".join(f"{k} {t}→{m}" for k, (t, m) in off.items()))
    print(table(st))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RobotError as e:
        sys.exit(f"error: {e}")
