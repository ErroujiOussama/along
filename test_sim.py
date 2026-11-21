"""End-to-end test of robotd + so101 + safety rules on the SIM (no hardware moves).
    SO101_SIM=1 .venv/bin/python robotd.py &      # sim daemon on ./sim.sock
    SO101_SIM=1 .venv/bin/python test_sim.py
"""
import os, sys, time, subprocess, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
assert os.environ.get("SO101_SIM") == "1", "run with SO101_SIM=1 (never against the real arms)"
from so101 import Robot, RobotError
from kin import ik

bot, res = Robot(), []
def check(name, ok, info=""):
    res.append(ok); print(("PASS " if ok else "FAIL ") + name, info)

check("loop ~50 Hz", 45 < bot.state()["hz"] < 55)
for bad, line in (("unknown cmd", "dance"), ("unknown joint", "move left_knee 5"),
                  ("goto outside box", "goto left 0.9 0 0"), ("bad torque", "torque middle off")):
    try: bot.call(line); check(f"rejects {bad}", False)
    except RobotError: check(f"rejects {bad}", True)
r = subprocess.run([sys.executable, "robotd.py"], capture_output=True, text=True, timeout=30)
check("second instance refused", r.returncode != 0 and "already running" in r.stderr)
L, R = bot.left, bot.right
lift0 = L.pose()["shoulder_lift"]
check("move settles", not L.move(lift=20) and abs(L.pose()["shoulder_lift"] - lift0 - 20) < 1.5)
L.torque(False); check("torque off reported", bot.state()["torque"]["left"] is False)
for name, fn in (("move", lambda: L.move(elbow=-5)), ("set", lambda: L.set(elbow=10)), ("grip", L.open),
                 ("goto", lambda: L.goto(0.2, 0, 0.1)), ("home", bot.home), ("recall", lambda: (bot.save("t"), bot.recall("t")))):
    try: fn(); check(f"limp arm rejects {name}", False)
    except RobotError as e: check(f"limp arm rejects {name}", "torque is OFF" in str(e))
check("other arm moves while left limp", not R.move(lift=10))
L.torque(True); st = bot.state()
check("torque on holds current pose", all(abs(st["target"][k] - st["measured"][k]) < 0.5 for k in st["target"] if k.startswith("left")))
q, _ = ik(np.array([0, -20, 40, 70, 0.0]), np.array([0.22, 0, 0.10]))
L.set(pan=q[0], lift=q[1], elbow=q[2], wrist=q[3], roll=q[4]); bot.save("ready_sim")
L.goto(0.25, 0.03, 0.05)
check("goto reaches target", np.linalg.norm(np.array(L.tip()) - [0.25, 0.03, 0.05]) < 0.01)
L.goto(0.30, 0, 0.12, wait=False); time.sleep(0.2); bot.stop()
check("stop cancels goto plan", bot.state()["plan"] == 0)
check("recall restores", not bot.recall("ready_sim"))
check("home returns to start pose", not bot.home())
print(f"{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
