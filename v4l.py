"""Minimal V4L2 control access (no v4l2-ctl needed): list / set exposure, gain, white balance, focus, ...

Keys are the driver's control names normalised: "Exposure Time, Absolute" -> exposure_time_absolute.
A second open of the device for controls is fine while robotd streams from it.
"""
import ctypes, fcntl, os, re


class _Query(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("type", ctypes.c_uint32), ("name", ctypes.c_char * 32),
                ("minimum", ctypes.c_int32), ("maximum", ctypes.c_int32), ("step", ctypes.c_int32),
                ("default", ctypes.c_int32), ("flags", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 2)]


class _Ctrl(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


QUERYCTRL, G_CTRL, S_CTRL = _iowr(36, ctypes.sizeof(_Query)), _iowr(27, ctypes.sizeof(_Ctrl)), _iowr(28, ctypes.sizeof(_Ctrl))
NEXT, DISABLED, INACTIVE, CTRL_CLASS = 0x80000000, 0x1, 0x10, 6


def key(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def controls(dev):
    """{key: {"value", "min", "max", "default", "inactive"}} (inactive = overridden by an auto mode)."""
    fd, out, q = os.open(dev, os.O_RDWR), {}, _Query(id=NEXT)
    try:
        while True:
            try:
                fcntl.ioctl(fd, QUERYCTRL, q)
            except OSError:
                return out
            if not q.flags & DISABLED and q.type != CTRL_CLASS:
                c = _Ctrl(id=q.id)
                try:
                    fcntl.ioctl(fd, G_CTRL, c)
                except OSError:
                    pass
                out[key(q.name.decode())] = {"value": c.value, "min": q.minimum, "max": q.maximum,
                                            "default": q.default, "inactive": bool(q.flags & INACTIVE), "_id": q.id}
            q.id |= NEXT
    finally:
        os.close(fd)


def set_controls(dev, values):
    """Set {key: int}. Auto modes first, so e.g. auto_exposure=1 then exposure_time_absolute=80 works in one call."""
    have = controls(dev)
    bad = [k for k in values if k not in have]
    if bad:
        raise ValueError(f"unknown control(s) {bad}; have: {' '.join(have)}")
    fd = os.open(dev, os.O_RDWR)
    try:
        for k in sorted(values, key=lambda k: "auto" not in k):
            fcntl.ioctl(fd, S_CTRL, _Ctrl(id=have[k]["_id"], value=int(values[k])))
    finally:
        os.close(fd)
