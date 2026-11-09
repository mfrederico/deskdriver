"""Input for a reflex loop: keys that stay held until released, taps, and mouse moves/clicks, all through deskd."""
import json
import time
import urllib.error
import urllib.request


class Desk:
    def __init__(self, base: str = "http://127.0.0.1:7777", agent: str = "reflex"):
        self.base = base
        self.agent = agent
        self.held: set[str] = set()
        self.last_tap: dict[str, float] = {}

    def _steps(self, steps: list[dict]):
        req = urllib.request.Request(self.base + "/steps", data=json.dumps({"steps": steps, "agent": self.agent}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=15).read())
        except urllib.error.HTTPError as e:
            if e.code == 423:
                # agents paused from the tray (a person has the desk): hold still rather than crash; the policy keeps
                # watching and carries on when the pause lifts
                self.paused_by_tray = True
                time.sleep(0.5)
                return {"ok": False, "paused": True}
            raise

    def lease(self, ttl: float = 120, note: str = "reflex"):
        req = urllib.request.Request(self.base + "/lease", data=json.dumps({"agent": self.agent, "ttl": ttl, "note": note, "wait": 10}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=15).read())

    def release(self):
        req = urllib.request.Request(self.base + "/release", data=json.dumps({"agent": self.agent}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    # -- keys ------------------------------------------------------------------------------------
    def hold(self, key: str):
        if key not in self.held:
            self._steps([{"do": "keydown", "keys": key}])
            self.held.add(key)

    def let_go(self, key: str):
        if key in self.held:
            self._steps([{"do": "keyup", "keys": key}])
            self.held.discard(key)

    def set_held(self, keys: set[str]):
        """Make exactly these keys held (WASD-style steering in one call)."""
        for k in list(self.held - keys):
            self.let_go(k)
        for k in keys - self.held:
            self.hold(k)

    def tap(self, key: str, cooldown: float = 0.0) -> bool:
        """Press and release, unless tapped within `cooldown` seconds. Returns whether it tapped."""
        now = time.time()
        if now - self.last_tap.get(key, 0) < cooldown:
            return False
        self._steps([{"do": "press", "keys": [key], "delay_ms": 0}])
        self.last_tap[key] = now
        return True

    def release_all(self):
        for k in list(self.held):
            self.let_go(k)

    # -- mouse ------------------------------------------------------------------------------------
    def move(self, x: int, y: int):
        self._steps([{"do": "move", "x": x, "y": y}])

    def click(self, x: int, y: int, button: str = "left"):
        self._steps([{"do": "click", "x": x, "y": y, "button": button}])

    def mouse_down(self, x: int, y: int, button: str = "left"):
        self._steps([{"do": "down", "x": x, "y": y, "button": button}])

    def scroll(self, x: int, y: int, dy: int):
        """Mouse wheel at (x, y): dy notches, positive = down (away), negative = up."""
        self._steps([{"do": "scroll", "x": x, "y": y, "dy": dy}])

    def mouse_up(self, button: str = "left"):
        self._steps([{"do": "up", "button": button}])
