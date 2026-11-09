"""The virtual input devices and the screen share, through the XDG RemoteDesktop + ScreenCast portals.

One portal session gives us a virtual pointer, a virtual keyboard and a PipeWire
stream of the screen. GNOME asks the person once (Share); after that the saved
restore token reconnects silently.
"""
import logging
import os
import pathlib
import secrets
import threading

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

log = logging.getLogger("desk.portal")

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJ = "/org/freedesktop/portal/desktop"
IFACE_RD = "org.freedesktop.portal.RemoteDesktop"
IFACE_SC = "org.freedesktop.portal.ScreenCast"
IFACE_REQ = "org.freedesktop.portal.Request"
IFACE_SESSION = "org.freedesktop.portal.Session"
IFACE_CLIP = "org.freedesktop.portal.Clipboard"

BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112, "back": 0x116, "forward": 0x115}
CURSOR_MODES = {"hidden": 1, "embedded": 2, "metadata": 4}
SOURCE_TYPES = {"monitor": 1, "window": 2, "any": 3, "virtual": 4}

TOKEN_FILE = (pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
              / "deskdriver" / "restore_token")


class PortalError(RuntimeError):
    pass


def _opts(d: dict | None = None) -> dbus.Dictionary:
    return dbus.Dictionary(d or {}, signature="sv")


class Portal:
    """Owns a GLib main loop thread, the portal session and the virtual devices."""

    def __init__(self, cursor_mode: str = "embedded", source_type: str = "monitor"):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        dbus.mainloop.glib.threads_init()
        self.cursor_mode = CURSOR_MODES[cursor_mode]
        self.source_type = SOURCE_TYPES[source_type]
        self.loop = GLib.MainLoop()
        self.thread = threading.Thread(target=self.loop.run, name="glib-loop", daemon=True)
        self.thread.start()
        self.bus: dbus.Bus | None = None
        self.session: dbus.ObjectPath | None = None
        self.stream_id: int | None = None
        self.logical = (0, 0)          # logical size of the shared stream
        self.position = (0, 0)         # position of the stream in the desktop layout
        self.pw_fd: int | None = None
        self.state = "init"
        self.on_closed = None
        self.clipboard = False          # True when the desktop granted clipboard access
        self._clip_text = ""

    # -- running things on the GLib thread ---------------------------------------------------
    def call(self, fn, *args, timeout: float | None = 30):
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["r"] = fn(*args)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
            done.set()
            return False

        GLib.idle_add(run)
        if not done.wait(timeout):
            raise PortalError("portal call timed out")
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def _iface(self, name):
        return dbus.Interface(self.bus.get_object(BUS_NAME, OBJ), name)

    def _request(self, iface_name: str, method: str, args: list, options: dict, timeout: float):
        """Call a portal method and wait for its Request.Response signal (on the caller's thread)."""
        token = "desk" + secrets.token_hex(6)
        got = threading.Event()
        out: dict = {}

        def start():
            sender = self.bus.get_unique_name()[1:].replace(".", "_")
            path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

            def on_response(code, results):
                out["code"] = int(code)
                out["results"] = results
                got.set()

            out["match"] = self.bus.add_signal_receiver(
                on_response, signal_name="Response", dbus_interface=IFACE_REQ, path=path)
            opts = _opts(options)
            opts["handle_token"] = dbus.String(token)
            getattr(self._iface(iface_name), method)(*args, opts)

        self.call(start)
        ok = got.wait(timeout)
        self.call(lambda: out["match"].remove())
        if not ok:
            raise PortalError(f"{method}: no response from the portal within {timeout}s")
        if out["code"] != 0:
            why = {1: "cancelled by the user", 2: "ended unexpectedly"}.get(out["code"], f"code {out['code']}")
            raise PortalError(f"{method}: {why}")
        return out["results"]

    # -- session ------------------------------------------------------------------------------
    def open(self, wait_for_user: float = 86400):
        self.state = "connecting"
        if self.bus is None:
            self.call(lambda: setattr(self, "bus", dbus.SessionBus(private=True)))
        r = self._request(IFACE_RD, "CreateSession", [],
                          {"session_handle_token": dbus.String("desk" + secrets.token_hex(4))}, 30)
        self.session = dbus.ObjectPath(r["session_handle"])
        self.call(lambda: self.bus.add_signal_receiver(
            self._closed, signal_name="Closed", dbus_interface=IFACE_SESSION, path=self.session))
        try:   # must come before Start; lets us paste text the layout cannot type, and read selections
            self.call(lambda: self._iface(IFACE_CLIP).RequestClipboard(self.session, _opts()))
            self.call(lambda: self.bus.add_signal_receiver(
                self._selection_transfer, signal_name="SelectionTransfer", dbus_interface=IFACE_CLIP))
        except Exception as e:  # noqa: BLE001
            log.warning("no clipboard portal: %s", e)

        devices = {"types": dbus.UInt32(7), "persist_mode": dbus.UInt32(2)}   # keyboard|pointer|touch, persist forever
        token = self._load_token()
        if token:
            devices["restore_token"] = dbus.String(token)
        self._request(IFACE_RD, "SelectDevices", [self.session], devices, 30)
        self._request(IFACE_SC, "SelectSources", [self.session], {
            "types": dbus.UInt32(self.source_type), "multiple": dbus.Boolean(False),
            "cursor_mode": dbus.UInt32(self.cursor_mode)}, 30)

        self.state = "awaiting_permission" if not token else "starting"
        log.info("asking the desktop to share the screen and input (%s)",
                 "silently, with the saved token" if token else "a Share dialog should be on screen")
        r = self._request(IFACE_RD, "Start", [self.session, dbus.String("")], {}, wait_for_user)
        streams = list(r.get("streams") or [])
        if not streams:
            raise PortalError("the desktop shared no screen")
        node, props = streams[0]
        self.stream_id = int(node)
        if "size" in props:
            self.logical = (int(props["size"][0]), int(props["size"][1]))
        if "position" in props:
            self.position = (int(props["position"][0]), int(props["position"][1]))
        if "restore_token" in r:
            self._save_token(str(r["restore_token"]))
        self.clipboard = bool(r.get("clipboard_enabled", False))
        fd = self.call(lambda: self._iface(IFACE_SC).OpenPipeWireRemote(self.session, _opts()))
        self.pw_fd = fd.take()
        self.state = "ready"
        log.info("session ready: stream %s logical %sx%s clipboard=%s", self.stream_id, *self.logical, self.clipboard)

    def close(self):
        if self.session is None:
            return
        try:
            self.call(lambda: dbus.Interface(self.bus.get_object(BUS_NAME, self.session), IFACE_SESSION).Close())
        except Exception as e:  # noqa: BLE001
            log.debug("close: %s", e)
        self.session = None
        self.state = "closed"

    def _closed(self, *_):
        log.warning("the desktop closed the session")
        self.state = "closed"
        self.session = None
        if self.on_closed:
            threading.Thread(target=self.on_closed, daemon=True).start()

    @staticmethod
    def _load_token() -> str | None:
        try:
            return TOKEN_FILE.read_text().strip() or None
        except OSError:
            return None

    @staticmethod
    def _save_token(token: str):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token)
        os.chmod(TOKEN_FILE, 0o600)

    @staticmethod
    def forget_token():
        try:
            TOKEN_FILE.unlink()
        except OSError:
            pass

    # -- virtual devices ----------------------------------------------------------------------
    def _notify(self, method: str, *args):
        if self.state != "ready":
            raise PortalError(f"no input session ({self.state})")
        self.call(lambda: getattr(self._iface(IFACE_RD), method)(self.session, _opts(), *args), timeout=10)

    def move_abs(self, x: float, y: float):
        """Move the pointer to (x, y) in the stream's logical coordinates."""
        self._notify("NotifyPointerMotionAbsolute", dbus.UInt32(self.stream_id), dbus.Double(x), dbus.Double(y))

    def move_rel(self, dx: float, dy: float):
        self._notify("NotifyPointerMotion", dbus.Double(dx), dbus.Double(dy))

    def button(self, name: str, pressed: bool):
        code = BUTTONS.get(name)
        if code is None:
            raise ValueError(f"unknown button '{name}' (left, right, middle, back, forward)")
        self._notify("NotifyPointerButton", dbus.Int32(code), dbus.UInt32(1 if pressed else 0))

    def wheel_steps(self, dx: int = 0, dy: int = 0):
        """Discrete scroll: positive dy scrolls down, positive dx scrolls right."""
        if dy:
            self._notify("NotifyPointerAxisDiscrete", dbus.UInt32(0), dbus.Int32(int(dy)))
        if dx:
            self._notify("NotifyPointerAxisDiscrete", dbus.UInt32(1), dbus.Int32(int(dx)))

    def wheel_smooth(self, dx: float, dy: float):
        self.call(lambda: self._iface(IFACE_RD).NotifyPointerAxis(
            self.session, _opts({"finish": dbus.Boolean(True)}), dbus.Double(dx), dbus.Double(dy)), timeout=10)

    def keycode(self, code: int, pressed: bool):
        self._notify("NotifyKeyboardKeycode", dbus.Int32(code), dbus.UInt32(1 if pressed else 0))

    def keysym(self, sym: int, pressed: bool):
        self._notify("NotifyKeyboardKeysym", dbus.Int32(sym), dbus.UInt32(1 if pressed else 0))

    # -- clipboard ------------------------------------------------------------------------------
    MIMES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "TEXT", "STRING"]

    def set_clipboard(self, text: str):
        """Own the clipboard with `text`; apps that paste get it through SelectionTransfer."""
        if not self.clipboard:
            raise PortalError("the desktop did not grant clipboard access")
        self._clip_text = text
        self.call(lambda: self._iface(IFACE_CLIP).SetSelection(
            self.session, _opts({"mime_types": dbus.Array(self.MIMES, signature="s")})), timeout=10)

    def _selection_transfer(self, session, mime, serial):
        if self.session is None or str(session) != str(self.session):
            return
        try:
            fd = self._iface(IFACE_CLIP).SelectionWrite(self.session, dbus.UInt32(serial)).take()
            try:
                os.write(fd, self._clip_text.encode())
            finally:
                os.close(fd)
            self._iface(IFACE_CLIP).SelectionWriteDone(self.session, dbus.UInt32(serial), dbus.Boolean(True))
        except Exception as e:  # noqa: BLE001
            log.debug("clipboard transfer %s (%s) not served: %s", serial, mime, e)

    def read_clipboard(self, mime: str = "text/plain;charset=utf-8") -> str:
        if not self.clipboard:
            raise PortalError("the desktop did not grant clipboard access")
        fd = self.call(lambda: self._iface(IFACE_CLIP).SelectionRead(self.session, dbus.String(mime)).take(), timeout=10)
        chunks = []
        try:
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                chunks.append(b)
        finally:
            os.close(fd)
        return b"".join(chunks).decode("utf-8", "replace")
