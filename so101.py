"""Python API for the bimanual SO-101 (client of robotd; robotd must be running).

    from so101 import Robot
    bot = Robot()
    bot.state()                       # {'measured': {...}, 'target': {...}, 'hz': 50, ...}
    bot.left.move(elbow=-5)           # relative, waits until settled; returns joints still off target
    bot.right.set(lift=-40, elbow=60) # absolute (deg); several joints move together
    bot.left.open(); bot.left.close(); bot.right.grip(30)
    bot.left.goto(0.20, 0.0, 0.08)    # straight Cartesian line (m, arm base frame), gripper down
    bot.left.pose()                   # {'shoulder_pan': 1.2, ...} measured
    bot.left.tip()                    # forward kinematics of the measured pose (m)
    bot.snap()                        # all connected cameras -> {'left': 'snap_left.jpg', ..., 'scene': ...}
    bot.step("move right_elbow_flex -5")  # raw cmds, wait, one small mosaic -> {'ran', 'state', 'image', 'missing_cams'}
    bot.snap_all()                    # wrist cams 400x225 on top, scene 800x450 below -> snap_all.jpg; bot.mosaic() -> numpy BGR
    bot.right.ik(0.3, 0.05, 0.06)     # dry run: joint targets for a tip xyz; bot.right.nudge(1, 0, -0.5) moves the tip (cm)
    bot.right.holding()               # gripper closed on something (stopped >= 5 short of a target <= 10)
    bot.left.frame()                  # newest left wrist-camera frame as numpy BGR; bot.left.snap() -> path
    bot.left.thumb(80, 45)            # small live frame (what the panel's camera view uses)
    bot.save("ready"); bot.recall("ready", side="left")
    bot.stop(); bot.home()
    bot.left.torque(False)            # limp for hand-guiding (hold it!); bot.left.torque(True) holds the new pose

Every motion method waits for the motion to finish unless wait=False. Joint aliases:
pan, lift, elbow, wrist, roll, gripper (full names like elbow_flex also work).
"""
import errno, json, os, socket, time

HERE = os.path.dirname(os.path.abspath(__file__))
SOCK = os.environ.get("SO101_SOCK") or os.path.join(HERE, "sim.sock" if os.environ.get("SO101_SIM") == "1" else "robot.sock")
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
ALIAS = {"pan": "shoulder_pan", "lift": "shoulder_lift", "shoulder": "shoulder_lift", "elbow": "elbow_flex",
         "wrist": "wrist_flex", "flex": "wrist_flex", "roll": "wrist_roll", "grip": "gripper"}


class RobotError(RuntimeError):
    pass


def holding(target, measured):
    """Gripper jaws stopped on something: commanded (nearly) closed but measured >= 5 more open."""
    return target <= 10 and measured - target >= 5


def tile(msg, w=400, h=225, n=1):
    """Grey w*n x h placeholder with a message (camera unplugged, robotd gone)."""
    import cv2, numpy as np
    t = np.full((h, w * n, 3), 60, np.uint8)
    cv2.putText(t, msg[:w // 10], (10, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
    return t


def joint_name(name):
    j = ALIAS.get(name, name)
    if j not in JOINTS:
        raise RobotError(f"unknown joint {name!r}; use one of {list(ALIAS)} or {JOINTS}")
    return j


class Robot:
    def __init__(self, sock=SOCK, timeout=5.0):
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.settimeout(timeout)
        try:
            self._s.connect(sock)
        except OSError as e:
            self._s.close()
            if not os.path.exists(sock) or e.errno in (errno.ECONNREFUSED, errno.ENOENT):
                raise RobotError("robotd is not running: systemctl --user start robotd") from e
            raise RobotError(f"robotd socket {sock} exists but cannot be opened ({e.strerror or e}; sandboxed? "
                             "run robot with escalated permissions / full access)") from e
        self._f = self._s.makefile("rw")
        self.sim, self.tag = os.path.basename(sock).startswith("sim"), None   # tag: plan id for the rollout log
        self.left, self.right = Arm(self, "left"), Arm(self, "right")

    # ---- protocol ----
    def call(self, line):
        """Send one raw robotd command; returns the reply dict or raises RobotError."""
        self._f.write(line + "\n"); self._f.flush()
        reply = json.loads(self._f.readline())
        if not reply.get("ok"):
            raise RobotError(reply.get("error", reply))
        return reply

    def close(self):
        self._s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- whole robot ----
    def state(self):
        return self.call("state")

    def limits(self):
        r = self.call("limits"); r.pop("ok"); return r

    def wait(self, timeout=20.0, tol=1.5):
        """Block until all ramps and goto waypoints are done. Returns {joint: (target, measured)} still off by > tol."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.state()
            if st["plan"] == 0 and all(abs(st["target"][k] - st["command"][k]) < 0.5 for k in st["target"]):
                time.sleep(0.4)
                st = self.state()
                return {k: (st["target"][k], st["measured"][k]) for k in st["target"]
                        if abs(st["target"][k] - st["measured"][k]) > tol}
            time.sleep(0.05)
        raise RobotError("timeout waiting for the arms to reach their targets")

    def _motion(self, line, wait):
        self.call(line)
        return self.wait() if wait else None

    def set(self, targets, wait=True):
        """Absolute targets for any joints: {'left_elbow_flex': 50, 'right_gripper': 40}."""
        return self._motion("pose " + json.dumps(targets), wait)

    def stop(self):
        self.call("stop")

    def home(self, wait=True):
        return self._motion("home", wait)

    def torque(self, on, side="all"):
        """Torque on/off for one side or both. Off = limp (support the arm); on = hold where it is."""
        self.call(f"torque {side} {'on' if on else 'off'}")
        time.sleep(0.1)   # applied by the next control-loop tick

    def speed(self, deg_per_s):
        return self.call(f"speed {deg_per_s}")["vel"]

    def snap(self, side=None, path=None):
        """Write the newest camera frames. side=None: all connected -> {cam: path}; cam given -> path."""
        if side is None:
            return self.call("snap")["paths"]
        return self.call(f"snap {side}" + (f" {path}" if path else ""))["paths"][side]

    def cams(self):
        """{name: {"ok": fresh frame, "age_s", "dev"}} for every camera robotd knows (left, right, scene)."""
        return self.call("cams")["cams"]

    def camctl(self, cam, reset=False, **controls):
        """List / set + save a camera's V4L2 controls, e.g. camctl("scene", auto_exposure=1, exposure_time_absolute=120).
        Saved in cams.json and re-applied whenever robotd (re)opens the camera."""
        return self.call(" ".join(["camctl", cam] + (["reset"] if reset else []) + [f"{k}={int(v)}" for k, v in controls.items()]))

    def frame(self, side):
        import cv2
        return cv2.imread(self.snap(side))

    def thumb(self, side, w, h):
        """Newest frame of one camera (left|right|scene) resized to w x h, as numpy BGR (cheap; for live previews)."""
        import base64, numpy as np
        r = self.call(f"thumb {side} {w} {h}")
        return np.frombuffer(base64.b64decode(r["bgr"]), np.uint8).reshape(r["h"], r["w"], 3)

    def mosaic(self, names=None, w=400, h=225, missing=None):
        """All cameras side by side (default every camera robotd knows), w x h each, labelled, as numpy BGR.
        A failing camera becomes a grey tile with the error (its name is appended to `missing` if given).
        Connection errors (robotd gone) propagate."""
        import cv2, numpy as np
        tiles = []
        for n in names or list(self.cams()):
            try:
                t = self.thumb(n, w, h).copy()
            except RobotError as e:
                t = tile(str(e), w, h)
                if missing is not None:
                    missing.append(n)
            cv2.putText(t, n, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
            cv2.putText(t, n, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            tiles.append(t)
        return np.hstack(tiles)

    def snap_all(self, path=None, missing=None, looks=()):
        """Write one JPEG: wrist cameras on top (left | right, 400x225 each), scene below at 800x450, then (after gotos)
        a strip of wrist frames taken where each goto ended; returns its path."""
        import cv2, numpy as np
        path = path or os.path.join(HERE, "snap_all.jpg")
        rows = [self.mosaic(["left", "right"], missing=missing), self.mosaic(["scene"], 800, 450, missing)]
        if looks:
            strip = np.hstack(list(looks) + [np.zeros((150, 266, 3), np.uint8)] * (3 - len(looks)))
            rows.append(np.hstack([strip, np.zeros((150, 800 - strip.shape[1], 3), np.uint8)]))
        img = np.vstack(rows)
        cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return path

    def arms(self, st=None):
        """Compact per-arm summary: measured/target joints, joints > 3 deg off, torque, holding, tip xyz (m)."""
        import sys, numpy as np
        sys.path.insert(0, HERE)
        from kin import fk
        st = st or self.state()
        out = {}
        for s in ("left", "right"):
            m, t = ({j: st[w][f"{s}_{j}"] for j in JOINTS} for w in ("measured", "target"))
            out[s] = {"measured": m, "target": t, "off": [j for j in JOINTS if abs(m[j] - t[j]) > 3],
                      "torque": st["torque"][s], "holding": holding(t["gripper"], m["gripper"]),
                      "tip": [round(v, 3) for v in fk(np.array([m[j] for j in JOINTS[:5]]))]}
        return out

    def step(self, cmds="", image=None):
        """Run ';'-separated raw robotd commands (like `robot raw`, plus `nudge <side> dx dy dz` in cm),
        wait until settled, write snap_all.jpg. Command errors are reported, not raised.
        -> {"ran": [[cmd, reply or "ERROR: ..."]], "state": arms() (+ "tip_delta" since the last step),
            "blocked": BLOCKED events during this step, "near": close link pairs, "image", "missing_cams"}"""
        ran, looks, cmds, t0 = [], [], [c.strip() for c in cmds.split(";") if c.strip()], time.time()
        for i, c in enumerate(cmds + ([] if cmds[-1:] == ["wait"] else ["wait"])):
            try:
                if c == "wait":     # a gripper closed on something never reaches its target: not news
                    r = {"off_target": {k: v for k, v in self.wait().items() if not (k.endswith("gripper") and v[0] <= 10)}}
                elif c.split()[0] == "nudge":
                    _, sd, *d = c.split(); r = {"joints_moved": getattr(self, sd).nudge(*map(float, d))}
                else:
                    r = self.call(c)
                    if c.split()[0] == "goto":   # runs in the background (the next command would cancel it): finish it
                        self.wait(); p = c.split()
                        off = sum((a - float(b)) ** 2 for a, b in zip(getattr(self, p[1]).tip(), p[2:5])) ** 0.5
                        looks.append(self._look(p[1], " ".join(p[:5])))
                        if off > 0.01:
                            raise ValueError(f"tip ended {off * 100:.1f} cm from the goal")
                r.pop("ok", None)
            except (RobotError, ValueError, TypeError, AttributeError) as e:
                r = f"ERROR: {e}"
            if i < len(cmds) or r != {"off_target": {}}:   # the final implicit wait only when it has news
                ran.append([c, r])
        st = self.state()
        missing, arms = [], self.arms(st)
        last = os.path.join(HERE, ".last_tips_sim.json" if os.environ.get("SO101_SIM") == "1" else ".last_tips.json")
        try:
            prev = json.load(open(last))
            for sd, a in arms.items():
                a["tip_delta"] = [round(x - y, 3) for x, y in zip(a["tip"], prev[sd])]
        except (OSError, ValueError, KeyError):
            pass
        json.dump({sd: a["tip"] for sd, a in arms.items()}, open(last, "w"))
        out = {"ran": ran, "state": arms, "blocked": [e for e in st.get("blocked", []) if e["t"] >= t0 - 0.5],
               "near": st.get("near", []), "image": self.snap_all(image, missing=missing, looks=looks[-3:]), "missing_cams": missing}
        try:
            self._record(cmds, out, t0, st)
        except Exception as e:      # the log must never break a step
            out["log_error"] = str(e)
        return out

    def _record(self, cmds, r, t0, st):
        """Append this step to rollouts/<date>/log.jsonl (+ its image and 10 Hz joint history) for `robot stream` /inspect."""
        import shutil
        if os.environ.get("SO101_NOLOG"):
            return
        d = os.path.join(HERE, "rollouts", time.strftime("%Y-%m-%d")); os.makedirs(d, exist_ok=True)
        rid = time.strftime("%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}" + ("s" if self.sim else "r")
        if r.get("image") and os.path.exists(r["image"]):
            shutil.copy(r["image"], os.path.join(d, rid + ".jpg"))
        tr = self.call(f"traj {t0 - 0.2}")
        rec = {"id": rid, "t0": t0, "t1": time.time(), "kind": "sim" if self.sim else "real", "plan": self.tag,
               "cmds": cmds, "ran": [[c, x if isinstance(x, str) else json.dumps(x)[:300]] for c, x in r["ran"]],
               "blocked": r["blocked"], "near": r["near"], "tips": {sd: a["tip"] for sd, a in r["state"].items()},
               "objects": st.get("collide", {}).get("objects", []), "traj": {k: tr[k] for k in ("keys", "t", "q")}}
        with open(os.path.join(d, "log.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def plan(self, cmds, dry=False):
        """Check a whole ';'-separated plan (step commands) on the SIM robotd started from the real pose, then run it for
        real if every command passed. -> {"sim": step result on the sim, "ok": bool, "real": step result or None}.
        The sim has the same collision model (collide.py + scene.json) and ramps; it is started if not running."""
        import subprocess
        sim_sock = os.path.join(HERE, "sim.sock")
        try:
            sim = Robot(sim_sock)
        except RobotError:
            subprocess.Popen([os.path.join(HERE, ".venv/bin/python"), os.path.join(HERE, "robotd.py")], cwd=HERE,
                             env={**os.environ, "SO101_SIM": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            for _ in range(50):
                time.sleep(0.2)
                try:
                    sim = Robot(sim_sock); break
                except RobotError:
                    pass
            else:
                raise RobotError("could not start the sim robotd")
        with sim:
            real = self.state()
            sim.call("collide off"); sim.call("speed 360")
            sim.call("pose " + json.dumps(real["target"])); sim.wait(timeout=30)
            sim.call("collide on")   # the plan check always uses the collision model
            sim.call(f"handover {'on' if real['collide']['handover'] else 'off'}")
            sim.call("object set " + json.dumps(real["collide"].get("objects", [])))
            sim.tag = self.tag = time.strftime("%H%M%S")
            r = sim.step(cmds, image=os.path.join(HERE, ".plan_sim.jpg"))
        ok = not any(isinstance(x[1], str) for x in r["ran"]) and not r["blocked"]
        try:
            return {"sim": r, "ok": ok, "real": self.step(cmds) if ok and not dry else None}
        finally:
            self.tag = None

    def locate(self, u, v, z=None):
        """Scene-camera pixel (u, v) in the 800x450 scene image (bottom of snap_all.jpg: subtract 225 from y) -> the point
        where that ray meets height z (m, world = left base frame; default the table) in both arms' base frames."""
        import sys, numpy as np
        sys.path.insert(0, HERE)
        import collide
        s = collide.setup(); c = json.load(open(os.path.join(HERE, "scene.json")))["scene_camera"]
        f, up = np.array(c["forward"]), np.array(c["up"]); r = np.cross(f, up); (w, h), fp = c["image_size_px"], c["focal_px"]
        d = f + r * (u - w / 2) / fp - up * (v - h / 2) / fp
        z = s["table_z"] if z is None else z
        p = np.array(c["position"]) + d * (z - c["position"][2]) / d[2]
        out = {}
        for side in ("left", "right"):
            q = np.linalg.inv(collide.base_T(side, s)) @ np.r_[p, 1.0]
            out[side] = [round(float(x), 3) for x in q[:3]]
        return out

    def _cam(self, side, st=None):
        """Wrist camera position and viewing direction (m, that arm's base frame) from the measured pose."""
        import sys, numpy as np
        sys.path.insert(0, HERE)
        import collide
        m = (st or self.state())["measured"]; F = collide.frames(np.array([m[f"{side}_{j}"] for j in collide.J]))[0, 5]
        cam = collide.setup()["mounted"].get(side, collide.CAPS["wrist_cam"]); cc = (np.array(cam[1]) + np.array(cam[2])) / 2
        return [round(float(x), 3) for x in (F @ np.r_[cc, 1])[:3]], [round(float(x), 2) for x in F[:3, :3] @ [0, 0, -1.0]]

    def _look(self, side, label):
        """Wrist frame right now (266x150) labelled with the goto and the camera pose: the 'scan as you go' strip."""
        import cv2
        try:
            t = self.thumb(side, 266, 150).copy()
        except RobotError as e:
            t = tile(str(e), 266, 150)
        p, d = self._cam(side)
        for col, th in (((0, 0, 0), 3), ((255, 255, 255), 1)):
            cv2.putText(t, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.38, col, th)
            cv2.putText(t, f"cam {p} dir {d}", (4, 144), cv2.FONT_HERSHEY_SIMPLEX, 0.32, col, th)
        return t

    def offer_pose(self, side, x, y, z, roll=0.0):
        """Joint targets (deg) that put the tip at (x, y, z) m pointing horizontally away from the base (toward a person in
        front), jaws turned by `roll` deg (0 = jaws close left/right of the approach, 90 = up/down). -> (targets, err m)."""
        import numpy as np, collide
        from kin import K, fk, REST
        t = np.arctan2(y, x); zax = np.array([np.cos(t), np.sin(t), 0.0]); yax = np.array([-np.sin(t), np.cos(t), 0.0])
        T = np.eye(4); T[:3, :3] = np.column_stack([np.cross(yax, zax), yax, zax]); T[:3, 3] = (x, y, z)
        st = self.state()["target"]; q0 = np.array([st[f"{side}_{j}"] for j in collide.J])
        best = None
        for seed in (q0, np.array([np.degrees(t), -40, 10, 0, 0.0]), np.array([np.degrees(t), -10, -20, 20, 0.0]), REST):
            q = K.inverse_kinematics(np.asarray(seed, float), T, 1.0, 1.0, 200)
            e = float(np.linalg.norm(fk(q) - T[:3, 3])) + 0.02 * float(np.linalg.norm(K.forward_kinematics(q)[:3, 2] - zax))
            if best is None or e < best[1]:
                best = (q, e)
        q = best[0].copy(); q[4] += roll
        return {f"{side}_{j}": round(float(v), 2) for j, v in zip(collide.J, q)}, round(best[1], 4)

    def offer(self, side, x, y, z, roll=0.0, open_pct=45, timeout=90.0):
        """Human hand-over: move the gripper to (x, y, z) m (this arm's base frame), pointing horizontally at the person,
        jaws open, then wait until something is placed between the jaws and stays still, close, and check HOLDING.
        A false trigger (closed on nothing) reopens and waits again. -> {"holding": bool, "tries": n, "waited_s": t}."""
        import numpy as np, cv2
        targets, err = self.offer_pose(side, x, y, z, roll)
        if err > 0.03:
            raise RobotError(f"offer pose unreachable (off by {err * 100:.1f} cm): pick a point closer to the arm")
        targets[f"{side}_gripper"] = open_pct
        self.set(targets)
        t0, tries = time.time(), 0

        def roi():
            g = cv2.cvtColor(self.thumb(side, 160, 90), cv2.COLOR_BGR2GRAY).astype(np.float32)
            return cv2.GaussianBlur(g[40:90, 60:130], (5, 5), 0)     # the jaw gap (object close = big change; background = small)
        while time.time() - t0 < timeout:
            time.sleep(0.5); base = roi(); prev, still, entered = base, 0, False
            while time.time() - t0 < timeout:
                time.sleep(0.12); cur = roi()
                d_base, d_prev = float(np.abs(cur - base).mean()), float(np.abs(cur - prev).mean())
                prev = cur
                entered = entered or d_base > 25
                still = still + 1 if entered and d_base > 18 and d_prev < 9 else 0   # a hand still holding it jitters
                if still >= 5:                     # something new between the jaws, held about still ~0.6 s
                    break
            else:
                break
            tries += 1
            self.set({f"{side}_gripper": 0}, wait=False)
            g = [self.state()["measured"][f"{side}_gripper"]]
            for _ in range(25):                   # until the jaws stop
                time.sleep(0.1); g.append(self.state()["measured"][f"{side}_gripper"])
                if len(g) > 4 and max(g[-4:]) - min(g[-4:]) < 0.5:
                    break
            seen = float(np.abs(roi() - base).mean())
            if g[-1] >= 3.5 or seen > 18:          # thin things stop low: the object still in view counts too
                return {"holding": True, "tries": tries, "gripper": round(g[-1], 1), "waited_s": round(time.time() - t0, 1)}
            self.set({f"{side}_gripper": open_pct})   # closed on nothing: reopen, wait again
        return {"holding": False, "tries": tries, "waited_s": round(time.time() - t0, 1)}

    def save(self, name):
        self.call(f"save {name}")

    def recall(self, name, side=None, wait=True):
        return self._motion(f"recall {name}" + (f" {side}" if side else ""), wait)

    def poses(self):
        return self.call("poses")["poses"]

    def shutdown(self, here=False):
        """Release torque (after going home, or right here) and stop robotd."""
        self.call("shutdown here" if here else "shutdown")


class Arm:
    def __init__(self, robot, side):
        self.robot, self.side = robot, side

    def _key(self, name):
        return f"{self.side}_{joint_name(name)}"

    def pose(self, which="measured"):
        st = self.robot.state()[which]
        return {j: st[f"{self.side}_{j}"] for j in JOINTS}

    def tip(self):
        """Gripper tip xyz (m) in this arm's base frame, from the measured pose."""
        import sys, numpy as np
        sys.path.insert(0, HERE)
        from kin import fk
        p = self.pose()
        return fk(np.array([p[j] for j in JOINTS[:5]])).tolist()

    def move(self, wait=True, **deltas):
        """Relative move, e.g. arm.move(elbow=-5, wrist=3)."""
        st = self.robot.state()["target"]
        return self.robot.set({self._key(j): st[self._key(j)] + d for j, d in deltas.items()}, wait)

    def set(self, wait=True, **targets):
        """Absolute targets, e.g. arm.set(lift=-40, elbow=60)."""
        return self.robot.set({self._key(j): v for j, v in targets.items()}, wait)

    def grip(self, percent, wait=True):
        return self.robot._motion(f"grip {self.side} {percent}", wait)

    def open(self, percent=50, wait=True):
        return self.grip(percent, wait)

    def close(self, wait=True):
        return self.grip(3, wait)

    def ik(self, x, y, z):
        """Dry run: joint targets (deg) that put the tip at (x, y, z) m pointing down, starting from the
        measured pose, plus the residual error (m) and the largest joint change. Moves nothing."""
        import sys, numpy as np
        sys.path.insert(0, HERE)
        from kin import ik, REST
        p = self.pose(); q0 = np.array([p[j] for j in JOINTS[:5]])
        best = []                                  # several seeds: the measured pose alone can diverge
        for seed in (q0, REST, np.array([0, -40, 40, 70, 0.0]), np.array([0, -10, 0, 80, 0.0])):
            q, err = ik(seed, [x, y, z])
            best.append((round(float(err), 3), float(np.abs(q - q0).max()), q, err))
        _, _, q, err = min(best, key=lambda t: t[:2])
        return {"targets": {j: round(float(v), 1) for j, v in zip(JOINTS[:5], q)}, "error_m": round(float(err), 4),
                "max_change_deg": round(float(max(abs(v - p[j]) for j, v in zip(JOINTS[:5], q))), 1)}

    def nudge(self, dx=0.0, dy=0.0, dz=0.0, wait=True, max_deg=5.0, max_cm=3.0):
        """Move the tip by (dx, dy, dz) cm in the arm base frame (x forward, y arm's left, z up), keeping the
        gripper's current orientation, in straight-line sub-steps of <= max_deg per joint (at most max_cm total).
        Returns the total joint change (deg)."""
        import sys, numpy as np
        sys.path.insert(0, HERE)
        from kin import K, fk
        d = np.array([dx, dy, dz], float) / 100
        if np.linalg.norm(d) > max_cm / 100:
            raise RobotError(f"nudge is limited to {max_cm} cm; split it")
        p = self.robot.state()["target"]
        q0 = q = np.array([p[self._key(j)] for j in JOINTS[:5]])
        T0, qs = K.forward_kinematics(q0), []
        for f in np.linspace(0, 1, 11)[1:]:      # 10 waypoints along the line, solved incrementally
            T = T0.copy(); T[:3, 3] += f * d
            a = np.arctan2(T[1, 3], T[0, 3]) - np.arctan2(T0[1, 3], T0[0, 3])   # sideways = pan: heading turns along
            T[:3, :3] = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]]) @ T0[:3, :3]
            q = K.inverse_kinematics(q, T, 1.0, 0.1, 60); q[4] = q0[4]   # never roll
            if np.linalg.norm(fk(q) - T[:3, 3]) > 0.005:
                raise RobotError(f"nudge unreachable from here (off by {np.linalg.norm(fk(q) - T[:3, 3]) * 100:.1f} cm)")
            qs.append(q.copy())
        if np.abs(qs[-1] - q0).max() > 6 * max_deg:
            raise RobotError("nudge needs a big joint swing here (near a singularity?): use joint moves")
        last = q0                                 # send only waypoints that are max_deg apart, plus the end
        for i, q in enumerate(qs):
            if np.abs(q - last).max() >= max_deg or i == len(qs) - 1:
                self.robot.set({self._key(j): round(float(v), 2) for j, v in zip(JOINTS[:5], q)}, wait or i < len(qs) - 1)
                last = q
        return {j: round(float(v - w), 1) for j, v, w in zip(JOINTS[:5], qs[-1], q0) if abs(v - w) >= 0.1}

    def goto(self, x, y, z, wait=True):
        """Straight Cartesian line to (x, y, z) m in this arm's base frame, gripper pointing down."""
        return self.robot._motion(f"goto {self.side} {x} {y} {z}", wait)

    def recall(self, name, wait=True):
        return self.robot.recall(name, self.side, wait)

    def torque(self, on):
        self.robot.torque(on, self.side)

    def holding(self):
        """True if the gripper is closed on something (stopped >= 5 short of a target <= 10)."""
        st = self.robot.state()
        return holding(st["target"][f"{self.side}_gripper"], st["measured"][f"{self.side}_gripper"])

    def snap(self, path=None):
        return self.robot.snap(self.side, path)

    def frame(self):
        return self.robot.frame(self.side)

    def thumb(self, w, h):
        return self.robot.thumb(self.side, w, h)

    def __repr__(self):
        return f"Arm({self.side}, {self.pose()})"
