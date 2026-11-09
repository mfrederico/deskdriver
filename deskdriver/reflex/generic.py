"""A data-driven policy for an arbitrary 2D game, from a JSON spec. Good enough to start any arcade game and the
natural thing for a language model to write after one look at a screenshot:

{
  "me":      {"rgb": [150, 230, 240], "tol": 45, "min_area": 10},     // what I look like
  "threats": [{"rgb": [255, 138, 31], "tol": 50}],                     // things to keep away from
  "goods":   [{"rgb": [255, 220, 80], "tol": 50}],                     // things to go and touch
  "controls": "wasd" | "arrows" | "mouse" | "tap",                     // how I move (tap = one key, see "tap_key")
  "tap_key": "space", "tap_when": "threat_near", "tap_cooldown": 0.5,  // for one-button games
  "danger": 90, "speed": 1.0,                                          // flee radius in frame px; movement scale
  "restart": {"rgb": [255, 90, 90], "tol": 70, "share": 0.012, "keys": ["space"]},  // a red game-over screen -> keys
  "region": [0.03, 0.14, 1.0, 1.0]                                     // the game's share of the view, x0 y0 x1 y1
}

Rules, in order each tick: restart if the restart colour fills `share` of the region; else if a threat is within
`danger` of me, move away from it (or tap); else move toward the nearest good; else drift. Everything else is a job
for a real policy, which is still only one file.
"""
import json
import math
import time

import cv2

from .runner import Policy, View
from .vision import blobs, mask_rgb


class Generic(Policy):
    name = "generic"
    hz = 15.0
    scale = 0.5

    def __init__(self, spec_path: str, debug_dir: str | None = None, dry: bool = False):
        super().__init__()
        self.spec = json.load(open(spec_path))
        self.debug_dir, self.dry = debug_dir, dry
        self.last_restart = 0.0
        self.why = ""
        self.restarts = 0

    def _region(self, f):
        h, w = f.shape[:2]
        x0, y0, x1, y1 = self.spec.get("region", [0, 0, 1, 1])
        return int(w * x0), int(h * y0), f[int(h * y0): int(h * y1), int(w * x0): int(w * x1)]

    @staticmethod
    def _find(f, d, min_area=6):
        return blobs(mask_rgb(f, d["rgb"], d.get("tol", 50)), min_area=d.get("min_area", min_area))

    def step(self, view: View):
        ox, oy, f = self._region(view.frame)
        now = time.time()
        rs = self.spec.get("restart")
        if rs:
            m = mask_rgb(f, rs["rgb"], rs.get("tol", 60))
            if m.mean() / 255 > rs.get("share", 0.01) and now - self.last_restart > 2.0:
                self.last_restart = now
                self.restarts += 1
                if not self.dry:
                    for k in rs.get("keys", ["space"]):
                        view.desk.tap(k)
                        time.sleep(0.1)
                self.why = "restart"
                return
        me = self._find(f, self.spec["me"], 10)
        if not me:
            self.why = "no me"
            view.desk.set_held(set()) if not self.dry else None
            return
        mx, my = me[0]["x"], me[0]["y"]
        threats = [b for t in self.spec.get("threats", []) for b in self._find(f, t)]
        goods = [b for g in self.spec.get("goods", []) for b in self._find(f, g)]
        danger = self.spec.get("danger", 90)
        near = [t for t in threats if math.hypot(t["x"] - mx, t["y"] - my) < danger]
        controls = self.spec.get("controls", "wasd")
        if controls == "tap":
            if near and not self.dry:
                view.desk.tap(self.spec.get("tap_key", "space"), cooldown=self.spec.get("tap_cooldown", 0.5))
            self.why = f"tap: {len(near)} near, {len(threats)} threats" if near else f"wait: {len(threats)} threats"
            return
        if near:
            t = min(near, key=lambda b: math.hypot(b["x"] - mx, b["y"] - my))
            dx, dy = mx - t["x"], my - t["y"]
            self.why = f"flee {len(near)}"
        elif goods:
            g = min(goods, key=lambda b: math.hypot(b["x"] - mx, b["y"] - my))
            dx, dy = g["x"] - mx, g["y"] - my
            self.why = f"collect ({len(goods)} goods)"
        else:
            dx, dy = math.cos(now * 0.4), math.sin(now * 0.4)
            self.why = "drift"
        if self.dry:
            return
        if controls in ("wasd", "arrows"):
            keys = set()
            kx = ("a", "d") if controls == "wasd" else ("left", "right")
            ky = ("w", "s") if controls == "wasd" else ("up", "down")
            if abs(dx) > 4:
                keys.add(kx[1] if dx > 0 else kx[0])
            if abs(dy) > 4:
                keys.add(ky[1] if dy > 0 else ky[0])
            view.desk.set_held(keys)
        else:   # mouse: click where I want to go
            n = math.hypot(dx, dy) or 1
            step = 120 * self.spec.get("speed", 1.0)
            vx, vy = view.to_view(ox + mx + dx / n * step, oy + my + dy / n * step)
            view.desk.click(vx, vy)

    def stop(self, view: View):
        view.desk.release_all()

    def status(self) -> str:
        return f"restarts={self.restarts} {self.why}"
