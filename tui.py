"""Terminal panel for robotd (live, no lag). Shared via quack: session so101-tui.

Keys: up/down pick joint · left/right move it (hold to keep moving) · 1/2/3 step 1/5/10 deg
      o/c open/close gripper of the selected side · space STOP · s snap · h home
      p save pose (asks name) · r recall pose (asks name) · q quit panel (robot keeps torque)
      v camera view on/off (live, shared with everyone in the quack session)
      t torque off for the selected arm (asks y; HOLD IT, it goes limp) · T torque on (holds where it is)
Columns: target (where it is told to go) and measured. A big gap between them = joint is blocked.
"""
import curses, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from so101 import Robot

JOINTS = [f"{s}_{j}" for s in ("left", "right")
          for j in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")]


def put(scr, y, text, attr=0):
    h, w = scr.getmaxyx()
    if y < h - 1:
        try:
            scr.addstr(y, 0, text[: w - 1], attr)
        except curses.error:
            pass


RAMP = " .:-=+*#%@"


def init_colors():
    """Pair i = background colour i (xterm-256). Returns True if colour video is possible."""
    try:
        curses.start_color(); curses.use_default_colors()
        if curses.COLORS < 256 or curses.COLOR_PAIRS < 256:
            return False
        for i in range(16, 256):
            curses.init_pair(i, -1, i)
        return True
    except curses.error:
        return False


def draw_cam(scr, r, y0, x0, w, h, color):
    """Camera frame, one pixel per cell; cells are ~2x taller than wide, so sample 16:9 at w x h with w ~ 3.5 h."""
    w, h = min(w, int(h * 3.55)), min(h, int(w / 3.55))
    if w < 8 or h < 3:
        return
    img = r.thumb(w, h)
    for y in range(h):
        for x in range(w):
            b, g, rr = (int(v) for v in img[y, x])
            try:
                if color:
                    c = 16 + 36 * round(rr / 51) + 6 * round(g / 51) + round(b / 51)
                    scr.addstr(y0 + y, x0 + x, " ", curses.color_pair(c))
                else:
                    scr.addstr(y0 + y, x0 + x, RAMP[(rr * 3 + g * 6 + b) // 10 * (len(RAMP) - 1) // 255])
            except curses.error:
                pass


def ask(scr, prompt):
    curses.echo(); scr.timeout(-1)
    h, _ = scr.getmaxyx()
    put(scr, h - 2, prompt)
    try:
        s = scr.getstr(h - 2, len(prompt), 30).decode().strip()
    finally:
        curses.noecho(); scr.timeout(100)
    return s


def main(scr):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.timeout(100)
    sel, step, status, r, cam = 0, 5, "ready", None, True
    color = init_colors()
    while True:
        try:
            r = r or Robot()
            st = r.state()
        except Exception as e:
            r, st = None, None
            status = f"robotd not reachable ({e}); retrying"
        scr.erase()
        put(scr, 0, "SO-101 panel  ↑↓ joint  ←→ move  1/2/3 step  o/c grip  SPACE stop  s snap  h home  p/r save/recall  t/T torque  v cam  q quit", curses.A_BOLD)
        info = (f"  loop {st['hz']} Hz  vel {st['vel']}°/s  P {st['p_gain']}  "
            + "  ".join(f"{s} torque {'on' if v else 'OFF (limp)'}" for s, v in st["torque"].items())) if st else ""
        put(scr, 1, f"step {step}°{info}   {status}")
        put(scr, 2, f"  {'joint':22s} {'target':>8s} {'measured':>9s}", curses.A_DIM)
        for i, j in enumerate(JOINTS):
            if st:
                t, m = st["target"][j], st["measured"][j]
                gap = "  <- blocked?" if abs(t - m) > 3 else ""
                txt = f"{'>' if i == sel else ' '} {j:22s} {t:8.1f} {m:9.1f}{gap}"
            else:
                txt = f"  {j:22s}        ?         ?"
            put(scr, 3 + i, txt, curses.A_REVERSE if i == sel else 0)
        if cam and st:
            H, W = scr.getmaxyx()
            try:
                if W - 50 >= 30:      # beside the table
                    draw_cam(scr, r, 2, 48, W - 49, H - 3, color)
                else:                 # below it
                    draw_cam(scr, r, 3 + len(JOINTS) + 1, 0, W - 1, H - len(JOINTS) - 6, color)
            except Exception as e:
                put(scr, H - 1, f"camera: {e}")
        scr.refresh()
        ch = scr.getch()
        if ch == -1:
            continue
        side = JOINTS[sel].split("_")[0]
        try:
            if ch == ord("q"):
                return
            elif ch == curses.KEY_UP:
                sel = (sel - 1) % len(JOINTS)
            elif ch == curses.KEY_DOWN:
                sel = (sel + 1) % len(JOINTS)
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and r:
                d = step if ch == curses.KEY_RIGHT else -step
                j = JOINTS[sel].split('_', 1)[1]; getattr(r, side).move(wait=False, **{j: d}); status = f"{JOINTS[sel]} {d:+}"
            elif ch in (ord("1"), ord("2"), ord("3")):
                step = {ord("1"): 1, ord("2"): 5, ord("3"): 10}[ch]
            elif ch == ord(" ") and r:
                r.stop(); status = "STOPPED (target = measured)"
            elif ch == ord("o") and r:
                getattr(r, side).open(wait=False); status = f"open {side}"
            elif ch == ord("c") and r:
                getattr(r, side).close(wait=False); status = f"close {side}"
            elif ch == ord("s") and r:
                status = f"snap {r.snap()}"
            elif ch == ord("h") and r:
                r.home(wait=False); status = "going home"
            elif ch == ord("v"):
                cam = not cam; status = f"camera {'on' if cam else 'off'}"
            elif ch == ord("t") and r:
                if ask(scr, f"release {side.upper()} torque? arm goes limp, hold it (y/N): ").lower() == "y":
                    getattr(r, side).torque(False); status = f"{side} torque OFF: move it by hand, T to hold"
                else:
                    status = "torque unchanged"
            elif ch == ord("T") and r:
                getattr(r, side).torque(True); status = f"{side} torque ON, holding current pose"
            elif ch == ord("p") and r:
                name = ask(scr, "save pose as: ")
                if name:
                    r.save(name); status = f"saved {name}"
            elif ch == ord("r") and r:
                names = r.poses()
                name = ask(scr, f"recall ({', '.join(names)}): ")
                if name:
                    r.recall(name, wait=False); status = f"recalling {name}"
        except Exception as e:
            r, status = None, f"error: {e}"


curses.wrapper(main)
