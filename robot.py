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
  robot snap [left|right] [path]         wrist-camera frames (default both: snap_left.jpg, snap_right.jpg)
  robot stop | home | limits | speed 30
  robot raw "move left_elbow_flex -5; wait; snap"   raw robotd protocol, ';' chains, wait = until settled
  robot shutdown [here]                  release torque after going home (or right here)

Joints: pan lift elbow wrist roll gripper. Sides: left/l (white), right/r (orange).
Flags: --json machine-readable output, --no-wait return before the motion finishes.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from so101 import Robot, RobotError, JOINTS

SIDE = {"l": "left", "left": "left", "r": "right", "right": "right"}


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


def main(argv):
    as_json, wait = "--json" in argv, "--no-wait" not in argv
    a = [x for x in argv if x not in ("--json", "--no-wait")]
    cmd = a[0] if a else "state"
    if cmd in ("-h", "--help", "help"):
        print(__doc__); return
    bot = Robot()
    off = None
    if cmd == "state":
        st = bot.state(); print(json.dumps(st) if as_json else table(st)); return
    if cmd == "watch":
        try:
            while True:
                print("\033[H\033[J" + table(bot.state()), flush=True); time.sleep(0.2)
        except KeyboardInterrupt:
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
            print(bot.snap(side(a), a[2] if len(a) > 2 else None))
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
