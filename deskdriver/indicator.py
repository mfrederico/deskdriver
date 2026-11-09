"""Dock indicator: a badge and progress bar on the deskdriver dock icon while an agent is driving.

Uses the Unity LauncherEntry D-Bus API that Ubuntu's dock (Dash to Dock) listens to:
signal com.canonical.Unity.LauncherEntry.Update(app_uri, {count, count-visible, progress, progress-visible, urgent}).
"""
import logging
import threading
import time

import dbus
import dbus.service

log = logging.getLogger("desk.indicator")

APP_URI = "application://deskdriver.desktop"
IFACE = "com.canonical.Unity.LauncherEntry"


class LauncherEntry(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, "/com/canonical/unity/launcherentry/deskdriver")

    @dbus.service.signal(IFACE, signature="sa{sv}")
    def Update(self, app_uri, properties):
        pass


class Indicator:
    """Polls the desk every 0.5s and pushes badge state to the dock when it changes (heartbeat every 5s)."""

    def __init__(self, desk, idle_after: float = 2.0):
        self.desk = desk
        self.idle_after = idle_after
        self.entry = None
        self.last = None
        self.last_sent = 0.0
        threading.Thread(target=self._loop, name="indicator", daemon=True).start()

    def _ensure(self):
        if self.entry is None and self.desk.portal.bus is not None:
            self.entry = self.desk.portal.call(lambda: LauncherEntry(self.desk.portal.bus))

    def state(self) -> dict:
        lease = self.desk._lease_info()
        recent = (time.time() - self.desk.last_step_ts) < self.idle_after
        driving = bool(lease) or recent
        progress = 0.0
        if lease:
            progress = max(0.0, min(1.0, lease["expires_in"] / max(1.0, lease.get("ttl", 60))))
        return {"driving": driving, "count": self.desk.steps_done, "progress": progress,
                "lease": bool(lease), "ready": self.desk.state() == "ready"}

    def push(self, s: dict):
        props = dbus.Dictionary({
            "count": dbus.Int64(s["count"]),
            "count-visible": dbus.Boolean(s["driving"]),
            "progress": dbus.Double(s["progress"]),
            "progress-visible": dbus.Boolean(s["lease"]),
            "urgent": dbus.Boolean(False),
        }, signature="sv")
        self.desk.portal.call(lambda: self.entry.Update(APP_URI, props), timeout=5)

    def _loop(self):
        while True:
            try:
                self._ensure()
                if self.entry is not None:
                    s = self.state()
                    key = (s["driving"], s["count"], s["lease"], round(s["progress"], 2))
                    if key != self.last or time.time() - self.last_sent > 5:
                        self.push(s)
                        self.last, self.last_sent = key, time.time()
            except Exception as e:  # noqa: BLE001
                log.debug("indicator: %s", e)
            time.sleep(0.5)
