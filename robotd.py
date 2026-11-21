"""robotd: the one process that owns both SO-101 arms and the camera.

Run:   systemctl --user start robotd          (unit: robotd.service; logs: journalctl --user -u robotd -f)
Talk:  so101.py / `robot` CLI                 (JSON lines over the unix socket ./robot.sock)

Design
- A 50 Hz control loop is the only code that touches the serial bus.
- Commands never block: they change the *target*. The loop moves the commanded position toward the
  target at a capped speed (VEL deg/s, gripper GRIP_VEL %/s), so a new command preempts the old one.
- The commanded position may lead the measured position by at most LEAD degrees. Unlike LeRobot's
  max_relative_target=2 (which clipped every goal to present+-2 deg and left loaded joints like the
  elbow without enough torque to lift the forearm), this gives the servo real effort but no wind-up.
- Every target is clamped to the calibrated range of each joint.
- One thread per wrist camera keeps the newest frame, so `snap` is instant.

Commands (all joints: <side>_<joint>, side = left|right, joint = shoulder_pan, shoulder_lift,
elbow_flex, wrist_flex, wrist_roll, gripper; degrees, gripper 0-100):
  state                         measured, target, limits, plan length
  move <joint> <delta>          target += delta (relative to the current target, not the measurement)
  set <joint> <value>           absolute target
  pose <json dict>              several absolute targets at once, e.g. pose {"left_gripper": 40}
  grip <side> <0-100>
  goto <side> x y z             Cartesian straight line (m, arm base frame) via IK, pointing down
  stop                          freeze: target = measured, drop any plan
  save <name> / recall <name> [side] / poses     named poses in ./poses.json
  snap [left|right] [path]      write newest wrist-camera frames (default both: ./snap_left.jpg, ./snap_right.jpg)
  thumb <side> <w> <h>          newest frame of that camera resized to w x h, raw BGR bytes base64 (terminal views)
  speed <deg_per_s>             change VEL
  home                          go to the pose captured at start
  torque <left|right|all> on|off   off = arm goes limp for hand-guiding (support it!); on = hold where it is
  shutdown [here]               go home (or stay here), release torque, exit
"""
import json, os, signal, socket, sys, threading, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


SIM = os.environ.get("SO101_SIM") == "1"         # fake arms, no camera: test the stack without hardware
SOCK = os.environ.get("SO101_SOCK", f"{HERE}/{'sim' if SIM else 'robot'}.sock")
LOG, POSES = f"{HERE}/{'sim' if SIM else 'robotd'}.log", f"{HERE}/{'sim_' if SIM else ''}poses.json"
CAMS = {"left": "/dev/v4l/by-id/usb-icSpring_icspring_camera_20240307110322-video-index0",        # left wrist
        "right": "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB2.0_CAM1_USB2.0_CAM1-video-index0"}  # right wrist
PORT = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_{}-if00"
SERIAL = {"left": "5B8E113306", "right": "5B61036524"}
CALIB_DIR = f"{HERE}/calibration"                 # tools/calibrate.sh writes here; the only copy
CALIB = CALIB_DIR + "/bimanual_{}.json"
HZ = 50
VEL = float(os.environ.get("SO101_VEL", 45))       # deg/s for arm joints
GRIP_VEL = 100.0                                   # %/s for grippers
LEAD = float(os.environ.get("SO101_LEAD", 15))     # max |commanded - measured| in deg
P_GAIN = int(os.environ.get("SO101_P", 16))        # servo P gain (LeRobot default 16)
KI = float(os.environ.get("SO101_KI", 3.0))        # deg/s of extra push per deg of steady error
I_MAX = 6.0                                        # cap on that extra push (deg); not on grippers
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
KEYS = [f"{s}_{j}" for s in ("left", "right") for j in JOINTS]


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


# ---------- limits ----------
LIMITS = {}
for side in ("left", "right"):
    for name, c in json.load(open(CALIB.format(side))).items():
        if name == "gripper":
            LIMITS[f"{side}_{name}"] = (0.0, 100.0)
        elif name == "wrist_roll":
            LIMITS[f"{side}_{name}"] = (-170.0, 170.0)
        else:
            half = (c["range_max"] - c["range_min"]) / 2 * 360 / 4095
            LIMITS[f"{side}_{name}"] = (-half, half)


def clamp(k, v):
    lo, hi = LIMITS[k]
    return min(max(v, lo), hi)


# ---------- single instance ----------
_probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    _probe.connect(SOCK)
    sys.exit("robotd is already running (robot.sock answers); not starting a second one")
except (FileNotFoundError, ConnectionRefusedError):
    pass
finally:
    _probe.close()

# ---------- robot ----------
class SimRobot:
    """Stand-in for BiSOFollower: each joint follows its goal with a 0.1 s lag."""
    def __init__(self):
        self.q = {f"{k}.pos": 0.0 for k in KEYS}
        self.q.update({"left_shoulder_lift.pos": -100.0, "right_shoulder_lift.pos": -100.0,
                       "left_elbow_flex.pos": 95.0, "right_elbow_flex.pos": 95.0,
                       "left_wrist_flex.pos": 63.0, "right_wrist_flex.pos": 63.0,
                       "left_gripper.pos": 3.0, "right_gripper.pos": 3.0})
    def connect(self, calibrate=False): pass
    def disconnect(self): pass
    def get_observation(self): return dict(self.q)
    def arm_torque(self, side, on): pass
    def reconfigure(self): pass
    def send_side(self, side, a): self.send_action(a)
    def send_action(self, a):
        for k, v in a.items(): self.q[k] += (v - self.q[k]) * 0.2


if SIM:
    robot = SimRobot()
else:
    from lerobot.robots.bi_so_follower import BiSOFollower, BiSOFollowerConfig
    from lerobot.robots.so_follower import SOFollowerConfig
    arm = lambda side: SOFollowerConfig(port=PORT.format(SERIAL[side]), max_relative_target=None,
                                    position_p_coefficient=P_GAIN)
    from pathlib import Path
    robot = BiSOFollower(BiSOFollowerConfig(id="bimanual", calibration_dir=Path(CALIB_DIR), left_arm_config=arm("left"),
                                            right_arm_config=arm("right")))
robot.connect(calibrate=False)


def arm_torque(side, on):
    if SIM:
        return
    bus = getattr(robot, f"{side}_arm").bus
    bus.enable_torque() if on else bus.disable_torque()


def reconfigure():
    """After a power cut the servos lose their RAM settings: re-apply them, keep torque as requested."""
    if SIM:
        return
    for side in ("left", "right"):
        getattr(robot, f"{side}_arm").configure()
        if not torque_on[side]:
            arm_torque(side, False)


def send(out):
    if SIM:
        robot.send_action(out); return
    for side in ("left", "right"):
        part = {k.removeprefix(side + "_"): v for k, v in out.items() if k.startswith(side + "_")}
        if part:
            getattr(robot, f"{side}_arm").send_action(part)


def read():
    obs = robot.get_observation()
    return {k.removesuffix(".pos"): float(v) for k, v in obs.items() if k.endswith(".pos")}


lock = threading.Lock()
measured = read()
_saved = json.load(open(POSES)) if os.path.exists(POSES) else {}
home = {**measured, **_saved.get("rest", {})}   # `save rest` once with the arms at rest -> safe home forever
target = dict(measured)      # where the user wants each joint
command = dict(measured)     # what the loop currently sends (ramps toward target)
plan = []                    # list of dicts of targets, consumed one at a time (goto waypoints)
offset = {k: 0.0 for k in KEYS}   # slow integral term: compensates gravity sag on loaded joints
torque_on = {"left": True, "right": True}
torque_req = []              # (side, on) applied by the control loop (only it touches the bus)
running = True
loop_stats = {"hz": 0.0, "errors": 0}


def control_loop():
    global measured, command, running
    period, n, t_rate, fails = 1 / HZ, 0, time.perf_counter(), 0
    next_t = time.perf_counter()
    while running:
        try:
            m = read()
        except Exception as e:  # bus error / power off: keep last measurement, send nothing
            loop_stats["errors"] += 1; fails += 1
            if fails == 1 or fails % 250 == 0:
                log(f"WARN read failed ({fails}x): {e}")
            time.sleep(period); next_t = time.perf_counter()
            continue
        fails_before = fails
        with lock:
            measured = m
            if fails >= 10:   # bus was gone (e.g. power cut): never jump to old targets, hold here
                plan.clear(); target.update(m); command.update(m); offset.update({k: 0.0 for k in KEYS})
                log(f"bus back after {fails} failed reads: holding current pose (targets reset)")
            reqs = list(torque_req); torque_req.clear()
            for side, on in reqs:   # torque change: hold exactly where the arm is
                for k in KEYS:
                    if k.startswith(side + "_"):
                        target[k] = command[k] = m[k]; offset[k] = 0.0
                plan[:] = [p for p in plan if not any(k.startswith(side + "_") for k in p)]
                torque_on[side] = on
            for k in KEYS:          # limp arm: targets follow the hand, so torque-on holds the new pose
                if not torque_on[k.split("_")[0]]:
                    target[k] = command[k] = m[k]; offset[k] = 0.0
            # goto waypoints: apply the first one, drop it once the ramp has reached it
            if plan and all(abs(plan[0][k] - command[k]) < 0.5 for k in plan[0]):
                plan.pop(0)
            if plan:
                target.update(plan[0])
            out = {}
            for k in KEYS:
                step = (GRIP_VEL if k.endswith("gripper") else VEL) * period
                c = command[k] + max(-step, min(step, target[k] - command[k]))
                command[k] = clamp(k, max(m[k] - LEAD, min(m[k] + LEAD, c)))   # no wind-up beyond LEAD
                if not k.endswith("gripper") and abs(target[k] - command[k]) < 0.5:
                    err = target[k] - m[k]                        # ramp done: integrate what is left
                    offset[k] = max(-I_MAX, min(I_MAX, offset[k] + KI * err * period))
                if torque_on[k.split("_")[0]]:
                    out[f"{k}.pos"] = clamp(k, command[k] + offset[k])
        try:
            if fails_before >= 10:
                reconfigure(); log("servo settings re-applied after bus loss")
            for side, on in reqs:
                if on:
                    send({k: v for k, v in out.items() if k.startswith(side + "_")})  # goal = here first
                arm_torque(side, on); log(f"torque {side} {'ON' if on else 'OFF'}")
            send(out)
        except Exception as e:
            loop_stats["errors"] += 1
            if loop_stats["errors"] % 50 == 1:
                log(f"WARN write failed: {e}")
        n += 1
        now = time.perf_counter()
        if now - t_rate >= 0.5:
            loop_stats["hz"] = round(n / (now - t_rate), 1); n, t_rate = 0, now
        next_t += period
        time.sleep(max(0, next_t - time.perf_counter()))
        if time.perf_counter() - next_t > 0.5:   # fell far behind: resync instead of bursting
            next_t = time.perf_counter()


# ---------- cameras (one on each wrist) ----------
frames = {"left": None, "right": None}     # side -> (timestamp, BGR image), newest only


def open_cam(dev):
    import cv2
    cam = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280); cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cam


def camera_loop(side):
    import cv2
    while SIM and running:   # synthetic moving test image, labelled with the side
        t = time.time(); yy, xx = np.mgrid[0:360, 0:640]
        f = np.zeros((360, 640, 3), np.uint8); f[..., 0] = xx * 255 // 640; f[..., 1] = yy * 255 // 360
        cv2.circle(f, (int(320 + 200 * np.sin(t)), 180), 50, (0, 0, 255) if side == "left" else (0, 200, 255), -1)
        cv2.putText(f, f"SIM {side.upper()} " + time.strftime("%H:%M:%S"), (20, 340), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)
        frames[side] = (t, f); time.sleep(0.1)
    if SIM:
        return
    cam, warned = open_cam(CAMS[side]), False
    while running:
        ok, f = cam.read()
        if ok:
            frames[side] = (time.time(), f); warned = False
        else:
            if not warned:
                log(f"WARN {side} camera: no frame, reopening"); warned = True
            time.sleep(0.5); cam.release(); cam = open_cam(CAMS[side])
    cam.release()


def get_frame(side):
    if side not in frames:
        raise ValueError(f"unknown camera {side!r} (left|right)")
    if frames[side] is None:
        raise ValueError(f"no {side} camera frame yet")
    return frames[side]


# ---------- commands ----------
def wait_settled(timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with lock:
            if not plan and all(abs(target[k] - command[k]) < 0.5 for k in KEYS):
                return True
        time.sleep(0.05)
    return False


def handle(parts):
    global VEL, running
    cmd = parts[0]
    if cmd in ("move", "set", "grip", "goto", "pose", "recall", "home"):
        if cmd == "pose":
            sides = {k.split("_")[0] for k in json.loads(" ".join(parts[1:]))}
        elif cmd in ("recall", "home"):
            sides = {parts[2]} if cmd == "recall" and len(parts) > 2 else {"left", "right"}
        else:
            sides = {parts[1].split("_")[0]} if len(parts) > 1 else set()
        limp = sorted(s for s in sides if s in torque_on and not torque_on[s])
        if limp:
            raise ValueError(f"{', '.join(limp)} torque is OFF (torque {limp[0]} on first)")
    if cmd == "state":
        with lock:
            return {"measured": {k: round(v, 1) for k, v in measured.items()},
                    "target": {k: round(v, 1) for k, v in target.items()},
                    "command": {k: round(v, 1) for k, v in command.items()},
                    "plan": len(plan), "offset": {k: round(v, 1) for k, v in offset.items() if abs(v) > 0.05}, "torque": dict(torque_on), "vel": VEL, "lead": LEAD, "p_gain": P_GAIN, **loop_stats}
    if cmd == "limits":
        return {k: [round(a, 1), round(b, 1)] for k, (a, b) in LIMITS.items()}
    if cmd in ("move", "set"):
        k, v = parts[1], float(parts[2])
        if k not in LIMITS:
            raise ValueError(f"unknown joint {k}")
        with lock:
            plan.clear()
            target[k] = clamp(k, target[k] + v if cmd == "move" else v)
            return {k: round(target[k], 1)}
    if cmd == "pose":
        new = json.loads(" ".join(parts[1:]))
        with lock:
            plan.clear()
            for k, v in new.items():
                target[k.removesuffix(".pos")] = clamp(k.removesuffix(".pos"), float(v))
        return {"ok": True}
    if cmd == "grip":
        k = f"{parts[1]}_gripper"
        with lock:
            target[k] = clamp(k, float(parts[2]))
        return {k: target[k]}
    if cmd == "stop":
        with lock:
            plan.clear(); target.update(measured); command.update(measured)
            offset.update({k: 0.0 for k in KEYS})
        return {"ok": True}
    if cmd == "torque":
        sides = ["left", "right"] if parts[1] in ("all", "both") else [parts[1]]
        if any(s not in ("left", "right") for s in sides) or parts[2] not in ("on", "off"):
            raise ValueError("usage: torque <left|right|all> on|off")
        with lock:
            torque_req.extend((s, parts[2] == "on") for s in sides)
        return {"torque": {s: parts[2] for s in sides}}
    if cmd == "speed":
        VEL = max(1.0, min(120.0, float(parts[1])))
        return {"vel": VEL}
    if cmd == "goto":
        from kin import fk, ik, J
        side = parts[1]; xyz = np.array([float(v) for v in parts[2:5]])
        if not (0.08 <= xyz[0] <= 0.38 and abs(xyz[1]) <= 0.2 and 0.0 <= xyz[2] <= 0.25):
            raise ValueError(f"target {xyz.tolist()} outside safe box")
        with lock:
            q = np.array([target[f"{side}_{j}"] for j in J])
        start = fk(q); n = max(int(np.linalg.norm(xyz - start) / 0.005), 1)
        path = []
        for i in range(1, n + 1):
            qn, err = ik(q, start + (xyz - start) * i / n)
            jump = float(np.abs(qn - q).max())
            if err > 0.015 or jump > 8:
                raise ValueError(f"ik fails at waypoint {i}/{n}: err={err*1000:.0f}mm jump={jump:.0f}deg")
            path.append({f"{side}_{j}": float(v) for j, v in zip(J, qn)}); q = qn
        with lock:
            plan[:] = path
        log(f"goto {side} {xyz.round(3).tolist()} ({n} waypoints)")
        return {"waypoints": n}
    if cmd == "save":
        saved = json.load(open(POSES)) if os.path.exists(POSES) else {}
        with lock:
            saved[parts[1]] = {k: round(v, 2) for k, v in measured.items()}
        json.dump(saved, open(POSES, "w"), indent=1)
        return {"saved": parts[1]}
    if cmd == "poses":
        return {"poses": list(json.load(open(POSES))) if os.path.exists(POSES) else []}
    if cmd == "recall":
        saved = json.load(open(POSES))[parts[1]]
        side = parts[2] if len(parts) > 2 else ""
        with lock:
            plan.clear()
            for k, v in saved.items():
                k = k.removesuffix(".pos")
                if k.startswith(side):
                    target[k] = clamp(k, v)
        return {"recall": parts[1]}
    if cmd == "snap":
        import cv2
        sides = [parts[1]] if len(parts) > 1 else ["left", "right"]
        out, age = {}, {}
        for side in sides:
            t, img = get_frame(side)
            out[side] = parts[2] if len(parts) > 2 else f"{HERE}/snap_{side}.jpg"
            cv2.imwrite(out[side], img); age[side] = round(time.time() - t, 2)
        return {"paths": out, "age_s": age}
    if cmd == "thumb":
        import base64, cv2
        t, img = get_frame(parts[1])
        w, h = max(1, min(400, int(parts[2]))), max(1, min(300, int(parts[3])))
        small = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        return {"w": w, "h": h, "age_s": round(time.time() - t, 2), "bgr": base64.b64encode(small.tobytes()).decode()}
    if cmd == "home":
        with lock:
            plan.clear(); target.update(home)
        return {"ok": True}
    if cmd == "shutdown":
        here = len(parts) > 1 and parts[1] == "here"
        threading.Thread(target=shutdown, kwargs={"go_home": not here}, daemon=True).start()
        return {"ok": "releasing torque " + ("here" if here else "after going home")}
    raise ValueError(f"unknown command {cmd}")


def shutdown(*_, go_home=True):
    global running
    log("shutdown: " + ("going home" if go_home else "releasing here"))
    with lock:
        plan.clear()
        target.update(home if go_home else measured)
    wait_settled(15)
    time.sleep(0.5)
    running = False


def serve():
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK); srv.listen(16); srv.settimeout(0.5)
    while running:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=client, args=(conn,), daemon=True).start()
    srv.close(); os.unlink(SOCK)


def client(conn):
    with conn, conn.makefile("rw") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            try:
                reply = {"ok": True, **handle(parts)}
                if parts[0] not in ("state", "limits", "poses", "thumb"):
                    log(" ".join(parts) + " -> " + json.dumps(reply)[:200])
            except Exception as e:
                reply = {"ok": False, "error": str(e)}
                log(f"ERR {' '.join(parts)}: {e}")
            f.write(json.dumps(reply) + "\n"); f.flush()


signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
for _side in ("left", "right"):
    threading.Thread(target=camera_loop, args=(_side,), daemon=True).start()
threading.Thread(target=serve, daemon=True).start()
log(f"robotd ready{' (SIM)' if SIM else ''}: {HZ} Hz, vel {VEL} deg/s, lead {LEAD} deg, P {P_GAIN}, KI {KI}; home=" +
    json.dumps({k: round(v, 1) for k, v in home.items()}))
try:
    control_loop()
finally:
    robot.disconnect()
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    log("robotd stopped, torque released")
