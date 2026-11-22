"""robot calibrate: measure where the scene camera is, using the arms as the ruler (no LLM, ~3 min).

Each arm in turn visits known tip points (gripper down, two heights); at each one the gripper opens and closes (the frame
difference says where the jaw is) and GPT-6.1 terra points at the closed jaw tip in a 2x crop of the full-res frame
(~20 paid image calls per run). A pinhole fit (cv2.solvePnP, focal searched) of tip xyz <-> pixel
gives the camera pose, alternating with a fit of the right arm's base (x, y, yaw) from its own points. Two points per arm
are held out: scene.json (scene_camera + right_base_pose) is rewritten when they land closer than with the current one
(guards against a bad run, e.g. someone walking through). snap_calibrate.jpg shows every detection, .json the points."""
import json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCENE = os.path.join(HERE, "scene.json")
W, H = 800, 450
FIT = [(x, y, z) for z in (0.16, 0.07) for x in (0.17, 0.26) for y in (-0.05, 0.05)]   # per arm, own base frame
HOLD = [(0.22, 0.0, 0.11), (0.24, 0.03, 0.08)]                                         # held out: the check


def cam_now():
    return json.load(open(SCENE))["scene_camera"]


def project(c, P):
    """World points (N,3) -> pixels (N,2) with a scene_camera dict (the same model as Robot.locate)."""
    f, up = np.array(c["forward"]), np.array(c["up"]); r = np.cross(f, up)
    d = np.asarray(P, float) - np.array(c["position"]); zc = d @ f
    return np.c_[W / 2 + c["focal_px"] * (d @ r) / zc, H / 2 - c["focal_px"] * (d @ up) / zc]


def fit(P, uv, guess):
    """Camera pose + focal from >= 6 world points and their pixels -> (scene_camera dict, rms px)."""
    import cv2
    P, uv = np.asarray(P, np.float64), np.asarray(uv, np.float64)
    f0, up0 = np.array(guess["forward"]), np.array(guess["up"])
    R0 = np.array([np.cross(f0, up0), -up0, f0])                     # rows: camera x (right), y (down), z (forward)
    rv0, tv0 = cv2.Rodrigues(R0)[0], -R0 @ np.array(guess["position"])
    best = None
    for fp in range(450, 951, 5):
        K = np.array([[fp, 0, W / 2], [0, fp, H / 2], [0, 0, 1.0]])
        ok, rv, tv = cv2.solvePnP(P, uv, K, None, rv0.copy(), tv0.copy(), True, cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            continue
        R = cv2.Rodrigues(rv)[0]
        c = {**guess, "position": (-R.T @ tv).ravel().tolist(), "forward": R[2].tolist(), "up": (-R[1]).tolist(),
             "focal_px": float(fp)}
        e = float(np.sqrt(np.mean(np.sum((project(c, P) - uv) ** 2, 1))))
        if best is None or e < best[1]:
            best = (c, e)
    return best


def world(tip, side, base):
    """Tip xyz in its arm's frame -> world (left base frame); base = right base (x, y, yaw deg)."""
    if side == "left":
        return np.asarray(tip, float)
    x, y, yaw = base; a = np.radians(yaw)
    return np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]]) @ tip + [x, y, 0.0]


def fit_base(c, R, base):
    """Right base (x, y, yaw) that puts the right arm's tips R [(tip, uv)] closest to their pixels, camera c fixed."""
    best, step = list(base), [0.02, 0.02, 4.0]
    err = lambda b: sum(np.sum((project(c, [world(t, "right", b)])[0] - uv) ** 2) for t, uv in R)
    for _ in range(40):                                             # coordinate search, halving the step
        moved = False
        for i in range(3):
            for sgn in (1, -1):
                b = list(best); b[i] += sgn * step[i]
                if err(b) < err(best):
                    best, moved = b, True
        if not moved:
            step = [x / 2 for x in step]
    return best


def fit_all(L, R, cam, base):
    """Camera + right base from L / R = [(tip in own arm frame, uv)]: alternate the two fits."""
    for _ in range(4):
        cam, rms = fit([t for t, _ in L] + [world(t, "right", base) for t, _ in R], [uv for _, uv in L + R], cam)
        base = fit_base(cam, R, base)
    cam, rms = fit([t for t, _ in L] + [world(t, "right", base) for t, _ in R], [uv for _, uv in L + R], cam)
    return cam, base, rms


def grab(bot, n=3):
    time.sleep(0.6)                                                 # camera latency after the move settled
    fs = []
    for _ in range(n):
        fs.append(bot.thumb("scene", W, H).astype(np.int16)); time.sleep(0.12)
    return np.median(fs, 0)


def detect(bot, arm, guess_uv, down):
    """Open and close the jaws; the moving jaw is the only change -> its pixels furthest along `down` (image direction
    from the wrist to the tip) = the tip. None if unclear."""
    import cv2
    arm.grip(0); a = grab(bot)
    arm.grip(45); b = grab(bot)
    arm.grip(0); c = grab(bot)
    d = np.minimum(np.abs(b - a), np.abs(b - c)).max(2).astype(np.uint8)   # changed when opened, back when closed
    d = cv2.GaussianBlur(d, (5, 5), 0)
    m = cv2.morphologyEx((d > 28).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    yy, xx = np.mgrid[:H, :W]                                       # only near where the current calibration puts it
    m[(xx - guess_uv[0]) ** 2 + (yy - guess_uv[1]) ** 2 > 170 ** 2] = 0
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    if n < 2:
        return None, m
    k = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    if st[k, cv2.CC_STAT_AREA] < 40:
        return None, m
    vs, us = np.nonzero(lab == k)
    a = us * down[0] + vs * down[1]; far = a >= a.max() - 4
    return (float(us[far].mean()), float(vs[far].mean())), m


MODEL = os.environ.get("ALONG_CALIB_MODEL", "gpt-6.1-terra")
POINT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["visible", "x", "y"],
                "properties": {"visible": {"type": "boolean"}, "x": {"type": "number"}, "y": {"type": "number"}}}


def point(side, full, ctr):
    """Ask the model for the closed jaw tip in a 2x crop (full-res frame, 400x225 tile px around ctr)
    -> (tile (u, v) or None, the crop with the answer drawn: debug view)."""
    import cv2, llm
    fh, fw = full.shape[:2]; sx, sy = fw / W, fh / H
    x0 = int(min(max(ctr[0] - W / 4, 0), W / 2)); y0 = int(min(max(ctr[1] - H / 4, 0), H / 2))
    crop = cv2.resize(full[int(y0 * sy):int((y0 + H / 2) * sy), int(x0 * sx):int((x0 + W / 2) * sx)], (W, H))
    arm = "white" if side == "left" else "orange"
    q = (f"This 800x450 image shows the gripper of the {arm} robot arm, pointing down, jaws closed. Give the pixel of the "
         f"gripper tip: the lowest end of the closed jaws, where the two jaw tips meet. visible = false if that tip is "
         f"hidden or not in the image.")
    try:
        r, _ = llm.call("calibrate", [q, ("image", llm.image_url(crop))], schema=POINT_SCHEMA, model=MODEL, tier="default", effort="low")
    except Exception as e:
        print(f"  {side}: model call failed: {e}"); return None, crop
    ok = r["visible"] and 0 <= r["x"] <= W and 0 <= r["y"] <= H
    if ok:
        cv2.circle(crop, (round(r["x"]), round(r["y"])), 10, (0, 255, 0), 2)
        cv2.drawMarker(crop, (round(r["x"]), round(r["y"])), (0, 255, 0), cv2.MARKER_CROSS, 40, 1)
    cv2.putText(crop, f"{side} {MODEL}: " + (f"({r['x']:.0f}, {r['y']:.0f})" if ok else "not visible"), (12, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0) if ok else (0, 0, 255), 2)
    return ((x0 + r["x"] / 2, y0 + r["y"] / 2) if ok else None), crop


def run(bot):
    import cv2, llm
    from along import env
    llm.SPEND, llm.API_KEY = True, env("OPENAI_API_KEY")          # a paid run: ~20 terra image calls
    if not llm.API_KEY:
        sys.exit("OPENAI_API_KEY missing (.env): robot calibrate asks the model for the jaw tips")
    old = cam_now()
    rb = json.load(open(SCENE))["right_base_pose"]
    base0 = [rb["position"][0], rb["position"][1], rb.get("yaw_deg", 0.0)]
    pts, sheet, shots = [], [], []                                   # pts: (side, held_out, tip xyz in its arm frame, uv)
    bot.recall("rest")
    for side in ("left", "right"):
        arm = getattr(bot, side)
        print(f"{side} arm ...", flush=True)
        bot.recall("ready", side)
        for held, p in [(False, p) for p in FIT] + [(True, p) for p in HOLD]:
            try:
                arm.goto(*p)
            except Exception as e:
                print(f"  {p}: skipped ({e})"); continue
            tip = np.array(arm.tip())
            if np.linalg.norm(tip - p) > 0.015:
                print(f"  {p}: skipped (tip ended at {np.round(tip, 3).tolist()})"); continue
            g, top = project(old, [world(tip, side, base0), world(tip, side, base0) + [0, 0, 0.1]])
            down = (g - top) / (np.linalg.norm(g - top) + 1e-9)
            uv, m = detect(bot, arm, g, down)                       # the diff only says where to look
            ys, xs = np.nonzero(m)
            ctr = (xs.mean(), ys.mean()) if len(xs) else g
            shots.append((side, held, tip.tolist(), ctr, bot.frame("scene"), bot.thumb("scene", W, H).copy()))
            print(f"  {p}{' (check)' if held else ''}: shot", flush=True)
        arm.grip(0); bot.recall("ready", side); bot.recall("rest", side)
    print(f"asking {MODEL} for the jaw tip in {len(shots)} images ...", flush=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:                               # terra points (the diff alone was wrong on half)
        found = list(ex.map(lambda sh: point(sh[0], sh[4], sh[3]), shots))
    dbg = os.path.join(HERE, "snap_calibrate"); os.makedirs(dbg, exist_ok=True)
    for f in os.listdir(dbg):
        os.remove(os.path.join(dbg, f))
    for i, ((side, held, tip, ctr, _, img), (uv, crop)) in enumerate(zip(shots, found)):
        cv2.imwrite(f"{dbg}/{i:02d}_{side}{'_check' if held else ''}.jpg", crop)   # debug view: what the model saw + said
        if uv:
            pts.append((side, held, tip, uv)); cv2.circle(img, (round(uv[0]), round(uv[1])), 5, (0, 255, 0), 2)
        cv2.rectangle(img, (int(ctr[0] - W / 4), int(ctr[1] - H / 4)), (int(ctr[0] + W / 4), int(ctr[1] + H / 4)), (255, 0, 255), 1)
        sheet.append(cv2.resize(img, (400, 225)))
    print(f"  {len(pts)} of {len(shots)} tips found (debug view: {dbg}/)")
    rows = [np.hstack(sheet[i:i + 4] + [np.zeros((225, 400, 3), np.uint8)] * (4 - len(sheet[i:i + 4])))
            for i in range(0, len(sheet), 4)]
    out = os.path.join(HERE, "snap_calibrate.jpg")
    if rows:
        cv2.imwrite(out, np.vstack(rows))
    json.dump(pts, open(os.path.join(HERE, "snap_calibrate.json"), "w"))
    return solve(pts, old, base0, out)


def solve(pts, old, base0, out="", write=True):
    """Fit on the non-held-out points, compare with the current calibration on the held-out ones, write if better."""
    pick = lambda side, held: [(np.array(t), np.array(uv)) for sd, h, t, uv in pts if sd == side and h == held]
    L, R, Lc, Rc = pick("left", False), pick("right", False), pick("left", True), pick("right", True)
    if len(L) + len(R) < 8 or len(R) < 4 or len(Lc) + len(Rc) < 2:
        print(f"only {len(L)}+{len(R)} fit / {len(Lc) + len(Rc)} check points found: scene.json unchanged (see {out})")
        return False
    cam, base, rms = fit_all(L, R, old, base0)
    def err(c, b):
        ws = [world(t, "left", b) for t, _ in Lc] + [world(t, "right", b) for t, _ in Rc]
        e = np.linalg.norm(project(c, ws) - np.array([uv for _, uv in Lc + Rc]), axis=1)
        return e.mean(), (e * np.linalg.norm(np.array(ws) - c["position"], axis=1) / c["focal_px"] * 100).mean()
    (pn, cn), (po, co) = err(cam, base), err(old, base0)
    print(f"fit: {len(L)}+{len(R)} points, rms {rms:.1f} px, focal {cam['focal_px']:.0f} px, camera at "
          f"{np.round(cam['position'], 3).tolist()}, right base x {base[0]:.3f} y {base[1]:.3f} yaw {base[2]:.1f}")
    print(f"check ({len(Lc) + len(Rc)} held-out points): new {pn:.1f} px (~{cn:.1f} cm), current {po:.1f} px (~{co:.1f} cm)")
    if pn >= po or not write:
        print(f"{'no better than the current calibration' if write else 'look only (--no-model)'}: scene.json unchanged "
              f"(see {out})"); return False
    cam, base, rms = fit_all(L + Lc, R + Rc, cam, base)                  # final: all points
    sc = json.load(open(SCENE)); stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    sc["scene_camera"] = {**sc["scene_camera"], "position": [round(x, 5) for x in cam["position"]],
                          "forward": [round(x, 5) for x in cam["forward"]], "up": [round(x, 5) for x in cam["up"]],
                          "focal_px": round(cam["focal_px"], 1), "position_uncertainty_m": 0.01,
                          "calibrated_at": stamp, "calibrated_by": "robot calibrate", "reprojection_px": round(rms, 1)}
    sc["right_base_pose"] = {**sc["right_base_pose"], "position": [round(base[0], 5), round(base[1], 5), 0.0],
                             "yaw_deg": round(base[2], 2), "horizontal_uncertainty_m": 0.01, "yaw_uncertainty_deg": 2,
                             "calibrated_at": stamp, "calibrated_by": "robot calibrate"}
    json.dump(sc, open(SCENE, "w"), indent=2)
    print(f"scene.json updated (all points: rms {rms:.1f} px); detections in {out}")
    return True
