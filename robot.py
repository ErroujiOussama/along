#!/usr/bin/env python3
"""robot — CLI for the SO-101 arms (talks to robotd through so101.py).

  robot                                  state table (target vs measured, both arms)
  robot watch                            live state table (Ctrl-C to quit)
  robot move left elbow -5               relative (deg; gripper in %)
  robot set right lift -40               absolute
  robot open left | close left | grip right 30
  robot goto left 0.2 0 0.08             Cartesian (m, arm base frame), gripper down
  robot tip left                         gripper tip position (m) from the measured pose
  robot save ready | recall ready [left|right] | poses
  robot torque off left | torque on [all]   limp for hand-guiding (HOLD THE ARM) / hold the new pose
  robot snap [left|right|scene] [path]   camera frames (default all connected: snap_<cam>.jpg)
  robot cams                             which cameras deliver frames (replug-safe: they come back by themselves)
  robot cam scene [k=v ...] [reset]      list / set camera controls, saved + re-applied on replug
                                         (auto_exposure=1 exposure_time_absolute=120 · auto_exposure=3 = auto)
  robot view [cam ...]                   live video window, cameras side by side (default all)
  robot stream [port [cam ...]]          the same as a web page (MJPEG, default port 8101; share with tailcat)
  robot stop | home | limits | speed 30
  robot step ["move right_elbow_flex -5; grip right 3"]   agent loop: raw cmds, wait until settled, then
                                         4-line summary (joints, HOLDING = gripper closed on something, tip xyz m)
                                         + ONE small image snap_all.jpg (left | right | scene, 400x225 each)
  robot raw "move left_elbow_flex -5; wait; snap"   raw robotd protocol, ';' chains, wait = until settled
  robot shutdown [here]                  release torque after going home (or right here)

Joints: pan lift elbow wrist roll gripper. Sides: left/l (white), right/r (orange).
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
        out.append(f"{s:6s} {js} {g}{' HOLDING' if a['holding'] else ''}   torque {'on' if a['torque'] else 'OFF'}"
                   f"   tip {' '.join(f'{v:.2f}' for v in a['tip'])}")
    cams = " | ".join(n + (" MISSING" if n in r["missing_cams"] else "") for n in ("left", "right", "scene"))
    return "\n".join(out + [f"image  {r['image']}  ({cams})"])


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
            if u.path != "/stream":
                return self.send(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream.html"), "rb").read(), "text/html")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f"); self.end_headers()
            try:
                while True:
                    with lock:
                        jpg = latest[0]
                    if jpg:
                        self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
                    time.sleep(1 / 12)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
