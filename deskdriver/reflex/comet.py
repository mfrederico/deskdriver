"""Comet Tag (voidlux.com/games/comet-tag): one key. Orbit a planet, Space flips your direction and lays a spoke.

What the eye sees (isometric view, orbit plane tilted 58 degrees, so lanes are ellipses squashed by cos 58):
  planet   big purple sphere                         -> centre of everything
  me       pale cyan comet                           -> angle and lane radius around the planet, direction of travel
  rivals   orange (chaser), green (twin), violet (switcher) comets
  spokes   pink walls laid by flips
  hearts   three red dots at the top                 -> lives
  GAME OVER big red text / PLAY cyan button          -> restart

Reflex: flip when a rival or spoke sits ahead of me on my lane within `look` radians; otherwise keep going. Flips
also lay spokes, which stun rivals, which is what scores tags. A language model would tune `look`, `band` and the
flip cooldown, not run the loop.
"""
import math
import time

import cv2
import numpy as np

from .runner import Policy, View
from .vision import blobs, mask_hsv, mask_rgb

TILT = math.cos(math.radians(58))   # y squash of the orbit plane


class CometTag(Policy):
    name = "comet"
    hz = 20.0
    scale = 0.5

    def __init__(self, look: float = 1.0, band: float = 0.45, cooldown: float = 0.55, debug_dir: str | None = None,
                 dry: bool = False):
        super().__init__()
        self.look = look            # radians ahead that count as "in my way"
        self.band = band            # lane tolerance as a share of the lane gap
        self.cooldown = cooldown    # seconds between flips
        self.debug_dir = debug_dir
        self.dry = dry
        self.centre = None
        self.lane_gap = None
        self.hist: list[tuple[float, float, float]] = []   # (angle, radius, time)
        self.flips = 0
        self.restarts = 0
        self.hearts = 0
        self.last_restart = 0.0
        self.last_seen = 0.0
        self.why = ""

    # -- seeing ---------------------------------------------------------------------------------
    def find_planet(self, f):
        m = mask_hsv(f, 118, 140, s_min=90, v_min=110)
        b = blobs(m, min_area=400)
        return (b[0]["x"], b[0]["y"], b[0]["w"]) if b else None

    def polar(self, x, y):
        cx, cy, _ = self.centre
        dx, dy = x - cx, (y - cy) / TILT
        return math.atan2(dy, dx), math.hypot(dx, dy)

    def arena(self, f):
        """The box around the planet that holds the lanes (keeps tab icons, the dock and the HUD out of the masks)."""
        if not self.centre:
            return 0, 0, f
        cx, cy, pw = self.centre
        rx, ry = int(pw * 3.6), int(pw * 3.6 * TILT + pw * 0.9)
        x0, y0 = max(0, int(cx - rx)), max(0, int(cy - ry))
        x1, y1 = min(f.shape[1], int(cx + rx)), min(f.shape[0], int(cy + ry))
        return x0, y0, f[y0:y1, x0:x1]

    def see(self, f):
        x0, y0, f = self.arena(f)
        me, rivals, spokes = self._see(f)
        for group in (me, rivals, spokes):
            for b in group:
                b["x"] += x0; b["y"] += y0; b["left"] += x0; b["top"] += y0
        return me, rivals, spokes

    def _see(self, f):
        me = blobs(mask_hsv(f, 85, 100, s_min=55, v_min=170), min_area=10, max_area=900)
        rivals = blobs(mask_hsv(f, 8, 22, s_min=150, v_min=180), min_area=8, max_area=900)          # orange
        rivals += blobs(mask_hsv(f, 55, 75, s_min=120, v_min=170), min_area=8, max_area=900)        # green
        rivals += blobs(mask_hsv(f, 128, 140, s_min=90, v_min=200), min_area=8, max_area=300)       # violet (small, unlike the planet)
        spokes = blobs(mask_hsv(f, 150, 172, s_min=90, v_min=170), min_area=6)                        # pink walls
        return me, rivals, spokes

    def hearts_now(self, f):
        """The red dots above the arena, between the planet's top and the browser bar."""
        if not self.centre:
            return 0
        cx, cy, pw = self.centre
        y1 = max(0, int(cy - pw * 2.2)); y0 = max(0, y1 - int(pw * 2.2))
        top = f[y0:y1, max(0, int(cx - pw * 1.2)): int(cx + pw * 1.2)]
        return len(blobs(mask_rgb(top, (255, 100, 100), 60), min_area=12, max_area=400))

    def game_over(self, f):
        h, w = f.shape[:2]
        mid = f[int(h * 0.3): int(h * 0.7), int(w * 0.25): int(w * 0.75)]
        red = mask_rgb(mid, (255, 100, 100), 70)
        return int(red.sum() // 255) > 0.012 * mid.shape[0] * mid.shape[1]

    def title(self, f):
        h, w = f.shape[:2]
        mid = f[int(h * 0.3): int(h * 0.85), int(w * 0.3): int(w * 0.7)]
        b = blobs(mask_hsv(mid, 85, 100, s_min=80, v_min=200), min_area=1500)
        return bool(b)

    # -- acting ---------------------------------------------------------------------------------
    def start(self, view: View):
        self.centre = self.find_planet(view.frame)

    def step(self, view: View):
        try:
            self._step(view)
        finally:
            if self.debug_dir and view.tick % 10 == 0:
                me, rivals, spokes = self.see(view.frame)
                self.draw(view.frame, me, rivals, spokes, view.tick)

    def _step(self, view: View):
        f = view.frame
        now = time.time()
        if view.tick % 40 == 0 or self.centre is None:
            c = self.find_planet(f)
            if c:
                self.centre = c
        if view.tick % 10 == 0 and self.find_planet(f):
            self.last_seen = now
        dark = f.mean() < 70
        button = dark and self.title(f)          # the big cyan PLAY / PLAY AGAIN button: the game is on screen, between runs
        if button:
            if now - self.last_restart > 2.0:
                self.last_restart = now
                self.restarts += 1
                self.hist.clear()
                if not self.dry:
                    view.desk.tap("b")
                    time.sleep(0.15)
                    view.desk.tap("space")
            self.why = "restart"
            return
        if not (dark and now - self.last_seen < 6.0):
            self.why = "game not on screen"
            self.hist.clear()
            return
        if self.centre is None:
            self.why = "no planet"
            return
        me, rivals, spokes = self.see(f)
        self.hearts = self.hearts_now(f)
        if not me:
            self.why = "no me"
            return
        a, r = self.polar(me[0]["x"], me[0]["y"])
        self.hist.append((a, r, now))
        self.hist = self.hist[-8:]
        if self.lane_gap is None:
            self.lane_gap = max(20.0, r * 0.3)
        direction = 0.0
        if len(self.hist) >= 3:
            a0, _, t0 = self.hist[0]
            da = (a - a0 + math.pi) % (2 * math.pi) - math.pi
            direction = 1.0 if da > 0 else -1.0 if da < 0 else 0.0
        threat = None
        for kind, group in (("rival", rivals), ("spoke", spokes)):
            for b in group:
                ba, br = self.polar(b["x"], b["y"])
                if abs(br - r) > self.band * self.lane_gap * 2:
                    continue
                ahead = (ba - a) * direction
                ahead = (ahead + math.pi) % (2 * math.pi) - math.pi
                if 0.05 < ahead < self.look:
                    if threat is None or ahead < threat[1]:
                        threat = (kind, ahead)
        if threat and direction != 0:
            if not self.dry and view.desk.tap("space", cooldown=self.cooldown):
                self.flips += 1
                self.hist.clear()
            self.why = f"flip: {threat[0]} {threat[1]:.2f} rad ahead"
        else:
            self.why = f"cruise dir={direction:+.0f} r={r:.0f} rivals={len(rivals)} spokes={len(spokes)}"

    def draw(self, f, me, rivals, spokes, tick):
        img = cv2.cvtColor(f.copy(), cv2.COLOR_RGB2BGR)
        if self.centre:
            cv2.circle(img, (int(self.centre[0]), int(self.centre[1])), 6, (255, 255, 255), 2)
        for b in me:
            cv2.circle(img, (int(b["x"]), int(b["y"])), 12, (255, 255, 0), 2)
        for b in rivals:
            cv2.circle(img, (int(b["x"]), int(b["y"])), 12, (0, 0, 255), 2)
        for b in spokes:
            cv2.rectangle(img, (b["left"], b["top"]), (b["left"] + b["w"], b["top"] + b["h"]), (255, 0, 255), 1)
        cv2.putText(img, self.why, (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.imwrite(f"{self.debug_dir}/comet-{tick:05d}.jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])

    def status(self) -> str:
        return f"hearts={self.hearts} flips={self.flips} restarts={self.restarts} {self.why}"
