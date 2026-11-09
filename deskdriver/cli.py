"""desk: command-line driver for deskd."""
import argparse
import json
import os
import subprocess
import sys
import time

from .client import DeskClient, DeskError

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "deskdriver")
PID_FILE = os.path.join(STATE_DIR, "deskd.pid")
LOG_FILE = os.path.join(STATE_DIR, "deskd.log")


def _running_pid() -> int | None:
    try:
        pid = int(open(PID_FILE).read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def cmd_start(a, c):
    if _running_pid() and not a.foreground:
        print(f"deskd already running (pid {_running_pid()})")
        return
    args = [sys.executable, "-m", "deskdriver.daemon"] + a.daemon_args
    if a.foreground:
        os.execv(sys.executable, args)
    os.makedirs(STATE_DIR, exist_ok=True)
    logf = open(LOG_FILE, "ab")
    p = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    open(PID_FILE, "w").write(str(p.pid))
    print(f"deskd started (pid {p.pid}), log: {LOG_FILE}")
    for _ in range(40):
        time.sleep(0.5)
        try:
            s = c.status()
        except DeskError:
            continue
        if s["state"] == "ready":
            print(f"ready: screen {s['screen']['view'][0]}x{s['screen']['view'][1]}")
            return
        if s["state"] == "awaiting_permission":
            print("GNOME is asking for permission: click Share in the dialog on screen (once; it is remembered).")
            return
    print("still connecting; check `desk status`")


def cmd_stop(a, c):
    pid = _running_pid()
    if not pid:
        print("deskd is not running")
        return
    os.kill(pid, 15)
    print(f"stopped pid {pid}")


def cmd_status(a, c):
    print(json.dumps(c.status(), indent=2))


def cmd_shot(a, c):
    region = tuple(int(v) for v in a.region.split(",")) if a.region else None
    data, headers = c.screenshot(a.scale, region, "jpeg" if a.out.lower().endswith((".jpg", ".jpeg")) else "png",
                                 native=a.native)
    open(a.out, "wb").write(data)
    print(f"{a.out}: {headers.get('X-Desk-Image-Size')} (view {headers.get('X-Desk-View-Size')})")


def cmd_step(a, c):
    kw = json.loads(a.json) if a.json else {}
    print(json.dumps(c.step(a.do, **kw)))


def _simple(do, **fixed):
    def run(a, c):
        kw = dict(fixed)
        for k in ("x", "y", "to_x", "to_y", "dx", "dy", "text", "keys", "button", "ms", "count"):
            v = getattr(a, k, None)
            if v is not None:
                kw[k] = v
        print(json.dumps(c.step(do, **kw)))
    return run


def cmd_lease(a, c):
    print(json.dumps(c.lease(a.ttl, a.note, a.wait)))


def cmd_release(a, c):
    print(json.dumps(c.release()))


def cmd_clip(a, c):
    if a.text is None:
        sys.stdout.write(c.clipboard_get())
    else:
        print(json.dumps(c.clipboard_set(a.text)))


def cmd_reconnect(a, c):
    print(json.dumps(c.reconnect(a.forget)))


REC_PID = os.path.join(STATE_DIR, "record.pid")


def cmd_record(a, c):
    """desk record start [out.mp4] | stop: real-time MP4 of the screen buffer via ffmpeg (wall-clock timestamps)."""
    import signal
    if a.action == "stop":
        try:
            pid = int(open(REC_PID).read())
            os.kill(pid, signal.SIGINT)
            for _ in range(40):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            print("recording stopped")
        except (OSError, ValueError):
            print("no recording running")
        return
    out = a.out or os.path.expanduser(time.strftime("~/Videos/desk-%Y%m%d-%H%M%S.mp4"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    url = f"{c.base}/stream?fps={a.fps}&scale={a.scale}&quality=80"
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-use_wallclock_as_timestamps", "1", "-f", "mpjpeg", "-i", url,
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-vsync", "cfr", "-r", str(a.fps),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(os.path.join(STATE_DIR, "record.log"), "ab"),
                         stdin=subprocess.DEVNULL, start_new_session=True)
    open(REC_PID, "w").write(str(p.pid))
    print(f"recording to {out} (pid {p.pid}); stop with: desk record stop")


EYES_PID = os.path.join(STATE_DIR, "eyes.pid")


def cmd_eyes(a, c):
    """desk eyes start|stop: a headless Chrome running the TensorFlow.js page off screen, posting /detections."""
    import signal
    if a.action == "stop":
        try:
            os.killpg(int(open(EYES_PID).read()), signal.SIGTERM)
            print("eyes stopped")
        except (OSError, ValueError):
            print("no eyes running")
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    prof = os.path.join(STATE_DIR, "eyes-profile")
    cmd = ["google-chrome", "--headless=new", "--enable-logging=stderr", "--v=0", "--no-first-run", "--disable-extensions", "--mute-audio",
           "--user-data-dir=" + prof, "--window-size=1100,700", "--remote-allow-origins=*",
           f"{c.base}/tfjs?quiet=1"]
    if a.cpu:
        cmd.insert(2, "--disable-gpu")
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(os.path.join(STATE_DIR, "eyes.log"), "ab"),
                         stdin=subprocess.DEVNULL, start_new_session=True)
    open(EYES_PID, "w").write(str(p.pid))
    print(f"eyes started headless (pid {p.pid}); watch GET {c.base}/detections; stop with: desk eyes stop")


def cmd_mcp(a, c):
    from .mcp_server import main as mcp_main
    mcp_main()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk", description="drive the desktop: virtual mouse, keyboard, screen")
    ap.add_argument("--url", default=None, help="deskd base URL (default $DESK_URL or http://127.0.0.1:7777)")
    ap.add_argument("--agent", default=None, help="agent name for leases (default $DESK_AGENT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start the daemon (background)"); s.add_argument("-f", "--foreground", action="store_true")
    s.add_argument("daemon_args", nargs="*", help="passed to deskd, e.g. --max-width 1280 --cursor hidden")
    s.set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the daemon").set_defaults(fn=cmd_stop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("shot", help="save a screenshot"); s.add_argument("out", nargs="?", default="shot.png")
    s.add_argument("--scale", type=float); s.add_argument("--region", help="x,y,w,h in view pixels")
    s.add_argument("--native", action="store_true", help="full frame resolution"); s.set_defaults(fn=cmd_shot)

    def xy(p):
        p.add_argument("x", type=float); p.add_argument("y", type=float)
    s = sub.add_parser("move"); xy(s); s.set_defaults(fn=_simple("move"))
    s = sub.add_parser("click"); xy(s); s.add_argument("--button", default="left"); s.add_argument("--count", type=int)
    s.set_defaults(fn=_simple("click"))
    s = sub.add_parser("dblclick"); xy(s); s.set_defaults(fn=_simple("dblclick"))
    s = sub.add_parser("rightclick"); xy(s); s.set_defaults(fn=_simple("rightclick"))
    s = sub.add_parser("drag"); xy(s); s.add_argument("to_x", type=float); s.add_argument("to_y", type=float)
    s.set_defaults(fn=_simple("drag"))
    s = sub.add_parser("scroll"); xy(s); s.add_argument("dy", type=int); s.add_argument("--dx", type=int, default=0)
    s.set_defaults(fn=_simple("wheel"))
    s = sub.add_parser("type"); s.add_argument("text"); s.set_defaults(fn=_simple("type"))
    s = sub.add_parser("press"); s.add_argument("keys", nargs="+", help="e.g. ctrl+s  or  ctrl+a delete")
    s.set_defaults(fn=_simple("press"))
    s = sub.add_parser("wait"); s.add_argument("ms", type=float); s.set_defaults(fn=_simple("wait"))
    s = sub.add_parser("step", help="raw step: desk step click '{\"x\":10,\"y\":20}'")
    s.add_argument("do"); s.add_argument("json", nargs="?"); s.set_defaults(fn=cmd_step)
    s = sub.add_parser("lease", help="take the input lease for this agent"); s.add_argument("--ttl", type=float, default=60)
    s.add_argument("--note"); s.add_argument("--wait", type=float, default=0); s.set_defaults(fn=cmd_lease)
    sub.add_parser("release").set_defaults(fn=cmd_release)
    s = sub.add_parser("paste", help="paste text through the clipboard (any Unicode)"); s.add_argument("text")
    s.set_defaults(fn=_simple("paste"))
    s = sub.add_parser("clip", help="read the clipboard, or set it when text is given"); s.add_argument("text", nargs="?")
    s.set_defaults(fn=cmd_clip)
    s = sub.add_parser("reconnect"); s.add_argument("--forget", action="store_true", help="forget the saved permission")
    s.set_defaults(fn=cmd_reconnect)
    s = sub.add_parser("record", help="record the screen buffer to an MP4 (needs ffmpeg)")
    s.add_argument("action", choices=["start", "stop"]); s.add_argument("out", nargs="?")
    s.add_argument("--fps", type=int, default=10); s.add_argument("--scale", type=float, default=0.667)
    s.set_defaults(fn=cmd_record)
    s = sub.add_parser("eyes", help="TensorFlow.js detector in a headless Chrome, off screen")
    s.add_argument("action", choices=["start", "stop"]); s.add_argument("--cpu", action="store_true", help="no GPU")
    s.set_defaults(fn=cmd_eyes)
    sub.add_parser("mcp", help="run the MCP server (stdio)").set_defaults(fn=cmd_mcp)

    a = ap.parse_args(argv)
    c = DeskClient(a.url, a.agent)
    try:
        a.fn(a, c)
    except DeskError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
