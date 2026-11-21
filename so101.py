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
    bot.snap()                        # both wrist cameras -> {'left': 'snap_left.jpg', 'right': ...}
    bot.left.frame()                  # newest left wrist-camera frame as numpy BGR; bot.left.snap() -> path
    bot.left.thumb(80, 45)            # small live frame (what the panel's camera view uses)
    bot.save("ready"); bot.recall("ready", side="left")
    bot.stop(); bot.home()
    bot.left.torque(False)            # limp for hand-guiding (hold it!); bot.left.torque(True) holds the new pose

Every motion method waits for the motion to finish unless wait=False. Joint aliases:
pan, lift, elbow, wrist, roll, gripper (full names like elbow_flex also work).
"""
import json, os, socket, time

HERE = os.path.dirname(os.path.abspath(__file__))
SOCK = os.environ.get("SO101_SOCK") or os.path.join(HERE, "sim.sock" if os.environ.get("SO101_SIM") == "1" else "robot.sock")
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
ALIAS = {"pan": "shoulder_pan", "lift": "shoulder_lift", "shoulder": "shoulder_lift", "elbow": "elbow_flex",
         "wrist": "wrist_flex", "flex": "wrist_flex", "roll": "wrist_roll", "grip": "gripper"}


class RobotError(RuntimeError):
    pass


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
            raise RobotError("robotd is not running: systemctl --user start robotd") from e
        self._f = self._s.makefile("rw")
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
        """Write the newest wrist-camera frames. side=None: both -> {side: path}; side given -> path."""
        if side is None:
            return self.call("snap")["paths"]
        return self.call(f"snap {side}" + (f" {path}" if path else ""))["paths"][side]

    def frame(self, side):
        import cv2
        return cv2.imread(self.snap(side))

    def thumb(self, side, w, h):
        """Newest frame of one wrist camera resized to w x h, as numpy BGR (cheap; for live previews)."""
        import base64, numpy as np
        r = self.call(f"thumb {side} {w} {h}")
        return np.frombuffer(base64.b64decode(r["bgr"]), np.uint8).reshape(r["h"], r["w"], 3)

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

    def goto(self, x, y, z, wait=True):
        """Straight Cartesian line to (x, y, z) m in this arm's base frame, gripper pointing down."""
        return self.robot._motion(f"goto {self.side} {x} {y} {z}", wait)

    def recall(self, name, wait=True):
        return self.robot.recall(name, self.side, wait)

    def torque(self, on):
        self.robot.torque(on, self.side)

    def snap(self, path=None):
        return self.robot.snap(self.side, path)

    def frame(self):
        return self.robot.frame(self.side)

    def thumb(self, w, h):
        return self.robot.thumb(self.side, w, h)

    def __repr__(self):
        return f"Arm({self.side}, {self.pose()})"
