"""Capsule collision model of both SO-101 arms: own links + wrist camera, the other arm, the table.

World frame = left arm base frame (x forward, y left, z up, m). The right base pose comes from setup.json.
Links are capsules (segment + radius) placed on the URDF joint frames; the wrist camera is a capsule on the
gripper, on the side away from the jaws' opening plane (checked on hardware 2026-11-21: same side on both arms).
Numbers are hand-fitted (the URDF has no meshes): tune RADII / CAPS here, verify with `robot collide`.
"""
import json, os, xml.etree.ElementTree as ET
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
J = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
SETUP = os.path.join(HERE, "setup.json")
DEFAULTS = {"right_base_in_left": [0.0, -0.30, 0.0],   # x, y (m), yaw (deg) of the right base in the left base frame
            "table_z": -0.02,                          # table plane in base frames (m)
            "arm_gap": 0.015,                          # min clearance between the arms (m); 0 during hand-over
            "obstacle_gap": 0.01,                      # min clearance to mapped obstacles (m)
            "near": 0.04}                              # report pairs closer than this (m)

# capsules: name -> (frame index, a, b, radius); frame 0 = base, 1..5 = after each joint in J
CAPS = {
    "base":      (0, (0.0, 0.0, 0.02), (0.0388, 0.0, 0.055), 0.045),
    "shoulder":  (1, (0.0, 0.0, 0.0), (-0.0304, -0.0183, -0.0542), 0.035),
    "upper_arm": (2, (0.0, 0.0, 0.0), (-0.1126, -0.028, 0.0), 0.028),
    "forearm":   (3, (0.0, 0.0, 0.0), (-0.1349, 0.0052, 0.0), 0.025),
    "wrist":     (4, (0.0, 0.0, 0.0), (0.0, -0.0611, 0.0181), 0.025),
    "gripper":   (5, (0.004, 0.0, -0.02), (-0.004, 0.0, -0.085), 0.022),
    "wrist_cam": (5, (0.0, 0.040, -0.015), (0.0, 0.040, -0.050), 0.022),
}
NAMES = list(CAPS)
SELF_PAIRS = [(a, b) for a in ("gripper", "wrist_cam") for b in ("base", "shoulder", "upper_arm")] + [("wrist_cam", "forearm")]
TABLE = ["upper_arm", "forearm", "wrist", "wrist_cam"]    # the jaws may touch the table (grasps); checked separately


def _rot(axis, a):
    c, s = np.cos(a), np.sin(a)
    R = np.zeros(np.shape(a) + (3, 3)); R[..., 0, 0] = 1; R[..., 1, 1] = 1; R[..., 2, 2] = 1
    i, j = {"x": (1, 2), "y": (2, 0), "z": (0, 1)}[axis]
    R[..., i, i] = c; R[..., j, j] = c; R[..., i, j] = -s; R[..., j, i] = s
    return R


def _origin(el):
    o = el.find("origin")
    xyz = [float(v) for v in o.get("xyz").split()]; r, p, y = (float(v) for v in o.get("rpy").split())
    T = np.eye(4); T[:3, :3] = _rot("z", y) @ _rot("y", p) @ _rot("x", r); T[:3, 3] = xyz
    return T


_root = ET.parse(os.path.join(HERE, "so101.urdf")).getroot()
_joints = {j.get("name"): j for j in _root.findall("joint")}
ORIGINS = [_origin(_joints[n]) for n in J]
TIP = _origin(_joints["gripper_frame_joint"])


def frames(q):
    """q (N,5) joint degrees -> (N,6,4,4) frames: base, then after each joint (arm base frame)."""
    q = np.radians(np.atleast_2d(q)); N = len(q)
    T = np.broadcast_to(np.eye(4), (N, 4, 4)).copy(); out = [T]
    for i, O in enumerate(ORIGINS):
        Rz = np.broadcast_to(np.eye(4), (N, 4, 4)).copy(); Rz[:, :3, :3] = _rot("z", q[:, i])
        T = T @ O @ Rz; out.append(T)
    return np.stack(out, 1)


def tip(q):
    return (frames(q)[:, 5] @ TIP)[:, :3, 3]


SCENE = os.path.join(HERE, "scene.json")
NOT_OBSTACLE = ("tabletop", "cable", "lead", "pouch", "zipper", "pen", "card", "coaster", "usb")   # flat or graspable
_cache = {}


def _box_capsule(c, size):
    """Axis-aligned box (link frame) -> capsule along its longest axis that contains it."""
    c, h = np.asarray(c, float), np.asarray(size, float) / 2
    i = int(np.argmax(h)); r = float(np.sort(h)[1] * 1.15)
    d = np.zeros(3); d[i] = max(h[i] - r, 0.0)
    return tuple(c - d), tuple(c + d), r


def setup():
    """DEFAULTS <- scene.json (agent calibration, prompts/calibrate.md) <- setup.json (manual overrides). Cached by mtime."""
    key = tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in (SCENE, SETUP))
    if _cache.get("key") == key:
        return dict(_cache["s"])
    s = {**DEFAULTS, "mounted": {}, "obstacles": [], "source": "defaults"}
    if os.path.exists(SCENE):
        sc = json.load(open(SCENE))
        rb = sc["right_base_pose"]
        s.update(right_base_in_left=[rb["position"][0], rb["position"][1], rb.get("yaw_deg", 0.0)],
                 table_z=sc.get("table_z", s["table_z"]), source=f"scene.json {sc.get('captured_at', '')}")
        for side, arm in sc.get("arms", {}).items():
            for l in arm.get("links", []):
                if "camera" in l["name"] and l.get("shape") == "box":
                    s["mounted"][side] = (l.get("frame", 5),) + _box_capsule(l["center"], l["size"])
        for o in sc.get("objects", []):
            n = o["name"].lower()
            if any(w in n for w in NOT_OBSTACLE):
                continue
            if o["shape"] == "box":
                top = o["center"][2] + o["size"][2] / 2
                if top > s["table_z"] + 0.02:
                    s["obstacles"].append({"name": o["name"], "box": (o["center"], o["size"], o.get("yaw_deg", 0.0))})
            elif o["shape"] == "capsule" and max(o["a"][2], o["b"][2]) + o["radius"] > s["table_z"] + 0.02:
                s["obstacles"].append({"name": o["name"], "cap": (o["a"], o["b"], o["radius"])})
    if os.path.exists(SETUP):
        s.update(json.load(open(SETUP)))
    _cache.update(key=key, s=s)
    return dict(s)


def base_T(side, s):
    if side == "left":
        return np.eye(4)
    x, y, yaw = s["right_base_in_left"]
    T = np.eye(4); T[:3, :3] = _rot("z", np.radians(yaw)); T[:3, 3] = (x, y, 0.0)
    return T


def capsules(q, side, s):
    """q (N,5) -> dict name -> (a (N,3), b (N,3), r) in the world frame."""
    F = base_T(side, s) @ frames(q)
    out, caps = {}, dict(CAPS)
    if side in s.get("mounted", {}):
        caps["wrist_cam"] = s["mounted"][side]          # calibrated mounted camera replaces the default guess
    for n, (f, a, b, r) in caps.items():
        out[n] = ((F[:, f] @ np.r_[a, 1.0])[:, :3], (F[:, f] @ np.r_[b, 1.0])[:, :3], r)
    return out


def box_dist(a, b, center, size, yaw, k=7):
    """Approx. distance from segment a-b (N,3) to a box (yaw about z): min over k points on the segment."""
    t = np.linspace(0, 1, k)[None, :, None]
    p = a[:, None] + (b - a)[:, None] * t - np.asarray(center, float)
    c, sn = np.cos(np.radians(yaw)), np.sin(np.radians(yaw))
    p = np.stack([c * p[..., 0] + sn * p[..., 1], -sn * p[..., 0] + c * p[..., 1], p[..., 2]], -1)
    q = np.abs(p) - np.asarray(size, float) / 2
    d = np.linalg.norm(np.maximum(q, 0), axis=-1) + np.minimum(q.max(-1), 0)
    return d.min(1)


def seg_dist(p1, q1, p2, q2):
    """Distance between segments p1-q1 and p2-q2, vectorized over leading dims."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e = (d1 * d1).sum(-1), (d2 * d2).sum(-1)
    b, c, f = (d1 * d2).sum(-1), (d1 * r).sum(-1), (d2 * r).sum(-1)
    den = a * e - b * b
    s = np.where(den > 1e-12, np.clip((b * f - c * e) / np.where(den > 1e-12, den, 1), 0, 1), 0.0)
    t = (b * s + f) / e
    s = np.where(t < 0, np.clip(-c / a, 0, 1), np.where(t > 1, np.clip((b - c) / a, 0, 1), s))
    t = np.clip(t, 0, 1)
    return np.linalg.norm(p1 + d1 * s[..., None] - p2 - d2 * t[..., None], axis=-1)


def clearances(qL, qR, s=None):
    """qL, qR (N,5) -> {pair label: (N,) clearance m, threshold m}. Negative clearance = overlap."""
    s = s or setup()
    C = {"left": capsules(qL, "left", s), "right": capsules(qR, "right", s)}
    out = {}
    for side, c in C.items():
        for a, b in SELF_PAIRS:
            out[f"{side} {a} ↔ {side} {b}"] = (seg_dist(c[a][0], c[a][1], c[b][0], c[b][1]) - c[a][2] - c[b][2], 0.0)
        for n in TABLE:
            out[f"{side} {n} ↔ table"] = (np.minimum(c[n][0][:, 2], c[n][1][:, 2]) - c[n][2] - s["table_z"], 0.0)
        tp = tip(qL if side == "left" else qR)[:, 2]
        out[f"{side} jaw tips ↔ table"] = (tp - s["table_z"] + 0.01, 0.0)      # tips may go 1 cm below the estimate
        for o in s.get("obstacles", []):
            for n in ("upper_arm", "forearm", "wrist", "gripper", "wrist_cam"):
                a, b, r = c[n]
                if "box" in o:
                    d = box_dist(a, b, *o["box"]) - r
                else:
                    oa, ob, orad = o["cap"]
                    d = seg_dist(a, b, np.broadcast_to(np.asarray(oa, float), a.shape), np.broadcast_to(np.asarray(ob, float), a.shape)) - r - orad
                out[f"{side} {n} ↔ {o['name']}"] = (d, s["obstacle_gap"])
    L, R = C["left"], C["right"]
    for a in NAMES:
        for b in NAMES:
            if a == "base" and b == "base":
                continue
            out[f"left {a} ↔ right {b}"] = (seg_dist(L[a][0], L[a][1], R[b][0], R[b][1]) - L[a][2] - R[b][2], s["arm_gap"])
    return out


def ramp_path(start, goals, step=2.0):
    """Joint paths robotd's ramp takes: every joint moves at the same speed toward each goal in turn.
    start: {key: deg}; goals: list of {key: deg} (later ones keep earlier values). -> list of {key: deg} samples."""
    cur, pts = dict(start), [dict(start)]
    for g in goals:
        g = {**cur, **g}
        span = max([abs(g[k] - cur[k]) for k in g] + [0.0])
        for x in np.linspace(step, span, max(int(span / step), 1)) if span > 0 else []:
            pts.append({k: cur[k] + float(np.clip(g[k] - cur[k], -x, x)) for k in cur})
        cur = g
    return pts


def _arr(pts, side):
    return np.array([[p[f"{side}_{j}"] for j in J] for p in pts])


def check(start, goals, s=None):
    """None if the move is fine, else a refusal string. A move may start in an overlap but must not make it worse."""
    s = s or setup()
    arm = lambda d: {k: v for k, v in d.items() if not k.endswith("gripper")}
    pts = ramp_path(arm(start), [arm(g) for g in goals])
    cl = clearances(_arr(pts, "left"), _arr(pts, "right"), s)
    worst = None
    for label, (d, thr) in cl.items():
        i = int(np.argmin(d))
        if d[i] < thr and d[i] < d[0] - 0.002 and (worst is None or d[i] - thr < worst[1] - worst[2]):
            worst = (label, float(d[i]), thr, i / max(len(d) - 1, 1))
    if worst:
        label, d, thr, frac = worst
        return (f"refused: {label} would come to {d * 100:+.1f} cm (min {thr * 100:.1f}) "
                f"{'at the end' if frac > 0.95 else f'{frac:.0%} along the way'}; back off or take another path")
    return None


def near(q, s=None):
    """Pairs closer than s['near'] at joint state q ({key: deg}) -> [(label, cm)], closest first."""
    s = s or setup()
    cl = clearances(_arr([q], "left"), _arr([q], "right"), s)
    def other(lab):   # the other arm, its camera or a mapped obstacle (not the own arm, not the table)
        a, b = (x.strip().split()[0] for x in lab.split("↔"))
        return a != b and "table" not in lab
    hits = [(lab, round(float(d[0]) * 100, 1)) for lab, (d, _) in cl.items()
            if "table" not in lab and d[0] < (s["near"] if other(lab) else 0.01)]
    return sorted(hits, key=lambda h: h[1])[:4]
