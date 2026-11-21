"""Collision-free joint paths for one arm around the capsule model in collide.py (other arm held still).

plan(side, start, goal_q, limits, s) -> list of {key: deg} waypoints (<= STEP deg apart), or raises ValueError.
Tries the straight joint move, then a few lift-over via points, then RRT-Connect; then shortens the path.
Validity uses collide.check's rule: every pair keeps its threshold, or (if it starts below) does not get worse.
"""
import time
import numpy as np
import collide

J = collide.J
STEP = 4.0      # deg between waypoints sent to robotd
RES = 2.0       # deg between collision samples along an edge


class _Free:
    def __init__(self, side, start, s):
        self.side, self.s = side, s
        other = "right" if side == "left" else "left"
        self.other = np.array([start[f"{other}_{j}"] for j in J])
        q0 = np.array([[start[f"{side}_{j}"] for j in J]])
        cl = self._cl(q0)
        mine = lambda lab: lab.split("↔")[0].split()[0] == side or lab.split("↔")[1].split()[0] == side
        self.labels = [lab for lab in cl if mine(lab)]
        self.floor = np.array([min(cl[l][1], cl[l][0][0] - 0.002) for l in self.labels])

    def _cl(self, Q):
        O = np.repeat(self.other[None], len(Q), 0)
        return collide.clearances(Q, O, self.s) if self.side == "left" else collide.clearances(O, Q, self.s)

    def worst(self, Q):
        """Q (N,5) -> (ok (N,) bool, label of the worst violation or None)."""
        cl = self._cl(np.atleast_2d(Q))
        D = np.stack([cl[l][0] for l in self.labels], 1) - self.floor     # (N, labels) >= 0 means fine
        ok = (D >= 0).all(1)
        return ok, (None if ok.all() else self.labels[int(np.argmin(D.min(0)))])

    def edge(self, a, b):
        n = max(int(np.abs(b - a).max() / RES), 1)
        return bool(self.worst(a + (b - a) * np.linspace(0, 1, n + 1)[:, None])[0].all())

    def path_ok(self, P):
        return all(self.edge(a, b) for a, b in zip(P[:-1], P[1:]))


def _shorten(P, free, tries=60, rng=None):
    rng = rng or np.random.default_rng(0)
    P = list(P)
    for _ in range(tries):
        if len(P) < 3:
            break
        i, j = sorted(rng.choice(len(P), 2, replace=False))
        if j - i > 1 and free.edge(P[i], P[j]):
            P = P[:i + 1] + P[j:]
    return P


def _densify(P):
    out = [P[0]]
    for a, b in zip(P[:-1], P[1:]):
        n = max(int(np.ceil(np.abs(b - a).max() / STEP)), 1)
        out += [a + (b - a) * k / n for k in range(1, n + 1)]
    return out


def _rrt(q0, q1, lo, hi, free, budget, rng, ext=12.0):
    A, B = [q0], [q1]; pa, pb = [-1], [-1]
    t_end = time.time() + budget

    def extend(T, par, x):
        i = int(np.argmin(np.abs(np.array(T) - x).max(1)))
        d = x - T[i]; m = np.abs(d).max()
        y = x if m <= ext else T[i] + d * ext / m
        if free.edge(T[i], y):
            T.append(y); par.append(i); return len(T) - 1, m <= ext
        return None, False

    while time.time() < t_end:
        x = rng.uniform(lo, hi)
        k, _ = extend(A, pa, x)
        if k is not None:
            while True:      # connect B toward the new node greedily
                kb, reached = extend(B, pb, A[k])
                if kb is None:
                    break
                if reached:
                    pa_path, pb_path = [], []
                    i = k
                    while i != -1: pa_path.append(A[i]); i = pa[i]
                    i = kb
                    while i != -1: pb_path.append(B[i]); i = pb[i]
                    P = pa_path[::-1] + pb_path
                    return P if np.allclose(P[0], q0) else P[::-1]
        A, B, pa, pb = B, A, pb, pa
    return None


def plan(side, start, goal_q, limits, s, budget=3.0):
    """start: {key: deg} both arms; goal_q: 5 joint degs for `side`. -> list of {side_joint: deg} waypoints."""
    free = _Free(side, start, s)
    q0 = np.array([start[f"{side}_{j}"] for j in J]); q1 = np.asarray(goal_q, float)
    ok, lab = free.worst(q1[None])
    if not ok[0]:
        raise ValueError(f"the goal itself collides ({lab})")
    lo = np.array([limits[f"{side}_{j}"][0] for j in J]); hi = np.array([limits[f"{side}_{j}"][1] for j in J])
    r0, r1 = sorted((q0[4], q1[4])); lo[4], hi[4] = max(lo[4], r0 - 30), min(hi[4], r1 + 30)   # no wild rolls
    P = None
    if free.edge(q0, q1):
        P = [q0, q1]
    if P is None:   # lift over: fold the arm up (lift back, elbow folded) at the start pan, swing, unfold
        for lift, elbow in ((-60, 60), (-80, 85), (-95, 95)):
            for wrist in (q0[3], 60.0):
                a = np.array([q0[0], lift, elbow, wrist, q0[4]]); b = np.array([q1[0], lift, elbow, wrist, q1[4]])
                a, b = np.clip(a, lo, hi), np.clip(b, lo, hi)
                if free.path_ok([q0, a, b, q1]):
                    P = [q0, a, b, q1]; break
            if P is not None:
                break
    if P is None:
        P = _rrt(q0, q1, lo, hi, free, budget, np.random.default_rng(0))
        if P is None:
            raise ValueError(f"no collision-free path found in {budget:.0f} s")
    P = _shorten(P, free)
    return [{f"{side}_{j}": float(v) for j, v in zip(J, q)} for q in _densify(P)]
