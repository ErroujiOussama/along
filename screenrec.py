"""Record the screen through the desktop portal: KDE asks once which screen to share, the choice is remembered
(restore token). PipeWire -> gst-launch -> H.264 .mkv. Usage: screenrec.py <out.mkv>; SIGTERM/SIGINT finalizes and stops."""
import os, signal, subprocess, sys
from jeepney import DBusAddress, new_method_call, MatchRule, message_bus
from jeepney.io.blocking import open_dbus_connection, Proxy

TOKEN = os.path.expanduser("~/.local/state/al_screenrec_token")
PORTAL = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                     interface="org.freedesktop.portal.ScreenCast")


def main(out):
    conn = open_dbus_connection("SESSION", enable_fds=True)
    bus, sender, n = Proxy(message_bus, conn), conn.unique_name[1:].replace(".", "_"), [0]

    def request(method, sig, *args):              # portal calls answer later with a Request.Response signal
        n[0] += 1
        tok = f"al{os.getpid()}_{n[0]}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response",
                         path=f"/org/freedesktop/portal/desktop/request/{sender}/{tok}")
        bus.AddMatch(rule)
        with conn.filter(rule) as q:
            args[-1]["handle_token"] = ("s", tok)
            conn.send_and_get_reply(new_method_call(PORTAL, method, sig, args))
            code, res = conn.recv_until_filtered(q, timeout=300).body
        if code != 0:
            sys.exit(f"screenrec: {method} cancelled or failed ({code})")
        return {k: v[1] for k, v in res.items()}

    session = request("CreateSession", "a{sv}", {"session_handle_token": ("s", f"als{os.getpid()}")})["session_handle"]
    opts = {"types": ("u", 1), "multiple": ("b", False), "cursor_mode": ("u", 2), "persist_mode": ("u", 2)}
    if os.path.exists(TOKEN):
        opts["restore_token"] = ("s", open(TOKEN).read().strip())
    request("SelectSources", "oa{sv}", session, opts)
    res = request("Start", "osa{sv}", session, "", {})
    if res.get("restore_token"):
        os.makedirs(os.path.dirname(TOKEN), exist_ok=True); open(TOKEN, "w").write(res["restore_token"])
    node = res["streams"][0][0]
    fd = conn.send_and_get_reply(new_method_call(PORTAL, "OpenPipeWireRemote", "oa{sv}", (session, {}))).body[0].to_raw_fd()
    os.dup2(fd, 3)
    gst = subprocess.Popen(["gst-launch-1.0", "-e", "-q", "pipewiresrc", "fd=3", f"path={node}", "do-timestamp=true",
                            "keepalive-time=1000", "!", "videoconvert", "!", "videoscale", "!", "videorate", "!",
                            "video/x-raw,format=I420,width=1920,pixel-aspect-ratio=1/1,framerate=25/1", "!",
                            "openh264enc", "bitrate=8000000", "!", "h264parse", "!", "matroskamux", "!",
                            "filesink", f"location={out}"], pass_fds=(3,))
    stop = lambda *_: gst.send_signal(signal.SIGINT)          # -e: EOS first, so the file is finalized
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    gst.wait()


if __name__ == "__main__":
    main(sys.argv[1])
