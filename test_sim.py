"""End-to-end test of robotd + so101 + safety rules on the SIM (no hardware moves).
    SO101_SIM=1 .venv/bin/python robotd.py &      # sim daemon on ./sim.sock
    SO101_SIM=1 .venv/bin/python test_sim.py
"""
import os; os.environ["SO101_NOLOG"] = "1"
import os, sys, time, subprocess, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
assert os.environ.get("SO101_SIM") == "1", "run with SO101_SIM=1 (never against the real arms)"
from so101 import Robot, RobotError, holding
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
paths = bot.snap()
check("snap writes all three cameras", set(paths) == {"left", "right", "scene"} and all(os.path.exists(p) for p in paths.values()))
check("cams reports all ok", all(c["ok"] for c in bot.cams().values()) and len(bot.cams()) == 3)
lt, rt = L.thumb(64, 18), R.thumb(64, 18)
check("per-arm thumbs differ", lt.shape == (18, 64, 3) and not (lt == rt).all())
try: bot.call("thumb middle 10 10"); check("rejects unknown camera", False)
except RobotError: check("rejects unknown camera", True)
e0 = bot.state()["measured"]["right_elbow_flex"]
r = bot.step("move right_elbow_flex -5; dance")
import cv2; img = cv2.imread(r["image"])
check("step moves and settles", abs(bot.state()["measured"]["right_elbow_flex"] - e0 + 5) < 1.5 and r["ran"][0][1] != "" and not r["state"]["right"]["off"])
check("step reports errors, still snaps", r["ran"][1][1].startswith("ERROR") and img is not None and img.shape[:2] == (225 + 450, 800) and not r["missing_cams"])
check("step state compact", set(r["state"]) == {"left", "right"} and len(r["state"]["left"]["tip"]) == 3 and r["state"]["right"]["holding"] is False)
L.torque(False); r = bot.step("move left_elbow_flex -5"); L.torque(True)
check("step on limp arm reports, not raises", "torque is OFF" in r["ran"][0][1] and os.path.exists(r["image"]))
check("holding rule", holding(3, 18) and not holding(3, 5) and not holding(50, 60) and not holding(0, 4.9) and not R.holding())
d = R.ik(0.22, 0, 0.10)
check("ik dry run moves nothing", d["error_m"] < 0.005 and abs(R.pose()["shoulder_lift"] - bot.state()["target"]["right_shoulder_lift"]) < 2)
R.set(**{k: v for k, v in d["targets"].items()}); z0 = R.tip()[2]
mv = R.nudge(0, 0, -1); check("nudge moves the tip ~1 cm down", abs(R.tip()[2] - z0 + 0.01) < 0.004 and max(map(abs, mv.values())) <= 5, mv)
try: R.nudge(20, 0, 0); check("nudge refuses big moves", False)
except RobotError as e: check("nudge refuses big moves", "limited" in str(e))
bot.step(); r = bot.step("nudge right 0 0 1"); check("step runs nudge, reports tip delta", abs(r["state"]["right"]["tip_delta"][2] - 0.01) < 0.004, r["ran"])
bot.home()
try: Robot(sock="/nonexistent/x.sock"); check("missing socket message", False)
except RobotError as e: check("missing socket message", "not running" in str(e))
import socket, tempfile; d = tempfile.mkdtemp(); srv = socket.socket(socket.AF_UNIX); srv.bind(f"{d}/s.sock"); srv.listen(1)
os.chmod(f"{d}/s.sock", 0)
try: Robot(sock=f"{d}/s.sock"); check("unopenable socket message", os.geteuid() == 0)
except RobotError as e: check("unopenable socket message", "exists but cannot be opened" in str(e))
srv.close(); os.unlink(f"{d}/s.sock"); os.rmdir(d)
st = bot.state(); check("state has collision info", "near" in st and "blocked" in st and st["collide"]["on"])
r = bot.step("collide off; recall rest; recall ready; collide on")
r = bot.step("set right_shoulder_pan -80")
check("collision refused", isinstance(r["ran"][0][1], str) and "refused" in r["ran"][0][1], r["ran"][0][1])
r = bot.step("handover on; set left_shoulder_pan 0; handover off; collide off; collide on")
check("collide/handover toggles", all(not isinstance(x[1], str) for x in r["ran"]), r["ran"])
bot.step("recall rest")
r = subprocess.run([sys.executable, "robot.py", "step"], capture_output=True, text=True, timeout=30)
check("robot step CLI", r.returncode == 0 and r.stdout.count("\n") == 3 and "snap_all.jpg" in r.stdout, "" if r.returncode == 0 else r.stderr)
print(f"{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
