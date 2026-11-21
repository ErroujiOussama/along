"""robotd: the one process that owns both SO-101 arms and the cameras.

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
- One thread per camera (left, right wrist; scene) keeps the newest frame, so `snap` is instant.

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
  snap [cam] [path]             write newest camera frames (default all connected: ./snap_<cam>.jpg); cam = left|right|scene
  cams                          per camera: ok (fresh frame), age_s, device
  camctl <cam> [k=v ...] [reset]   list / set+save V4L2 controls (auto_exposure 1=manual 3=auto,
                                exposure_time_absolute, gain, brightness, power_line_frequency 1=50 Hz, focus...)
  thumb <side> <w> <h>          (max 960x540) newest frame of that camera resized to w x h, raw BGR bytes base64 (terminal views)
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
        "right": "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB2.0_CAM1_USB2.0_CAM1-video-index0",  # right wrist
        "scene": "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_DB9E36FF-video-index0"}                  # Logitech C920
STALE = 2.0      # s without a new frame = camera counts as gone (unplugged)
CAMCFG = f"{HERE}/cams.json"   # {cam: {v4l2 control: value}}, applied whenever a camera (re)opens; `camctl` edits it
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


def reconnect():
    """USB replug / power cycle: drop the dead port handles and reopen both arms by their by-id paths."""
    if SIM:
        return
    for side in ("left", "right"):
        bus = getattr(robot, f"{side}_arm").bus
        try:
            bus.disconnect(disable_torque=False)
        except Exception:
            bus._bus = None
        bus.connect()   # raises while the adapter or the servo power is missing; retried by the loop


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
BLOCK_DEG, BLOCK_S = 5.0, 0.4  # arm joint pushed > BLOCK_DEG off but not moving for BLOCK_S -> blocked: hold that arm
events = []                  # recent BLOCKED events (dicts with "t")
from collections import deque
traj = deque(maxlen=6000)   # (t, measured in KEYS order), 10 min at 10 Hz
MIN_MOVE, MIN_GOTO, MIN_FIX, MIN_DEG = not SIM, 0.02, 0.01, 5.0   # real arms: no creeping. One 1-2 cm fix per approach
small_fix_used = {"left": False, "right": False}
collide_cfg = {"on": SIM, "handover": False, "objects": []}   # objects: agent-placed (world frame), jaws may touch


def control_loop():
    global measured, command, running
    period, n, t_rate, fails, last_reconnect = 1 / HZ, 0, time.perf_counter(), 0, 0.0
    stuck = {}   # joint -> (since, measured then)
    next_t = time.perf_counter()
    while running:
        try:
            m = read()
        except Exception as e:  # bus error / power off: keep last measurement, send nothing
            loop_stats["errors"] += 1; fails += 1
            if fails == 1 or fails % 250 == 0:
                log(f"WARN read failed ({fails}x): {e}")
            if fails >= 25 and time.time() - last_reconnect > 2:
                last_reconnect = time.time()
                try:
                    reconnect(); log("serial ports reopened")
                except Exception as e:
                    if fails % 250 < 25:
                        log(f"reconnect failed: {str(e).strip().splitlines()[0][:120]}")
            time.sleep(period); next_t = time.perf_counter()
            continue
        fails_before = fails
        with lock:
            measured = m
            if n % 5 == 0:   # 10 Hz joint history for the rollout inspector (`traj`)
                traj.append((round(time.time(), 2), [round(m[k], 1) for k in KEYS]))
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
            now = time.time()
            for k in KEYS:          # block detection: pushing but not moving = something is in the way
                side = k.split("_")[0]
                if k.endswith("gripper") or not torque_on[side] or abs(command[k] - m[k]) <= BLOCK_DEG:
                    stuck.pop(k, None); continue
                t0, m0 = stuck.setdefault(k, (now, m[k]))
                if abs(m[k] - m0) > 1.0:
                    stuck[k] = (now, m[k])
                elif now - t0 > BLOCK_S:
                    ev = {"t": now, "joint": k, "target": round(target[k], 1), "measured": round(m[k], 1)}
                    events.append(ev); del events[:-20]
                    log(f"BLOCKED {k}: target {ev['target']} measured {ev['measured']} -> holding {side} arm here")
                    for kk in KEYS:
                        if kk.startswith(side + "_") and not kk.endswith("gripper"):
                            target[kk] = command[kk] = m[kk]; offset[kk] = 0.0; stuck.pop(kk, None)
                    plan[:] = [p for p in plan if not any(kk.startswith(side + "_") for kk in p)]
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


# ---------- cameras (one per wrist + scene); replug-safe: reopen by by-id path when it comes back ----------
frames = {name: None for name in CAMS}     # name -> (timestamp, BGR image), newest only


RES = {"scene": (1920, 1080)}    # others 1280x720


def open_cam(dev, side=None):
    import cv2
    cam = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    w, h = RES.get(side, (1280, 720))
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, w); cam.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
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
    dev, cam, up = CAMS[side], None, None
    while running:
        if cam is None:
            if not os.path.exists(dev):          # unplugged: wait for the by-id link to come back
                if up is not False:
                    log(f"WARN {side} camera: not connected ({dev})"); up = False
                time.sleep(1); continue
            cam = open_cam(dev, side)
            apply_camcfg(side)
        ok, f = cam.read()
        if ok:
            frames[side] = (time.time(), f)
            if up is not True:
                log(f"{side} camera: streaming"); up = True
        else:
            if up is not False:
                log(f"WARN {side} camera: no frame, reopening"); up = False
            cam.release(); cam = None; time.sleep(0.5)
    if cam is not None:
        cam.release()


def load_camcfg():
    try:
        return json.load(open(CAMCFG))
    except (OSError, ValueError):
        return {}


def apply_camcfg(side):
    cfg = load_camcfg().get(side)
    if cfg and not SIM:
        try:
            import v4l; v4l.set_controls(CAMS[side], cfg)
        except Exception as e:
            log(f"WARN {side} camera settings {cfg}: {e}")


def camctl(parts):
    """camctl <cam> [key=value ...] [reset]: set + save controls; returns all controls of that camera."""
    if len(parts) < 2 or parts[1] not in CAMS:
        raise ValueError(f"usage: camctl <{'|'.join(CAMS)}> [control=value ...] [reset]")
    name, cfg = parts[1], load_camcfg()
    if SIM:
        return {"cam": name, "controls": {}, "saved": cfg.get(name, {})}
    import v4l
    if "reset" in parts[2:]:      # driver defaults for everything, forget saved settings
        for k, c in sorted(v4l.controls(CAMS[name]).items(), key=lambda kc: "auto" not in kc[0]):
            try:
                v4l.set_controls(CAMS[name], {k: c["default"]})
            except OSError:
                pass              # read-only / not settable in the current mode
        cfg.pop(name, None)
    new = dict(kv.split("=", 1) for kv in parts[2:] if "=" in kv)
    if new:
        v4l.set_controls(CAMS[name], {k: int(v) for k, v in new.items()})
        cfg[name] = {**cfg.get(name, {}), **{k: int(v) for k, v in new.items()}}
    if new or "reset" in parts[2:]:
        json.dump(cfg, open(CAMCFG, "w"), indent=1)
    ctl = {k: {x: c[x] for x in ("value", "min", "max", "default", "inactive")} for k, c in v4l.controls(CAMS[name]).items()}
    return {"cam": name, "controls": ctl, "saved": cfg.get(name, {})}


def get_frame(side):
    if side not in frames:
        raise ValueError(f"unknown camera {side!r} ({'|'.join(CAMS)})")
    if frames[side] is None:
        raise ValueError(f"no {side} camera frame yet")
    if time.time() - frames[side][0] > STALE:
        raise ValueError(f"{side} camera: no frame for {time.time() - frames[side][0]:.0f} s (unplugged?)")
    return frames[side]


def cams():
    now = time.time()
    return {n: {"ok": f is not None and now - f[0] <= STALE, "age_s": None if f is None else round(now - f[0], 1),
                "dev": CAMS[n]} for n, f in frames.items()}


# ---------- commands ----------
def wait_settled(timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with lock:
            if not plan and all(abs(target[k] - command[k]) < 0.5 for k in KEYS):
                return True
        time.sleep(0.05)
    return False


import collide


def guard(goals):
    """Raise ValueError if moving to goals (list of {key: deg}, applied in turn) would collide (collide.py)."""
    if not collide_cfg["on"]:
        return
    import collide
    with lock:
        start, tgt = dict(command), dict(target)
    cfg = collide_setup()
    msg = collide.check(start, [{**tgt, **goals[0]}] + goals[1:], cfg)
    if msg:
        log(msg)
        raise ValueError(msg)


def collide_setup():
    import collide
    cfg = collide.setup()
    cfg["obstacles"] = cfg["obstacles"] + collide_cfg["objects"]
    if collide_cfg["handover"]:
        cfg["arm_gap"] = 0.0
    return cfg


def guarded(side, goal_q, path=None):
    """Waypoints for `side` to joint goal goal_q: the given/straight path if it passes guard(), else a planned
    detour (planner.py). -> (waypoints, how)."""
    path = path or [{f"{side}_{j}": float(v) for j, v in zip(collide.J, goal_q)}]
    if not collide_cfg["on"]:
        return path, "straight"
    with lock:
        start, tgt = dict(command), dict(target)
    e = collide.check(start, [{**tgt, **path[0]}] + path[1:], collide_setup())   # planning uses the model even when live checks are off
    if not e:
        return path, "straight"
    import planner
    try:
        wps = planner.plan(side, start, goal_q, LIMITS, collide_setup())
    except ValueError as e2:
        log(f"{e} (and no detour: {e2})")
        raise ValueError(f"{e} (and no detour: {e2})")
    return wps, f"detour ({len(wps)} waypoints) around {e.split(' would')[0].removeprefix('refused: ')}"


def collide_state(m):
    try:
        return [f"{lab} {cm:.1f} cm" for lab, cm in collide.near(m, collide_setup())]
    except Exception as e:
        return [f"collision model error: {e}"]


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
                    "plan": len(plan), "offset": {k: round(v, 1) for k, v in offset.items() if abs(v) > 0.05}, "torque": dict(torque_on), "vel": VEL, "lead": LEAD, "p_gain": P_GAIN, **loop_stats,
                    "blocked": [dict(e, age_s=round(time.time() - e["t"], 1)) for e in events if time.time() - e["t"] < 120],
                    "near": collide_state(measured), "collide": dict(collide_cfg)}
    if cmd == "traj":   # traj <t0> [t1]: joint history (10 Hz) -> {"keys": KEYS, "t": [...], "q": [[...]]}
        t0, t1 = float(parts[1]), float(parts[2]) if len(parts) > 2 else time.time() + 1
        with lock:
            pts = [p for p in traj if t0 <= p[0] <= t1]
        return {"keys": KEYS, "t": [p[0] for p in pts], "q": [p[1] for p in pts]}
    if cmd == "limits":
        return {k: [round(a, 1), round(b, 1)] for k, (a, b) in LIMITS.items()}
    if cmd in ("move", "set"):
        k, v = parts[1], float(parts[2])
        if k not in LIMITS:
            raise ValueError(f"unknown joint {k}")
        with lock:
            new = clamp(k, target[k] + v if cmd == "move" else v)
            if MIN_MOVE and not k.endswith(("gripper", "wrist_roll")) and 0 < abs(new - target[k]) < MIN_DEG:
                raise ValueError(f"{k} change {new - target[k]:+.1f} deg is too small: joint moves are >= {MIN_DEG} deg "
                                 "(no creeping; estimate better and make one decisive move)")
        guard([{k: new}])
        with lock:
            plan.clear()
            target[k] = new
            return {k: round(target[k], 1)}
    if cmd == "pose":
        new = json.loads(" ".join(parts[1:]))
        guard([{k.removesuffix(".pos"): clamp(k.removesuffix(".pos"), float(v)) for k, v in new.items()}])
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
    if cmd == "object":   # object <name> box <side> cx cy cz sx sy sz [yaw] | <name> capsule <side> ax ay az bx by bz r | clear [name] | set <json>
        objs = collide_cfg["objects"]
        if len(parts) > 1 and parts[1] == "clear":
            objs[:] = [o for o in objs if len(parts) > 2 and o["name"] != parts[2]]
        elif len(parts) > 1 and parts[1] == "set":
            objs[:] = json.loads(" ".join(parts[2:]))
        elif len(parts) > 3:
            solid = parts[-1] == "solid"; parts = parts[:-1] if solid else parts
            name, shape, side, v = parts[1], parts[2], parts[3], [float(x) for x in parts[4:]]
            T = collide.base_T(side, collide.setup())
            w = lambda p: (T @ np.r_[p, 1.0])[:3].round(4).tolist()
            if shape == "box" and len(v) in (6, 7):
                o = {"name": name, "box": (w(v[:3]), v[3:6], (v[6] if len(v) == 7 else 0.0) + float(np.degrees(np.arctan2(T[1, 0], T[0, 0]))))}
            elif shape == "capsule" and len(v) == 7:
                o = {"name": name, "cap": (w(v[:3]), w(v[3:6]), v[6])}
            else:
                raise ValueError("usage: object <name> box <side> cx cy cz sx sy sz [yaw_deg] | object <name> capsule <side> ax ay az bx by bz r"
                                 " (m, in that arm's base frame) [solid] | object clear [name]")
            objs[:] = [x for x in objs if x["name"] != name] + [dict(o, grasp=not solid)]
            log(f"object {o}")
        return {"objects": objs, "near": collide_state(dict(measured))}
    if cmd in ("collide", "handover"):
        if len(parts) > 1:
            if parts[1] not in ("on", "off"):
                raise ValueError(f"usage: {cmd} [on|off]")
            collide_cfg["on" if cmd == "collide" else "handover"] = parts[1] == "on"
            log(f"{cmd} {parts[1]}")
        with lock:
            m = dict(measured)
        return {**collide_cfg, "setup": collide.setup(), "near": collide_state(m)}
    if cmd == "speed":
        VEL = max(1.0, min(360.0 if SIM else 120.0, float(parts[1])))
        return {"vel": VEL}
    if cmd == "goto":
        from kin import fk, J, K
        side = parts[1]; xyz = np.array([float(v) for v in parts[2:5]])
        if not (0.08 <= xyz[0] <= 0.38 and abs(xyz[1]) <= 0.2 and 0.0 <= xyz[2] <= 0.25):
            raise ValueError(f"target {xyz.tolist()} outside safe box")
        with lock:
            q = np.array([target[f"{side}_{j}"] for j in J])
        T0 = K.forward_kinematics(q); start = T0[:3, 3].copy(); n = max(int(np.linalg.norm(xyz - start) / 0.005), 1)
        dist = float(np.linalg.norm(xyz - start))
        if MIN_MOVE and dist < MIN_GOTO:
            if dist < MIN_FIX or small_fix_used[side]:
                raise ValueError(f"goto of {dist * 100:.1f} cm is too small: gotos are >= {MIN_GOTO * 100:.0f} cm, plus ONE "
                                 f"{MIN_FIX * 100:.0f}-{MIN_GOTO * 100:.0f} cm fix per approach (used: {small_fix_used[side]}). "
                                 "No creeping: back off >= 2 cm and come in again with a better estimate")
            small_fix_used[side] = True
        else:
            small_fix_used[side] = False
        path = []
        for i in range(1, n + 1):
            T = T0.copy(); T[:3, 3] = start + (xyz - start) * i / n
            a = np.arctan2(T[1, 3], T[0, 3]) - np.arctan2(start[1], start[0])   # keep the orientation (incl. roll), turned by the pan
            T[:3, :3] = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]]) @ T0[:3, :3]
            qn = K.inverse_kinematics(q, T, 1.0, 0.1, 60)
            err = float(np.linalg.norm(fk(qn) - T[:3, 3]))
            jump = float(np.abs(qn - q).max())
            if err > 0.015 or jump > 12:
                raise ValueError(f"ik fails at waypoint {i}/{n}: err={err*1000:.0f}mm jump={jump:.0f}deg")
            path.append({f"{side}_{j}": float(v) for j, v in zip(J, qn)}); q = qn
        path, how = guarded(side, q, path)
        with lock:
            plan[:] = path
        log(f"goto {side} {xyz.round(3).tolist()} ({len(path)} waypoints, {how})")
        return {"waypoints": len(path), "path": how}
    if cmd == "save":
        saved = json.load(open(POSES)) if os.path.exists(POSES) else {}
        with lock:
            saved[parts[1]] = {k: round(v, 2) for k, v in measured.items()}
        json.dump(saved, open(POSES, "w"), indent=1)
        return {"saved": parts[1]}
    if cmd == "poses":
        return {"poses": list(json.load(open(POSES))) if os.path.exists(POSES) else []}
    if cmd == "recall":
        allp = json.load(open(POSES)) if os.path.exists(POSES) else {}
        if SIM and parts[1] not in allp and os.path.exists(f"{HERE}/poses.json"):   # plan checks use the real poses
            allp = json.load(open(f"{HERE}/poses.json"))
        if parts[1] not in allp:
            raise ValueError(f"unknown pose {parts[1]} (have: {', '.join(allp)})")
        saved = allp[parts[1]]
        side = parts[2] if len(parts) > 2 else ""
        goal = {k.removesuffix(".pos"): clamp(k.removesuffix(".pos"), v) for k, v in saved.items() if k.startswith(side)}
        if side:   # one arm: take a detour if the direct move would hit something
            wps, how = guarded(side, [goal[f"{side}_{j}"] for j in collide.J])
            if how != "straight":
                with lock:
                    target[f"{side}_gripper"] = goal[f"{side}_gripper"]; plan[:] = wps
                log(f"recall {parts[1]} {side}: {how}")
                return {"recall": parts[1], "path": how}
        else:
            guard([goal])
        with lock:
            plan.clear()
            for k, v in saved.items():
                k = k.removesuffix(".pos")
                if k.startswith(side):
                    target[k] = clamp(k, v)
        return {"recall": parts[1]}
    if cmd == "snap":
        import cv2
        sides = [parts[1]] if len(parts) > 1 else [n for n, c in cams().items() if c["ok"]]
        out, age = {}, {}
        for side in sides:
            t, img = get_frame(side)
            out[side] = parts[2] if len(parts) > 2 else f"{HERE}/snap_{side}.jpg"
            cv2.imwrite(out[side], img); age[side] = round(time.time() - t, 2)
        return {"paths": out, "age_s": age, "missing": [n for n in CAMS if n not in out]}
    if cmd == "cams":
        return {"cams": cams()}
    if cmd == "camctl":
        return camctl(parts)
    if cmd == "thumb":
        import base64, cv2
        t, img = get_frame(parts[1])
        w, h = max(1, min(960, int(parts[2]))), max(1, min(540, int(parts[3])))
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
                if parts[0] not in ("state", "limits", "poses", "thumb", "cams") and parts[:1] + parts[2:] != ["camctl"]:   # quiet polls
                    log(" ".join(parts) + " -> " + json.dumps(reply)[:200])
            except Exception as e:
                reply = {"ok": False, "error": str(e)}
                if parts[0] not in ("thumb", "cams"):   # panels poll thumb ~10x/s; don't flood the log
                    log(f"ERR {' '.join(parts)}: {e}")
            f.write(json.dumps(reply) + "\n"); f.flush()


signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
for _side in CAMS:
    threading.Thread(target=camera_loop, args=(_side,), daemon=True).start()
threading.Thread(target=serve, daemon=True).start()
log(f"robotd ready{' (SIM)' if SIM else ''}: {HZ} Hz, vel {VEL} deg/s, lead {LEAD} deg, P {P_GAIN}, KI {KI}; home=" +
    json.dumps({k: round(v, 1) for k, v in home.items()}))
try:
    control_loop()
finally:
    try:
        robot.disconnect()
    except Exception as e:
        log(f"disconnect failed (bus gone?): {e}")
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    log("robotd stopped, torque released")
