"""Tray icon in GNOME's top bar: is the desk off, ready, or being driven right now.

Implements org.kde.StatusNotifierItem over D-Bus (what Ubuntu's AppIndicator extension shows), with the icon
drawn as a pixmap so no icon theme is involved. Three looks:
  off      grey pointer                 no portal session
  ready    blue pointer                 session up, nobody driving
  driving  orange pointer, pulsing dot  an agent holds the lease or just sent input
The tooltip names the agent and its note; a click opens the status page.
"""
import logging
import subprocess
import threading
import time

import dbus
import dbus.service
from PIL import Image, ImageDraw

log = logging.getLogger("desk.tray")

SNI = "org.kde.StatusNotifierItem"
WATCHER = "org.kde.StatusNotifierWatcher"
PATH = "/StatusNotifierItem"
SIZE = 24
GUIDE = "DRIVING · xxxxxxxxxxxxxxxx"   # the widest label, so the panel reserves the room once

COLORS = {"off": (110, 110, 118), "ready": (79, 140, 255), "driving": (255, 159, 67)}


def _pixmap(state: str, pulse: bool) -> list:
    """An ARGB32 (network byte order) pixmap of the pointer, as the SNI spec wants: [(w, h, bytes)]."""
    s = SIZE * 4  # draw big, shrink for smooth edges
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = COLORS[state]
    k = s / 64
    # a pointer that fills the box, so it reads at panel size
    pts = [(10 * k, 4 * k), (10 * k, 50 * k), (22 * k, 39 * k), (31 * k, 60 * k), (41 * k, 55 * k), (32 * k, 35 * k), (48 * k, 35 * k)]
    d.polygon(pts, fill=c + (255,), outline=(8, 10, 16, 255), width=max(1, int(3 * k)))
    if state == "driving":
        r = (10 if pulse else 7) * k
        d.ellipse((52 * k - r, 12 * k - r, 52 * k + r, 12 * k + r), fill=(255, 220, 120, 255) if pulse else c + (255,))
    im = im.resize((SIZE, SIZE), Image.LANCZOS)
    rgba = im.tobytes()
    argb = bytearray()
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        argb += bytes((a, r, g, b))
    return [(dbus.Int32(SIZE), dbus.Int32(SIZE), dbus.ByteArray(bytes(argb)))]


MENU = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
ID_STATUS, ID_OPEN, ID_PAUSE, ID_SEP, ID_QUIT, ID_RECONNECT = 1, 2, 3, 4, 5, 6


class Menu(dbus.service.Object):
    """The tray icon's right-click menu: status line, open the status page, pause agents, reconnect the screen, quit."""

    def __init__(self, bus, tray):
        super().__init__(bus, MENU_PATH)
        self.tray = tray
        self.revision = 1

    def _props(self, i):
        if i == ID_STATUS:
            return {"label": self.tray.status_label, "enabled": False}
        if i == ID_OPEN:
            return {"label": "Open status page"}
        if i == ID_PAUSE:
            return {"label": "Pause agents", "toggle-type": "checkmark",
                    "toggle-state": 1 if self.tray.desk.paused else 0}
        if i == ID_RECONNECT:
            return {"label": "Reconnect screen" if self.tray.desk.state() == "ready" else "Reconnect screen (desk off)"}
        if i == ID_SEP:
            return {"type": "separator"}
        if i == ID_QUIT:
            return {"label": "Quit deskdriver"}
        return {"children-display": "submenu"}

    @staticmethod
    def _dict(d):
        out = dbus.Dictionary(signature="sv")
        for k, v in d.items():
            out[k] = dbus.Int32(v) if isinstance(v, int) and not isinstance(v, bool) else (
                dbus.Boolean(v) if isinstance(v, bool) else dbus.String(v))
        return out

    def _node(self, i, depth):
        kids = [ID_STATUS, ID_OPEN, ID_PAUSE, ID_RECONNECT, ID_SEP, ID_QUIT] if i == 0 and depth != 0 else []
        children = dbus.Array([self._node(k, depth - 1) for k in kids], signature="v")
        return dbus.Struct((dbus.Int32(i), self._dict(self._props(i)), children), signature="ia{sv}av", variant_level=1)

    @dbus.service.method(MENU, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent, depth, names):
        return dbus.UInt32(self.revision), self._node(int(parent), int(depth))

    @dbus.service.method(MENU, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, names):
        return dbus.Array([dbus.Struct((dbus.Int32(i), self._dict(self._props(int(i)))), signature="ia{sv}")
                           for i in ids], signature="(ia{sv})")

    @dbus.service.method(MENU, in_signature="is", out_signature="v")
    def GetProperty(self, i, name):
        return self._dict(self._props(int(i))).get(name, dbus.String(""))

    @dbus.service.method(MENU, in_signature="isvu")
    def Event(self, i, event, data, ts):
        if str(event) == "clicked":
            self.tray.menu_clicked(int(i))

    @dbus.service.method(MENU, in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for i, event, data, ts in events:
            if str(event) == "clicked":
                self.tray.menu_clicked(int(i))
        return dbus.Array([], signature="i")

    @dbus.service.method(MENU, in_signature="i", out_signature="b")
    def AboutToShow(self, i):
        return False

    @dbus.service.method(MENU, in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], signature="i"), dbus.Array([], signature="i")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.GetAll(iface)[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return dbus.Dictionary({"Version": dbus.UInt32(3), "TextDirection": dbus.String("ltr"),
                                "Status": dbus.String("normal"), "IconThemePath": dbus.Array([], signature="s")},
                               signature="sv")

    @dbus.service.signal(MENU, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass

    @dbus.service.signal(MENU, signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed):
        pass

    def refresh(self):
        self.revision += 1
        self.LayoutUpdated(dbus.UInt32(self.revision), dbus.Int32(0))


class Item(dbus.service.Object):
    def __init__(self, bus, on_activate):
        super().__init__(bus, PATH)
        self.state = "off"
        self.pulse = False
        self.tip = "deskdriver: off"
        self.label = "DESK OFF"
        self.on_activate = on_activate

    # -- properties ------------------------------------------------------------------------------
    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.GetAll(iface)[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        pix = dbus.Array(_pixmap(self.state, self.pulse), signature="(iiay)")
        return dbus.Dictionary({
            "Category": dbus.String("ApplicationStatus"),
            "Id": dbus.String("deskdriver"),
            "Title": dbus.String("deskdriver"),
            "Status": dbus.String("NeedsAttention" if self.state == "driving" else "Active"),
            "WindowId": dbus.Int32(0),
            "IconName": dbus.String(""),
            "IconPixmap": pix,
            "OverlayIconName": dbus.String(""),
            "OverlayIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionIconName": dbus.String(""),
            "AttentionIconPixmap": pix,
            "AttentionMovieName": dbus.String(""),
            "ToolTip": dbus.Struct((dbus.String(""), dbus.Array([], signature="(iiay)"),
                                    dbus.String("deskdriver"), dbus.String(self.tip)), signature="sa(iiay)ss"),
            "ItemIsMenu": dbus.Boolean(False),
            "Menu": dbus.ObjectPath(MENU_PATH),
            "XAyatanaLabel": dbus.String(self.label),
            "XAyatanaLabelGuide": dbus.String(GUIDE),
        }, signature="sv")

    # -- methods the host calls --------------------------------------------------------------------
    @dbus.service.method(SNI, in_signature="ii")
    def Activate(self, x, y):
        self.on_activate()

    @dbus.service.method(SNI, in_signature="ii")
    def SecondaryActivate(self, x, y):
        self.on_activate()

    @dbus.service.method(SNI, in_signature="ii")
    def ContextMenu(self, x, y):
        self.on_activate()

    @dbus.service.method(SNI, in_signature="is")
    def Scroll(self, delta, orientation):
        pass

    # -- signals ---------------------------------------------------------------------------------
    @dbus.service.signal(SNI)
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI)
    def NewAttentionIcon(self):
        pass

    @dbus.service.signal(SNI, signature="s")
    def NewStatus(self, status):
        pass

    @dbus.service.signal(SNI)
    def NewToolTip(self):
        pass

    @dbus.service.signal(SNI, signature="ss")
    def XAyatanaNewLabel(self, label, guide):
        pass


class Tray:
    """Watches the desk twice a second and keeps the tray icon honest."""

    def __init__(self, desk, idle_after: float = 2.0):
        self.desk = desk
        self.idle_after = idle_after
        self.item = None
        self.menu = None
        self.last = None
        self.status_label = "deskdriver: starting"
        threading.Thread(target=self._loop, name="tray", daemon=True).start()

    def _register(self):
        bus = self.desk.portal.bus
        self.menu = Menu(bus, self)
        self.item = Item(bus, self._open)
        bus.get_object(WATCHER, "/StatusNotifierWatcher").RegisterStatusNotifierItem(
            bus.get_unique_name(), dbus_interface=WATCHER)
        log.info("tray icon registered")

    @staticmethod
    def _open():
        subprocess.Popen(["xdg-open", "http://127.0.0.1:7777/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def menu_clicked(self, i):
        if i == ID_OPEN:
            self._open()
        elif i == ID_PAUSE:
            self.desk.paused = not self.desk.paused
            log.info("agents %s from the tray", "paused" if self.desk.paused else "resumed")
            self.menu.refresh()
        elif i == ID_RECONNECT:
            log.info("reconnect from the tray")
            threading.Thread(target=self.desk._reconnect, daemon=True).start()
        elif i == ID_QUIT:
            log.info("quit from the tray")
            threading.Thread(target=self.desk.quit, daemon=True).start()

    def _state(self) -> tuple[str, str, str]:
        """(state, tooltip, label beside the icon)"""
        if self.desk.state() != "ready":
            return "off", f"deskdriver: {self.desk.state()}", "DESK OFF"
        lease = self.desk._lease_info()
        recent = (time.time() - self.desk.last_step_ts) < self.idle_after
        if lease:
            who = str(lease["agent"])[:16]
            return "driving", f"driving: {lease['agent']}" + (f" · {lease['note']}" if lease.get("note") else ""), f"DRIVING · {who}"
        if recent:
            return "driving", "driving", "DRIVING"
        return "ready", f"deskdriver: ready · {self.desk.steps_done} steps", "desk ready"

    def _loop(self):
        while True:
            try:
                if self.item is None:
                    if self.desk.portal.bus is not None:
                        self.desk.portal.call(self._register)
                else:
                    state, tip, label = self._state()
                    if self.desk.paused:
                        state, label = "off", "PAUSED"
                        tip = "deskdriver: agents paused from the tray"
                    pulse = (not self.item.pulse) if state == "driving" else False
                    key = (state, tip, pulse, label)
                    if key != self.last:
                        if self.status_label != tip:
                            self.status_label = tip
                            self.desk.portal.call(self.menu.refresh, timeout=5)
                        def push():
                            changed_state = self.item.state != state
                            changed_label = self.item.label != label
                            self.item.state, self.item.pulse, self.item.tip, self.item.label = state, pulse, tip, label
                            self.item.NewIcon()
                            self.item.NewAttentionIcon()
                            self.item.NewToolTip()
                            if changed_label:
                                self.item.XAyatanaNewLabel(label, GUIDE)
                            if changed_state:
                                self.item.NewStatus("NeedsAttention" if state == "driving" else "Active")
                        self.desk.portal.call(push, timeout=5)
                        self.last = key
            except Exception as e:  # noqa: BLE001
                log.debug("tray: %s", e)
            time.sleep(0.5)
