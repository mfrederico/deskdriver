"""A click-to-move action RPG (Path of Exile 2 and its kin): find enemies by their health bars, attack the nearest,
drink a flask when the life globe runs low, and wander when nothing is in sight.

Everything is tunable because every game draws these differently:
  enemy_rgb / enemy_tol   the colour of a monster's health bar (PoE 2: a red bar above the monster)
  life_box / life_rgb     where the life globe sits (a box in frame pixels) and its fill colour
  attack                  what attacking is: "lclick" (click the enemy) or a key held while the mouse points at it
  flask                   the key for the healing flask, and the life share that triggers it
  wander                  how far from screen centre a wander click lands
The player is assumed to stay at the screen centre (true for PoE 2 and most isometric ARPGs).
A language model should set these from one screenshot, then leave the loop alone.
"""
import json
import logging
import math
import os
import random
import time

import cv2
import numpy as np

from .runner import Policy, View
from .vision import CamMotion, Motion, bar_fill, blobs, mask_rgb


log = logging.getLogger("arpg")


class ClickToMove(Policy):
    name = "arpg"
    hz = 8.0
    scale = 0.5

    def __init__(self, enemy_rgb=(200, 30, 30), enemy_tol: float = 55, min_bar: float = 6, life_box=(38, 448, 44, 64),
                 life_rgb=(160, 20, 30), life_low: float = 0.45, flask: str = "1", flask_cooldown: float = 4.0,
                 attack: str = "lclick", attack_key: str = "", wander: float = 520, wander_every: float = 2.5,
                 enemy_mode: str = "both", motion_min: float = 60, minimap=(822, 5, 138, 140), nav: str = "minimap",
                 heading_deg: float = 0.0, move: str = "click", unstick_key: str = "q", aoe_key: str = "e",
                 goal_deg: float | None = None, zoom_out: float = 1, take_exits: float = 0, control: str = "",
                 loot_max_tier: float = 3,
                 aoe_every: float = 4.0, motion_kind: str = "cam",
                 debug_dir: str | None = None, dry: bool = False):
        super().__init__()
        self.unstick_key = unstick_key            # a movement skill to fire when stuck (PoE2 warrior: q = Rolling Slam)
        self.aoe_key, self.aoe_every = aoe_key, aoe_every   # an area attack to drop on the crowd now and then (e = Earthquake)
        self.last_aoe = 0.0
        self.move = move                          # click: click the ground to walk; wasd: hold the game's WASD keys
        self.moving: set[str] = set()             # the movement keys held right now (wasd mode)
        self.minimap, self.nav = minimap, nav     # the minimap's box in frame pixels; nav = minimap | random | fixed
        self.mana_box = (878, 448, 44, 64)        # the mana globe: blue whenever the game is on screen
        self.heading_until = 0.0
        self.fixed_heading = math.radians(heading_deg)   # nav=fixed: a strategist read the map and chose this
        self.enemy_mode, self.motion_min = enemy_mode, int(motion_min)   # bars | motion | both
        self.motion = CamMotion() if motion_kind == "cam" else Motion()   # cam: works while walking; plain: old differencing
        self.enemy_rgb, self.enemy_tol, self.min_bar = enemy_rgb, int(enemy_tol), int(min_bar)
        self.life_box, self.life_rgb, self.life_low = life_box, life_rgb, life_low
        self.zoom_out = bool(zoom_out)           # --set zoom_out=0 to leave the camera alone
        self.goal_deg = goal_deg                 # --set goal_deg=-45: explorer bias toward a screen angle (0 right, 90 down)
        self.take_exits = bool(take_exits)       # --set take_exits=1: walk to the nearest orange exit icon on the minimap
        self.exits_seen = 0
        self.exit_dir = None                     # (screen angle, minimap px) of the exit icon we are walking to
        self.me_y = 0.42                         # the character stands this far down the frame (the camera leads it)
        self.loot_max_tier = int(loot_max_tier)  # 0 quest, 1 unique, 2 rare/gem, 3 magic, 4 normal (--set loot_max_tier=4 to take whites)
        self.control = control                   # --set control=FILE: a JSON file a strategist edits while we run
        self.control_mtime = 0.0; self.control_checked = 0.0
        self.strip = []; self.strip_at = 0.0     # the minimap every 5 s, last 12, as one image for a strategist
        self.blocked = []                        # (heading, when) of recent stuck events: the explorer avoids them
        self.pause_wanted = False; self.paused = False   # strategist handshake: {"pause": true} in the control file
        self.quiet_since = time.time(); self.status_at = 0.0
        self.chord_at = 0.0                      # when the current WASD chord went down
        self.life_long = []; self.attacks_hist = []   # for the slow-drain hurt test
        self.world_hist = []                     # dead-reckoned positions, for the stuck test
        self.frontiers = []; self.exit_list = []; self.unstick_hist = []   # text features for a strategist
        self.flask, self.flask_cooldown = flask, flask_cooldown
        self.attack, self.attack_key = attack, attack_key
        self.wander, self.wander_every = wander, wander_every
        self.debug_dir, self.dry = debug_dir, dry
        self.last_flask = 0.0
        self.last_wander = 0.0
        self.heading = math.radians(heading_deg) if nav == "fixed" else random.uniform(0, 2 * math.pi)
        self.enemies = 0
        self.life = 1.0
        self.attacks = 0
        self.why = ""
        self.before_click = None      # the frame just before a wander click: a panel opening changes most of it
        self.panels_closed = 0
        self.dead_since = 0.0
        self.deaths = 0
        self.picked: list[tuple[float, float, float]] = []   # (x, y, t) of labels clicked lately
        self.pickup_fails = 0           # labels still on the ground after a click: the bag is probably full
        self.inventory_full = False     # a strategist (LLM) must sell, equip or drop before loot matters again
        self.beam_since = 0.0           # when we started walking to the current beam
        self.map_gray = None            # last minimap (grey) for motion tracking
        self.world = [0.0, 0.0]         # where we are, in minimap pixels, since the zone started
        self.visited = np.zeros((600, 600), np.uint8)   # 5 px cells, origin at (300, 300): where we have walked
        self.map_prev_t = 0.0
        import glob as _glob, os as _os
        tdir = _os.path.join(_os.path.dirname(__file__), "templates")
        self.zone_templates = [cv2.imread(t, cv2.IMREAD_GRAYSCALE) for t in sorted(_glob.glob(_os.path.join(tdir, "zone-*.png")))]
        self.zone_templates = [t for t in self.zone_templates if t is not None]
        self.beams_done: list[tuple[float, float, float]] = []   # beams given up on or collected: (x, y, t)
        self.pickups = 0
        self.holding = False          # left button held on a target (a mace swing must not be re-clicked mid-swing)
        self.hover = None             # (x, y, t) of the motion blob the cursor was sent to, awaiting the red highlight
        self.sweep_i = 0              # the ring sweep: hover points around the character to light up monsters
        self.sweep_hit = None         # (x, y, t) a sweep point that lit up red
        self.rejected: list[tuple[float, float, float]] = []   # hovered, no highlight: not a monster, for a while
        self.hover_streak = 0.0           # when the current run of unconfirmed hovers began
        self.life_hist: list[tuple[float, float]] = []   # (t, life) lately: a drop with no target means retaliate
        self.panic_until = 0.0            # while set, swing at whatever is adjacent, no confirmation needed
        self.panic_i = 0
        self.retaliations = 0
        self.motion_mute_until = 0.0      # after 3 s of hovering nothing real, motion is ignored for a while
        self.confirmed = 0
        self.map_prev = None          # the minimap a moment ago: if it stops scrolling we are stuck
        self.still_since = 0.0
        self.unsticks = 0
        self.follow_until = 0.0       # wall-following until then (switched on by repeated stucks, or nav=edge)
        self.follow_side = 1          # +1 wall on the right, -1 on the left; flipped each time a follow run times out
        self.wall_gap = None
        if nav == "edge":
            self.follow_until = 1e18      # --set nav=edge: hug the map edge from the start

    def track_minimap(self, f):
        """Dead-reckon our position from how the minimap scrolls under the marker (phase correlation)."""
        x, y, w, h = self.minimap
        g = cv2.cvtColor(f[y: y + h, x: x + w], cv2.COLOR_RGB2GRAY).astype(np.float32)
        cv2.circle(g, (w // 2, h // 2), 9, float(g.mean()), -1)           # hide the marker, it never moves
        if self.map_gray is not None and self.map_gray.shape == g.shape:
            (dx, dy), resp = cv2.phaseCorrelate(self.map_gray, g)
            if math.hypot(dx, dy) <= 35 and resp >= 0.05:                   # else: effects or a panel, skip the frame
                self.world[0] -= dx; self.world[1] -= dy                   # the map scrolls against our motion
        self.map_gray = g
        cx, cy = int(300 + self.world[0] / 5), int(300 + self.world[1] / 5)
        if 2 <= cx < 598 and 2 <= cy < 598:
            self.visited[cy - 2: cy + 3, cx - 2: cx + 3] = 1               # a swath ~25 px wide counts as seen

    def explore_heading(self, f):
        """Head for the revealed-area boundary farthest from anywhere we have walked (frontier exploration with
        memory). Works in fields, where the lavender line is just the edge of the revealed map, and in caves."""
        x, y, w, h = self.minimap
        m = f[y: y + h, x: x + w].astype(int)
        r, g, b = m[..., 0], m[..., 1], m[..., 2]
        outline = (b > 85) & (r > 55) & (b > r + 8) & (b > g + 8) & (np.abs(r - g) < 40)
        cyan = (b > 150) & (b < 215) & (g > 120) & (r < 120) & (b > r + 60)
        water = (b > 120) & (g > 90) & (r < 90) & (b > r + 60)
        big_cyan = np.zeros_like(cyan)
        for cb in blobs(cyan.astype(np.uint8) * 255, min_area=121):
            big_cyan[cb["top"]: cb["top"] + cb["h"], cb["left"]: cb["left"] + cb["w"]] = True
        cand = (outline | (cyan & ~big_cyan)) & ~(water & big_cyan)
        cand[:, : 2] = cand[:, -2:] = cand[: 2, :] = cand[-2:, :] = False
        ys, xs = np.nonzero(cand)
        px, py = w / 2, h / 2
        # zone exits and area entrances are orange icons on the minimap (the player's X is orange too, at the centre):
        # walking into one changes zone, so steer clear unless a strategist asked for the exit (take_exits=1)
        yy_, xx_ = np.mgrid[0:h, 0:w]
        ex = (r > 150) & (g > 60) & (g < 175) & (b < 90) & (r > b + 70) & ((xx_ - px) ** 2 + (yy_ - py) ** 2 > 12 ** 2)
        exits = [(e["x"], e["y"]) for e in blobs(ex.astype(np.uint8) * 255, min_area=4)]
        self.exits_seen = len(exits)
        self.exit_dir = None
        if self.take_exits and exits:
            exx, exy = min(exits, key=lambda e: (e[0] - px) ** 2 + (e[1] - py) ** 2)
            self.exit_dir = (math.atan2(exy - py, exx - px), math.hypot(exx - px, exy - py))
            return self.exit_dir[0]
        if self.goal_deg is not None:
            # an exit icon in the goal's direction is the door we are looking for: keep it out of the penalty, and
            # once it is close, walk straight at it
            g_ = math.radians(self.goal_deg)
            toward = [e for e in exits if math.cos(math.atan2(e[1] - py, e[0] - px) - g_) > 0.0]   # the goal's half-plane
            exits = [e for e in exits if e not in toward]
            if toward and math.hypot(*self.world) > 120:
                # ... but not a door right beside where this run began (the Mud Burrow mouth sits next to the waypoint)
                exx, exy = min(toward, key=lambda e: (e[0] - px) ** 2 + (e[1] - py) ** 2)
                if (exx - px) ** 2 + (exy - py) ** 2 < 60 ** 2:
                    return math.atan2(exy - py, exx - px)
            elif toward:
                exits = exits + toward                                     # still too close to home: a door is a wall
        if len(xs) < 10:
            return None
        far = cv2.distanceTransform((1 - self.visited).astype(np.uint8), cv2.DIST_L2, 3)   # cells to the nearest visit
        best, best_score = None, -1e9
        scored = []
        for i in range(0, len(xs), 2):
            dx, dy = xs[i] - px, ys[i] - py
            d = math.hypot(dx, dy)
            if d < 6:
                continue
            cx = int(300 + (self.world[0] + dx) / 5); cy = int(300 + (self.world[1] + dy) / 5)
            novelty = float(far[cy, cx]) if 0 <= cx < 600 and 0 <= cy < 600 else 0.0
            score = min(novelty, 12.0) * 5 - 0.08 * d + (8 if cyan[ys[i], xs[i]] else 0)
            a = math.atan2(dy, dx)
            if math.cos(a - self.heading) > 0.7:
                score += 12                                                # commit: re-aiming every 1.5 s must not flip-flop
            if self.goal_deg is not None and math.cos(a - math.radians(self.goal_deg)) > 0.5:
                score += 10                                                # a strategist read the world map: the exit lies this way
            nb = sum(1 for ba, _ in self.blocked if math.cos(a - ba) > 0.75)
            if nb:
                score -= 40 * nb                                           # stuck that way, more each time it happened
            if exits and min((xs[i] - exx) ** 2 + (ys[i] - exy) ** 2 for exx, exy in exits) < 16 ** 2:
                score -= 40                                                # a boundary next to an exit icon is a door, not a frontier
            scored.append((score, a, novelty, d))
            if score > best_score:
                best, best_score = a, score
        # the top few distinct directions, for a strategist that reads text: (deg, novelty, px)
        top = []
        for sc, a, nov, d in sorted(scored, key=lambda t: -t[0]):
            if all(math.cos(a - b) < 0.7 for _, b, _, _ in top):
                top.append((sc, a, nov, d))
            if len(top) == 4:
                break
        self.frontiers = [{"deg": int(math.degrees(a)), "novelty": int(nov), "px": int(d)} for _, a, nov, d in top]
        self.exit_list = [{"deg": int(math.degrees(math.atan2(e[1] - py, e[0] - px))), "px": int(math.hypot(e[0] - px, e[1] - py))} for e in exits]
        return best

    def follow_heading(self, f):
        """Wall-following (user, 2026-11-09: "follow the outside edge of the map itself and find new areas"): keep
        the lavender outline on one side and walk along it. A hand on the wall passes every opening along it, so
        it finds the corridors and exits that the frontier scorer keeps missing. Returns a screen angle or None."""
        x, y, w, h = self.minimap
        m = f[y: y + h, x: x + w].astype(int)
        if m.size == 0:
            return None
        r, g, b = m[..., 0], m[..., 1], m[..., 2]
        wall = (b > 85) & (r > 55) & (b > r + 8) & (b > g + 8) & (np.abs(r - g) < 40)
        wall = cv2.dilate(wall.astype(np.uint8), None, iterations=1)      # the dim line breaks up: rejoin it
        # The OUTSIDE edge only: islands (rocks, ponds) are closed loops inside the map and circling one never ends.
        # The outer edge runs off the minimap box; an island fits inside it.
        n, lab, st, _ = cv2.connectedComponentsWithStats(wall, connectivity=8)
        outer = np.zeros(wall.shape, bool)
        for i in range(1, n):
            l, t, bw, bh = st[i, 0], st[i, 1], st[i, 2], st[i, 3]
            if l <= 3 or t <= 3 or l + bw >= w - 3 or t + bh >= h - 3:
                outer |= lab == i
        wall = outer
        wall[:, :2] = wall[:, -2:] = wall[:2, :] = wall[-2:, :] = False
        ys, xs = np.nonzero(wall)
        if len(xs) < 10:
            return None
        px, py = w / 2, h / 2
        dx, dy = xs - px, ys - py
        d = np.hypot(dx, dy)
        keep = d > 3                                               # the marker covers the wall right under it
        dx, dy, d = dx[keep], dy[keep], d[keep]
        if not len(d):
            return None
        near = d < 16
        if not near.any():
            i = int(np.argmin(d))                                   # no wall at hand yet: go and touch the nearest
            self.wall_gap = float(d[i])
            return math.atan2(dy[i], dx[i])
        wgt = 1.0 / d[near] ** 2
        aw = math.atan2(float((dy[near] * wgt).sum()), float((dx[near] * wgt).sum()))   # where the wall is
        gap = float(d[near].min())
        self.wall_gap = gap
        side = self.follow_side                                    # +1: wall on the right (screen y points down)
        heading = aw - side * math.pi / 2                          # walk along it
        if gap < 5:
            heading -= side * 0.45                                 # too close: ease off the wall
        elif gap > 10:
            heading += side * 0.45                                 # drifting off: lean back in
        return heading

    def minimap_heading(self, f):
        """Where to go, from the minimap: away from the centre of what is already explored (the drawn, brighter
        map), toward a gold quest marker if one shows. Returns an angle in screen space, or None."""
        x, y, w, h = self.minimap
        m = f[y: y + h, x: x + w].astype(int)
        if m.size == 0:
            return None
        r, g, b = m[..., 0], m[..., 1], m[..., 2]
        px, py = w / 2, h / 2                                          # the minimap is centred on the player
        yy_, xx_ = np.ogrid[:h, :w]
        orange = (r > b + 40) & (r > 120) & ((xx_ - px) ** 2 + (yy_ - py) ** 2 <= 10 ** 2)   # the player's X marker
        if orange.sum() >= 5:                                          # it sits a few px off the box centre
            ys_, xs_ = np.nonzero(orange)
            px, py = float(xs_.mean()), float(ys_.mean())
        gold = (r > 150) & (g > 120) & (b < 100) & (r > b + 60) & ~orange   # a quest marker (gold chevron)
        if gold.sum() >= 4:
            ys, xs = np.nonzero(gold)
            return math.atan2(ys.mean() - py, xs.mean() - px)
        # Ray-cast from the marker: in each of 24 directions walk out until the zone outline (lavender) or the map's
        # edge. The best direction has the longest clear ray and the most unexplored (blue-tinted or undrawn) ground
        # along it, so the bot follows corridors and never grinds into the zone's edge.
        # the lavender boundary, dim edges included: the strict (b > 150) test saw only a few dozen pixels of it
        outline = (b > 85) & (r > 55) & (b > r + 8) & (b > g + 8) & (np.abs(r - g) < 40)
        water = (b > 120) & (g > 90) & (r < 90) & (b > r + 60)                            # the river: also a wall
        # Small cyan segments cap the ends of explored corridors: they are the frontier, the way into the unknown.
        # Long cyan runs are water or the zone edge, which are walls.
        cyan = (b > 150) & (b < 215) & (g > 120) & (r < 120) & (b > r + 60)   # b < 215 keeps checkpoint icons out
        frontier = np.zeros_like(cyan)
        for cb in blobs(cyan.astype(np.uint8) * 255, min_area=5, max_area=120):
            frontier[cb["top"]: cb["top"] + cb["h"], cb["left"]: cb["left"] + cb["w"]] |= cyan[cb["top"]: cb["top"] + cb["h"], cb["left"]: cb["left"] + cb["w"]]
        outline = (outline | water | cyan) & ~frontier
        outline = cv2.dilate(outline.astype(np.uint8), None, iterations=1).astype(bool)   # rays must not slip between pixels
        # The player marker is drawn over the wall it stands beside, leaving a gap that rays would escape through:
        # thicken the outline around the centre so a wall interrupted by the marker closes up again.
        gray = m.max(axis=2).astype(np.float32)
        cave = float((gray < 12).mean()) > 0.3    # caves: mostly black void, the lavender lines are solid corridor walls
        bridge = cv2.dilate(outline.astype(np.uint8), np.ones((11, 11), np.uint8)) > 0
        if cave:
            barrier = outline | (orange & bridge)     # marker pixels within 5 px of a wall are that wall: no gap to leak through
        else:
            # fields: the lavender line is the edge of what has been revealed, walkable or not; only water is sure
            barrier = (water | (cyan & ~frontier))
            barrier = cv2.dilate(barrier.astype(np.uint8), None, iterations=1).astype(bool)
        # Flood the walkable map from the player: whatever the fill reaches is where we can go.
        # The marker may straddle a wall, with free pixels on both sides. Flood from each side; the explored
        # corridor network is the smaller of the two regions (the outside is the rest of the box).
        fills = []
        cx_, cy_ = int(round(px)), int(round(py))
        for dy in range(-6, 7):
            for dx in range(-6, 7):
                sx, sy = cx_ + dx, cy_ + dy
                if not (0 <= sx < w and 0 <= sy < h) or barrier[sy, sx] or any(fl[sy, sx] for _, fl in fills):
                    continue
                fmask = np.zeros((h + 2, w + 2), np.uint8)
                fmask[1:-1, 1:-1] = barrier.astype(np.uint8)
                cv2.floodFill(np.zeros((h, w), np.uint8), fmask, (sx, sy), 255, flags=4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
                fl = fmask[1:-1, 1:-1] == 255
                fills.append((int(fl.sum()), fl))
        if not fills:
            return None
        big = [fl for area, fl in fills if area >= 60]
        filled = min(big, key=lambda fl: int(fl.sum())) if big else max(fills, key=lambda t: t[0])[1]
        reach = []
        for cb in blobs(frontier.astype(np.uint8) * 255, min_area=5):
            x0, y0, x1, y1 = max(0, cb["left"] - 2), max(0, cb["top"] - 2), min(w, cb["left"] + cb["w"] + 2), min(h, cb["top"] + cb["h"] + 2)
            if filled[y0:y1, x0:x1].any():
                dx, dy = cb["x"] - px, cb["y"] - py
                reach.append((math.hypot(dx, dy), math.atan2(dy, dx)))
        if reach:
            return min(reach)[1]
        # Unexplored ground: the explored map is a smooth translucent fill, fog shows the terrain texture through
        # (local contrast) and cave void is plain black. Rays want to end up in either.
        mu = cv2.blur(gray, (7, 7)); mu2 = cv2.blur(gray * gray, (7, 7))
        texture = np.sqrt(np.maximum(mu2 - mu * mu, 0))
        unexplored = ((texture > 5) | (gray < 12)) & ~outline
        unexplored = cv2.morphologyEx(unexplored.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)) > 0
        best, best_score = None, -1.0
        for i in range(24):
            a = i * 2 * math.pi / 24
            ca, sa = math.cos(a), math.sin(a)
            free, fog = 0, 0
            for step in range(1, 60):
                xx, yy = int(round(px + ca * step)), int(round(py + sa * step))
                if not (0 <= xx < w and 0 <= yy < h) or not filled[yy, xx]:
                    break
                free += 1
                if unexplored[yy, xx]:
                    fog += 1
            score = (free if cave else 0.3 * free) + 1.5 * fog + (4 if math.cos(a - self.heading) > 0.7 else 0)
            if score > best_score:
                best, best_score = a, score
        if best is None or best_score < 6:
            return None
        return best

    @staticmethod
    def beams_now(f: np.ndarray) -> list[tuple[float, float]]:
        """Drop beams: valuable items (skill gems, currency) shoot a thin slanted teal beam up from the ground.
        Returns the beams' bases (frame px), lowest point of each, in the play area."""
        h, w = f.shape[:2]
        r, g, b = f[..., 0].astype(int), f[..., 1].astype(int), f[..., 2].astype(int)
        m = (((g > 140) & (b > 140) & (r < 150) & (g > r + 40) & (np.abs(g - b) < 40))       # teal: gems, currency
             | ((g > 170) & (r < 130) & (b < 130) & (g > r + 60))).astype(np.uint8)          # green: quest items
        m[int(h * 0.8):, :] = 0; m[:, : int(w * 0.12)] = 0; m[:, int(w * 0.84):] = 0
        n, lab, st, _ = cv2.connectedComponentsWithStats(m)
        out = []
        for i in range(1, n):
            x, y, bw, bh, area = st[i]
            if bh >= 35 and bh >= 2 * bw and area >= 30:
                ys, xs = np.nonzero(lab[y: y + bh, x: x + bw] == i)
                j = int(ys.argmax())
                out.append((float(x + xs[j]), float(y + ys[j])))
        return out

    def loot_now(self, f):
        """Ground item labels: a short line of light text (white, blue, yellow or orange) on a flat dark box, in the
        play area, made of several letter-sized pieces. Returns [{x, y, w, h}] of label centres."""
        h, w = f.shape[:2]
        mx = f.max(axis=2); mn = f.min(axis=2)
        teal = (f[..., 1] > 150) & (f[..., 2] > 150) & (f[..., 0] < 80)          # skill gem labels: teal on dark teal
        R, G, B = f[..., 0].astype(int), f[..., 1].astype(int), f[..., 2].astype(int)
        green = (G > 140) & (R < 110) & (B < 110) & (G > R + 50)                  # quest items
        orange = (R > 150) & (G > 60) & (G < 140) & (B < 80) & (R > G + 40)        # uniques: brown-orange text
        light = (((mx > 170) & (mn > 60)) | teal | green | orange).astype(np.uint8) * 255
        # a drop beam is teal too and touches its label: cut tall teal components out so the label stays a short line
        n_b, lab_b, st_b, _ = cv2.connectedComponentsWithStats(teal.astype(np.uint8))
        tall = [i for i in range(1, n_b) if st_b[i][3] >= 30]
        if tall:
            light[cv2.dilate(np.isin(lab_b, tall).astype(np.uint8), None, iterations=2) > 0] = 0
        light[: int(h * 0.07), :] = 0; light[int(h * 0.86):, :] = 0; light[:, : int(w * 0.12)] = 0; light[:, int(w * 0.84):] = 0
        light[: int(h * 0.13), int(w * 0.30): int(w * 0.70)] = 0      # the boss bar: name and modifiers under the top edge
        light[: int(h * 0.27), int(w * 0.85):] = 0           # the minimap, top right
        light[int(h * 0.27): int(h * 0.45), int(w * 0.80):] = 0   # the quest tracker under it
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 1))
        lines = cv2.dilate(light, k, iterations=2)
        n_l, lab_l = cv2.connectedComponents(light)          # the letters themselves
        out = []
        for b in blobs(lines, min_area=40, max_area=4000):
            if not (18 <= b["w"] <= 220 and 5 <= b["h"] <= 16 and b["w"] > 3.2 * b["h"]):   # a text line; the checkpoint beam is 32x11
                continue
            x0, x1 = b["left"], b["left"] + b["w"]; y0, y1 = b["top"], b["top"] + b["h"]
            ids = set(lab_l[y0:y1, x0:x1].ravel()) - {0}
            if len(ids) < 4:                                  # a highlight streak is one or two pieces; words are many
                continue
            heights = []
            for i in ids:
                ys_i = np.nonzero((lab_l[y0:y1, x0:x1] == i).any(axis=1))[0]
                heights.append(ys_i.max() - ys_i.min() + 1)
            if np.std(heights) > 3.0 or np.mean(heights) < 3:  # letters share a height; gravel speckles do not
                continue
            # the label's own dark box shows just outside the text, above and below it (the wider margin may be snow)
            above = f[max(0, y0 - 3): max(0, y0 - 1), x0:x1].max(axis=2); below = f[min(h, y1 + 1): min(h, y1 + 3), x0:x1].max(axis=2)
            inner = np.concatenate([above.ravel(), below.ravel()])
            if inner.size == 0 or np.median(inner) > 60 or (inner > 110).mean() > 0.25:
                continue
            # the chat log: lines of text at the left edge, all starting in the same column, above the life globe
            if 0.105 * w <= x0 <= 0.125 * w and 0.30 * h < y0 < 0.80 * h:
                continue
            if self.is_zone_label(f, x0, y0, x1, y1):
                continue                                      # a doorway or object label: clicking it changes zone
            box = (light[y0:y1, x0:x1] > 0); n = max(1, int(box.sum()))
            r_, g_, b_ = R[y0:y1, x0:x1][box], G[y0:y1, x0:x1][box], B[y0:y1, x0:x1][box]
            share = lambda m: float(m.sum()) / n
            if share((g_ > 140) & (r_ < 110) & (b_ < 110)) > 0.3:
                tier, kind = 0, "quest"                       # green
            elif share((r_ > 150) & (g_ < 140) & (b_ < 80) & (r_ > g_ + 40)) > 0.3:
                tier, kind = 1, "unique"                      # brown-orange
            elif share((r_ > 200) & (g_ > 200) & (b_ < 170)) > 0.3:
                tier, kind = 2, "rare"                        # yellow
            elif share((g_ > 150) & (b_ > 150) & (r_ < 80)) > 0.3:
                tier, kind = 2, "gem"                         # teal
            elif share((b_ > 200) & (r_ < 180) & (g_ < 180)) > 0.3:
                tier, kind = 3, "magic"                       # blue
            else:
                tier, kind = 4, "normal"                      # white
            out.append({"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"], "quest": tier == 0, "tier": tier, "kind": kind})
        out.sort(key=lambda l: l["tier"])                     # quest, unique, rare/gem, magic, normal
        return out

    def is_zone_label(self, f, x0, y0, x1, y1) -> bool:
        """Does the label box match a known zone/object label (Clearfell, Waypoint, ...)? Template match in grey."""
        if not self.zone_templates:
            return False
        h, w = f.shape[:2]
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        for t in self.zone_templates:
            # a window around the label centre at least as big as the template: a tall template (a two-line label,
            # or one harvested with its margins) must still fit, else the test silently passes the label through
            th, tw = t.shape[:2]
            hh, hw = max(th // 2 + 6, (y1 - y0) // 2 + 4), max(tw // 2 + 8, (x1 - x0) // 2 + 6)
            reg = g[max(0, cy - hh): min(h, cy + hh), max(0, cx - hw): min(w, cx + hw)]
            if reg.shape[0] >= th and reg.shape[1] >= tw:
                if cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED).max() > 0.6:   # same label on a glow scores ~0.65; ground peaks at 0.43
                    return True
        return False

    @staticmethod
    def fiery(f: np.ndarray, b: dict) -> bool:
        """Is this blob a flame? Torches and braziers flicker, which reads as motion: bright with an orange core."""
        box = f[b["top"]: b["top"] + b["h"], b["left"]: b["left"] + b["w"]].astype(int)
        if box.size == 0:
            return False
        orange = (box[..., 0] > 150) & (box[..., 1] > 70) & (box[..., 0] > box[..., 2] + 60)
        return orange.mean() > 0.04

    @staticmethod
    def highlighted(f, x, y, r=40):
        """A hovered monster is outlined red: enough strong-red pixels within r of (x, y). The game also puts the
        hovered monster's name and red life bar at the top centre of the screen; that alone is proof."""
        h, w = f.shape[:2]
        tb = f[int(h * 0.025): int(h * 0.07), int(w * 0.38): int(w * 0.62)].astype(int)
        tred = (tb[..., 0] > 110) & (tb[..., 1] < 60) & (tb[..., 2] < 60) & (tb[..., 0] > tb[..., 1] + 60)
        if tred.size and float(tred.mean(axis=1).max()) > 0.3:      # 0 on 95% of frames, ~0.64 with a target
            return True
        box = f[max(0, int(y - r)): min(h, int(y + r)), max(0, int(x - r)): min(w, int(x + r))].astype(int)
        if box.size == 0:
            return False
        r, g, b = box[..., 0], box[..., 1], box[..., 2]
        # the outline is an anti-aliased pinkish red (about 181,91,86): green and blue alike; flames are orange (b << g)
        red = (r > 120) & (r > g + 70) & (r > b + 70)   # +70: torch-lit ground (155,98,88) stays out, 0/1800 spots
        if int(red.sum()) < 20:
            return False
        return float((g[red] - b[red]).mean()) < 12     # flames lean yellow (g - b about 25); the outline does not

    @staticmethod
    def bar_blobs(f: np.ndarray) -> list[dict]:
        """Enemy health bars: thin, solid, dark-red rectangles with dark pixels above and below (the bar's frame).
        Torch flames and forge glow are red too, but patchy, orange-cored and bright all around."""
        r, g, b = f[..., 0].astype(int), f[..., 1].astype(int), f[..., 2].astype(int)
        m = ((r > 110) & (g < 70) & (b < 80) & (r - g > 60)).astype(np.uint8) * 255
        lum = f.mean(axis=2)
        h, w = m.shape
        out = []
        for bb in blobs(m, min_area=6):
            if not (bb["h"] <= 5 and bb["w"] >= 6 and bb["w"] >= 3 * bb["h"]):
                continue
            x0, y0, x1, y1 = bb["left"], bb["top"], bb["left"] + bb["w"], bb["top"] + bb["h"]
            if (m[y0:y1, x0:x1] > 0).mean() < 0.8:
                continue
            above = lum[max(0, y0 - 3): max(0, y0 - 1), x0:x1]
            below = lum[min(h, y1 + 1): min(h, y1 + 3), x0:x1]
            if above.size and below.size and (above.mean() > 90 or below.mean() > 90):
                continue                      # bright all around: a flame, not a framed bar
            if bb["y"] < 0.14 * h and 0.3 * w < bb["x"] < 0.7 * w and bb["w"] > 30:
                continue                      # the boss bar across the top of the screen is not a target
            out.append(bb)
        return out

    def enemies_now(self, f):
        """Health bars (small wide blobs of the bar colour) and, in motion mode, things that move while the player
        stands at the centre: a monster coming at you is a moving blob away from the centre and the HUD."""
        out = []
        if self.enemy_mode in ("bars", "both"):
            bars = self.bar_blobs(f)
            prev = getattr(self, "bar_prev", [])
            steady = [b for b in bars if any(abs(b["x"] - q["x"]) < 10 and abs(b["y"] - q["y"]) < 8 for q in prev)]
            self.bar_prev = bars              # spell splashes flicker; a health bar stays put from one tick to the next
            out += [dict(b, kind="bar") for b in steady]
        if self.enemy_mode in ("motion", "both"):
            h, w = f.shape[:2]
            mv = self.motion.update(f, thresh=34)
            mv[: int(h * 0.06), :] = 0; mv[int(h * 0.8):, :] = 0; mv[:, : int(w * 0.12)] = 0; mv[:, int(w * 0.84):] = 0
            cy, cx = int(h * self.me_y), w // 2
            mv[cy - 60: cy + 35, cx - 32: cx + 32] = 0          # the player's own animation, bar and head included
            mv = cv2.dilate(mv, None, iterations=2)
            mb = [b for b in blobs(mv, min_area=self.motion_min, max_area=6000)
                  if 0.35 < b["w"] / max(1, b["h"]) < 2.0 and b["h"] >= 10 and not self.fiery(f, b)]
            if len(mb) > 10 or (mv.mean() / 255) > 0.06:   # the whole screen is moving (rain, scrolling): not monsters
                mb = []
            out += [dict(b, kind="motion", h=0) for b in mb]
        return out

    def start(self, view: View):
        # pin the camera: every detector (bar sizes, hover radius, loot label shape) was tuned fully zoomed out,
        # which is the game's default, and a stray wheel notch would quietly shrink every threshold
        if not self.dry and self.zoom_out:
            h, w = view.frame.shape[:2]
            vx, vy = view.to_view(w * 0.5, h * 0.55)
            try:
                view.desk.scroll(int(vx), int(vy), 8)
            except Exception:  # noqa: BLE001
                pass

    def read_control(self, now):
        """Hot-reload strategist orders: {"goal_deg": -38, "take_exits": 0, "nav": "minimap", "heading_deg": 45}."""
        if not self.control or now - self.control_checked < 2.0:
            return
        self.control_checked = now
        try:
            mt = os.path.getmtime(self.control)
            if mt == self.control_mtime:
                return
            self.control_mtime = mt
            with open(self.control) as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            return
        if "pause" in c:
            self.pause_wanted = bool(c["pause"])
        if "goal_deg" in c:
            self.goal_deg = None if c["goal_deg"] is None else float(c["goal_deg"])
        if "take_exits" in c:
            self.take_exits = bool(c["take_exits"])
        if "nav" in c:
            self.nav = str(c["nav"])
        if "move" in c and c["move"] in ("click", "wasd"):
            if self.move == "wasd" and c["move"] == "click":
                self.moving = set()
            self.move = c["move"]
        if "wander_every" in c:
            self.wander_every = float(c["wander_every"])
        if "heading_deg" in c and c.get("nav", self.nav) == "fixed":
            # a fixed heading is an order for nav=fixed only; under the explorer a stale one must not pin us
            self.fixed_heading = self.heading = math.radians(float(c["heading_deg"]))
            self.heading_until = now + 3.0
        elif self.nav != "fixed" and self.heading_until > now + 10:
            self.heading_until = now
        log.info("control: %s", c)

    def step(self, view: View):
        f = view.frame
        h, w = f.shape[:2]
        now = time.time()
        self.read_control(now)
        if self.pause_wanted and not self.paused:
            # hand the desk to the strategist: let go of everything, give the lease back, keep watching
            self._halt(view)
            if self.holding and not self.dry:
                view.desk.mouse_up(); self.holding = False
            if not self.dry:
                view.desk.release_all()
                try:
                    view.desk.release()
                except Exception:  # noqa: BLE001
                    pass
            self.paused = True
            log.info("paused for the strategist")
        elif self.paused and not self.pause_wanted:
            if not self.dry:
                try:
                    view.desk.lease(ttl=1800, note="reflex: arpg")
                except Exception:  # noqa: BLE001
                    pass
            self.paused = False
            self.motion.prev = None
            log.info("resumed")
        if now - self.status_at >= 1.0 and self.control:
            self.status_at = now
            try:
                with open(self.control + ".status", "w") as fh:
                    self.unstick_hist = [t for t in self.unstick_hist if now - t < 60.0]
                    old_life = [l for t, l in self.life_long if now - t > 8.0]
                    old_pos = [(wx, wy) for t, wx, wy in self.world_hist if now - t > 4.0]
                    came = None
                    if old_pos:
                        dx, dy = old_pos[0][0] - self.world[0], old_pos[0][1] - self.world[1]
                        if math.hypot(dx, dy) > 4:
                            came = int(math.degrees(math.atan2(dy, dx)))
                    json.dump({"t": round(now, 1), "life": round(self.life, 2), "enemies": self.enemies,
                               "quiet_s": round(now - self.quiet_since, 1), "exits_seen": self.exits_seen,
                               "paused": self.paused, "bag_full": self.inventory_full, "why": self.why,
                               "life_trend": round(self.life - old_life[0], 2) if old_life else 0.0,
                               "stuck_1m": len(self.unstick_hist), "frontiers": self.frontiers, "exits": self.exit_list,
                               "blocked": [int(math.degrees(a)) for a, _ in self.blocked], "came_from_deg": came,
                               "goal_deg": self.goal_deg, "nav": self.nav, "deaths": self.deaths}, fh)
            except OSError:
                pass
        if self.paused:
            self.why = "paused (strategist has the desk)"
            return
        # the Tab map overlay replaces the minimap with the world (and the strategist may leave it on): toggle it off
        if self.nav == "minimap" and not self.dry:
            x, y, mw, mh = self.minimap
            box = f[y: y + mh, x: x + mw].astype(int)
            navy = ((box[..., 2] > box[..., 0] + 10) & (box[..., 2] >= box[..., 1]) | (box.max(axis=2) < 25)).mean()
            if navy < 0.35:
                self.overlay_ticks = getattr(self, "overlay_ticks", 0) + 1
                if self.overlay_ticks >= 3 and now - getattr(self, "overlay_tap", 0.0) > 5.0:
                    self.overlay_tap = now
                    view.desk.tap("tab", cooldown=5.0)
                    self.why = "map overlay was on: tab"
            else:
                self.overlay_ticks = 0
        if self.debug_dir and now - self.strip_at >= 5.0:
            x, y, mw, mh = self.minimap
            crop = f[y: y + mh, x: x + mw].copy()
            cv2.putText(crop, f"{view.tick}", (2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1)
            self.strip = (self.strip + [crop])[-12:]; self.strip_at = now
            try:
                cv2.imwrite(f"{self.debug_dir}/minimap-strip.jpg", cv2.cvtColor(np.hstack(self.strip), cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_JPEG_QUALITY, 85])
            except cv2.error:
                pass
        if self.nav == "minimap":
            self.track_minimap(f)
        if self.life_box:
            self.life = max(bar_fill(f, self.life_box, self.life_rgb, axis="y"),
                            bar_fill(f, self.life_box, (85, 130, 30), axis="y"))   # poisoned: the globe turns green
            # the mana globe (bottom right) is always full of blue while the game is on screen; the life globe can be empty
            mx_, my_, mw_, mh_ = self.mana_box
            mp = f[my_: my_ + mh_, mx_: mx_ + mw_].astype(int)
            reddish = ((mp[..., 2] > 110) & (mp[..., 2] > mp[..., 0] + 40)).mean() if mp.size else 0.0
            # the death screen keeps the mana globe blue: the banner's gold lettering with an empty life globe is death
            band_ = f[int(h * 0.70): int(h * 0.77), int(w * 0.40): int(w * 0.60)].astype(int)
            banner = ((band_[..., 0] > 150) & (band_[..., 1] > 120) & (band_[..., 2] < 110)).mean() if self.life < 0.5 else 0.0
            if reddish < 0.25 or banner > 0.0007:
                band = f[int(h * 0.70): int(h * 0.77), int(w * 0.40): int(w * 0.60)].astype(int)
                gold_text = ((band[..., 0] > 150) & (band[..., 1] > 120) & (band[..., 2] < 110)).mean()
                if gold_text > 0.0007:                       # the resurrect button's lettering
                    if not self.dead_since:
                        self.dead_since = now
                    elif now - self.dead_since > 1.5 and now - getattr(self, "last_res", 0) > 10:
                        self.dead_since = 0.0
                        self.last_res = now
                        self.deaths += 1
                        vx, vy = view.to_view(w / 2, h * 0.733)
                        if not self.dry:
                            view.desk.click(vx, vy)
                        self.why = f"resurrect ({self.deaths})"
                        time.sleep(4.0)
                    else:
                        self.why = "dead, waiting"
                    return
                self.why = "game not on screen (no life globe)"
                self.world = [0.0, 0.0]; self.visited[:] = 0; self.map_gray = None   # a loading screen: new zone, new memory
                self._halt(view)                  # never keep WASD or the mouse button down over someone else's window
                if self.holding and not self.dry:
                    view.desk.mouse_up()
                    self.holding = False
                return
            # dead: the globe is empty for a while and the game is still on screen -> the resurrect button sits mid-screen
            if self.life < 0.04:
                if not self.dead_since:
                    self.dead_since = now
                elif now - self.dead_since > 2.5:
                    self.dead_since = 0.0
                    self.deaths += 1
                    vx, vy = view.to_view(w / 2, h * 0.733)
                    if not self.dry:
                        view.desk.click(vx, vy)
                    self.why = f"resurrect ({self.deaths})"
                    time.sleep(3.0)
                    return
            else:
                self.dead_since = 0.0
            self.life_hist = [(t, l) for t, l in self.life_hist if now - t < 2.5] + [(now, self.life)]
            older = sorted(l for t, l in self.life_hist if now - t > 0.6)
            base = older[len(older) // 2] if older else self.life          # median: one flashed sample is not a hit
            recent = [l for t, l in self.life_hist[-2:]]
            hurt = len(older) >= 3 and len(recent) == 2 and all(l < base - 0.03 for l in recent)
            # a slow drain (chill, poison, a ghost that never lights up on hover) never trips the two-tick test:
            # life well below where it was a few seconds ago while we have not swung at anything is being hit too
            self.life_long = [(t, l) for t, l in self.life_long if now - t < 10.0] + [(now, self.life)]
            self.attacks_hist = [(t, a) for t, a in self.attacks_hist if now - t < 4.0] + [(now, self.attacks)]
            earlier = [l for t, l in self.life_long if 4.0 < now - t < 9.0]
            idle = self.attacks_hist[0][1] == self.attacks if len(self.attacks_hist) > 3 else False
            if earlier and self.life < max(earlier) - 0.12 and idle and self.life > 0.02:
                hurt = True
        else:
            hurt = False
        if self.before_click is not None and now - self.before_click[0] > 0.7:
            diff = float(np.abs(f.astype(np.int16) - self.before_click[1]).mean())
            self.before_click = None
            if diff > 40:                 # a wander click opened something big (a stash, a waypoint map): close it
                self.panels_closed += 1
                if not self.dry:
                    view.desk.tap("esc")
                self.why = f"closed a panel (frame changed {diff:.0f})"
                return
        enemies = self.enemies_now(f)
        raw = [e for e in enemies if 0.12 * w < e["x"] < 0.84 * w and 0.06 * h < e["y"] < 0.8 * h]
        if hurt and not any(e.get("kind") == "bar" for e in raw):
            self.panic_until = now + 2.0      # being hit by something unseen: swing around until a bar shows
        if self.holding and now >= self.panic_until and not any(0.12 * w < e["x"] < 0.84 * w and 0.06 * h < e["y"] < 0.8 * h for e in enemies):
            self.holding = False
            if not self.dry:
                view.desk.mouse_up()
        # only the play area: not the chat at the left, the quest list at the right, the minimap or the HUD
        enemies = [e for e in enemies if 0.12 * w < e["x"] < 0.84 * w and 0.06 * h < e["y"] < 0.8 * h]
        self.enemies = len(enemies)
        if enemies or self.holding or now < self.panic_until:
            self.quiet_since = now            # the strategist waits for quiet_s to grow before asking for the desk
        if now < self.motion_mute_until:
            enemies = [e for e in enemies if e.get("kind") == "bar"]   # motion has been crying wolf: ignore it a while
        if now < self.panic_until and not any(e.get("kind") == "bar" for e in enemies):
            enemies = []                      # no hovering about while being hit: retaliate below
        if self.holding or self.sweep_hit or now < self.panic_until or any(e.get("kind") == "bar" for e in enemies):
            self._halt(view)                  # stand and fight: a moving warrior cannot swing
        if not enemies and not self.holding and not self.sweep_hit and now >= self.panic_until and not self.dry:
            self._sweep(view, f, w, h)        # keep the cursor circling: the game outlines whatever it passes over
        if self.life_box and self.life < self.life_low and now - self.last_flask > self.flask_cooldown:
            self.last_flask = now
            if not self.dry:
                view.desk.tap(self.flask)
            self.why = f"flask (life {self.life:.0%})"
        elif enemies:
            cx, cy = w / 2, h * self.me_y
            self.rejected = [(px, py, t) for px, py, t in self.rejected if now - t < 6]
            bars = [e for e in enemies if e.get("kind") == "bar"]
            cands = bars or [e for e in enemies
                             if not any(math.hypot(e["x"] - px, e["y"] - py) < 30 for px, py, t in self.rejected)]
            if not cands:
                self.why = "motion, none confirmed"
                enemies = []
            else:
                e = min(cands, key=lambda b: (b["x"] - cx) ** 2 + (b["y"] - cy) ** 2)
                tx, ty = (e["x"], e["y"] + e["h"] + 14) if e.get("kind") == "bar" else (e["x"], e["y"])
                vx, vy = view.to_view(tx, ty)
                if e.get("kind") == "bar":
                    ok = True                                   # a bar means we are already fighting it
                    self.hover_streak = 0.0
                elif self.holding and self.highlighted(f, tx, ty):
                    ok = True                                   # still lit under the held attack
                elif self.hover and math.hypot(self.hover[0] - tx, self.hover[1] - ty) < 30 and now - self.hover[2] > 0.3:
                    ok = self.highlighted(f, tx, ty)            # the cursor has been on it: did it turn red?
                    self.hover_checks = getattr(self, "hover_checks", 0) + 1
                    if self.debug_dir and (ok or self.hover_checks % 6 == 0):   # calibration crops, sampled: a full grab is slow
                        from .vision import Frames as _F
                        full = _F(self.frames_base if hasattr(self, "frames_base") else "http://127.0.0.1:7777", 1.0).grab()
                        fh, fw = full.shape[:2]
                        fx, fy = min(fw - 1, max(0, int(tx / self.scale))), min(fh - 1, max(0, int(ty / self.scale)))
                        crop = np.ascontiguousarray(full[max(0, fy - 90): min(fh, fy + 90), max(0, fx - 90): min(fw, fx + 90)])
                        if crop.size:
                            cv2.imwrite(f"{self.debug_dir}/hover-{view.tick:05d}-{'yes' if ok else 'no'}.png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
                    if ok:
                        self.confirmed += 1
                        self.hover_streak = 0.0
                    else:
                        self.rejected.append((tx, ty, now))
                        if not self.hover_streak:
                            self.hover_streak = now
                        elif now - self.hover_streak > 3.0:
                            self.hover_streak = 0.0
                            self.motion_mute_until = now + 6.0
                            enemies = []
                    self.hover = None
                else:
                    if not self.hover_streak:
                        self.hover_streak = now
                    if now - self.hover_streak > 3.0:          # candidates that keep moving are never checked: mute
                        self.hover_streak = 0.0
                        self.motion_mute_until = now + 6.0
                        self.hover = None
                        ok = False
                        enemies = []
                    else:
                        if not self.dry:
                            view.desk.move(vx, vy)              # hover it and look next tick
                        self.hover = (tx, ty, now)
                        ok = None
                if ok:
                    if not self.dry:
                        if self.attack == "lclick":
                            view.desk.move(vx, vy)
                            if not self.holding:
                                view.desk.mouse_down(vx, vy)
                                self.holding = True
                        else:
                            view.desk.move(vx, vy)
                            view.desk.tap(self.attack_key or "q", cooldown=0.25)
                    self.why = f"attack {len(cands)} ({e.get('kind')}), nearest {math.hypot(e['x'] - cx, e['y'] - cy):.0f}px"
                elif ok is None:
                    self.why = f"hovering candidate at {tx:.0f},{ty:.0f}"
                else:
                    self.why = "hovered, not a monster"
        if enemies and self.why.startswith("attack"):
            self.attacks += 1
            if self.aoe_key and len(enemies) >= 2 and now - self.last_aoe > self.aoe_every and not self.dry:
                self.last_aoe = now
                view.desk.tap(self.aoe_key)          # Earthquake under the pack; the held attack resumes after
            self.why = f"attack {len(enemies)} ({e.get('kind')}), nearest {math.hypot(e['x'] - cx, e['y'] - cy):.0f}px"
        elif now < self.panic_until:
            cx, cy = w / 2, h * self.me_y
            near = [e for e in raw if math.hypot(e["x"] - cx, e["y"] - cy) < 220]
            # ranged attackers stand at the edge of the screen: charge the nearest thing we can see, bars first,
            # at any distance (a held attack walks us to it)
            far = sorted(raw, key=lambda b: (0 if b.get("kind") == "bar" else 1, math.hypot(b["x"] - cx, b["y"] - cy)))
            if near:
                e = min(near, key=lambda b: math.hypot(b["x"] - cx, b["y"] - cy))
                tx, ty = e["x"], e["y"]
                self.last_seen = (tx, ty, now)
            elif far:
                tx, ty = far[0]["x"], far[0]["y"]
                self.last_seen = (tx, ty, now)
            elif now - getattr(self, "last_seen", (0, 0, 0))[2] < 1.5:
                tx, ty = self.last_seen[0], self.last_seen[1]          # swing where the attacker was a moment ago
            else:
                tx = ty = None
                # nothing to swing at: do not stand in it (sky lightning, arrows): step out and look from there
                self.heading = random.uniform(0, 2 * math.pi)
                self.heading_until = now + 1.5
                self._go(view, w, h)
            if tx is not None:
                vx, vy = view.to_view(tx, ty)
                if not self.dry:
                    view.desk.move(vx, vy)
                    if not self.holding:
                        view.desk.mouse_down(vx, vy)
                        self.holding = True
                self.attacks += 1
            elif self.holding and not self.dry:
                view.desk.mouse_up(); self.holding = False
            self.retaliations += 1
            if self.aoe_key and now - self.last_aoe > self.aoe_every and not self.dry:
                self.last_aoe = now
                view.desk.tap(self.aoe_key)          # being hit by something unseen: shake the ground around us
            self.why = (f"retaliate at {tx:.0f},{ty:.0f} ({'seen' if near else 'charge far' if far else 'last seen'})" if tx is not None
                        else "hit by something unseen: step out and look")
        elif self.sweep_hit and now - self.sweep_hit[2] < 1.5:
            sx, sy, _ = self.sweep_hit
            vx, vy = view.to_view(sx, sy)
            if self.highlighted(f, sx, sy):
                if not self.dry:
                    view.desk.move(vx, vy)
                    if not self.holding:
                        view.desk.mouse_down(vx, vy)
                        self.holding = True
                self.attacks += 1
                self.sweep_hit = (sx, sy, now)
                self.why = f"attack (sweep) at {sx:.0f},{sy:.0f}"
            else:
                self.sweep_hit = None
        elif (beams := [bm for bm in (self.beams_now(f) if view.tick % 2 == 0 else [])
                        if not any(math.hypot(bm[0] - px, bm[1] - py) < 60 and now - t < 20 for px, py, t in self.beams_done)]):
            cx, cy = w / 2, h * self.me_y
            bx, by = min(beams, key=lambda bm: (bm[0] - cx) ** 2 + (bm[1] - cy) ** 2)
            if not self.beam_since or now - getattr(self, "beam_seen", 0.0) > 2.0:
                self.beam_since = now                        # a fresh chase, not the tail of a long-lost one
            self.beam_seen = now
            d = math.hypot(bx - cx, by - cy)
            if now - self.beam_since > 10.0:                      # can't get there: leave it for a while
                self.beams_done.append((bx, by, now))
                self.beam_since = 0.0
                self.why = "beam: gave up"
            elif d > 120:
                self.heading = math.atan2((by - cy) / 0.6, bx - cx)   # the screen is foreshortened: undo it for WASD
                self.motion.reset_on_scroll()
                self._go(view, w, h)
                self.why = f"to beam at {bx:.0f},{by:.0f} ({d:.0f}px)"
            else:
                self._halt(view)
                near = [l for l in self.loot_now(f) if math.hypot(l["x"] - bx, l["y"] - by) < 150]
                tx, ty = (near[0]["x"], near[0]["y"]) if near else (bx, by - 12)
                if not self.dry:
                    view.desk.click(*view.to_view(tx, ty))
                self.pickups += 1
                self.beams_done.append((bx, by, now))
                self.beam_since = 0.0
                self.why = f"beam: pick up at {tx:.0f},{ty:.0f} ({'label' if near else 'base'})"
                time.sleep(0.8)
        elif (loot := [l for l in (self.loot_now(f) if view.tick % 2 == 1 and not self.inventory_full else [])
                       if (l["tier"] <= self.loot_max_tier      # white "normal" labels look like place names: skip by default
                           or self._exit_label(l, w, h))        # ... unless it is the door we were told to take
                       and not any(math.hypot(l["x"] - px, l["y"] - py) < 25 and now - t < 10 for px, py, t in self.picked)]):
            # a label clicked 2-6 s ago that is still lying there did not go into the bag
            stale = [(px, py, t) for px, py, t in self.picked if 2.0 < now - t < 6.0
                     and any(math.hypot(l["x"] - px, l["y"] - py) < 25 for l in self.loot_now(f))]
            if stale:
                self.pickup_fails += 1
                self.picked = [(px, py, t) for px, py, t in self.picked if (px, py, t) not in stale] + [(px, py, now + 20) for px, py, t in stale]
                if self.pickup_fails >= 3:
                    self.inventory_full = True
            cx, cy = w / 2, h * self.me_y
            l = min(loot, key=lambda b: b.get("tier", 4) * 10 ** 7 + (b["x"] - cx) ** 2 + (b["y"] - cy) ** 2)
            self.picked = [(px, py, t) for px, py, t in self.picked if now - t < 10] + [(l["x"], l["y"], now)]
            vx, vy = view.to_view(l["x"], l["y"])
            self._halt(view)
            if not self.dry:
                view.desk.click(vx, vy)
            self.pickups += 1
            self.why = f"pick up {l.get('kind', '')} ({len(loot)} labels)"
            time.sleep(0.6)
        elif now - self.last_wander > (min(self.wander_every, 1.0) if now < self.follow_until else self.wander_every):
            self.last_wander = now
            # stuck? the minimap scrolls under the marker whenever we move
            # the minimap is translucent and the world animates through it, so a pixel diff says "moved" while we
            # stand against a tree; the dead-reckoned position (phase correlation of the map itself) does not
            self.world_hist = [(t, wx, wy) for t, wx, wy in self.world_hist if now - t < 6.0] + [(now, *self.world)]
            old = [(wx, wy) for t, wx, wy in self.world_hist if now - t >= 2.0]
            moved = (not old) or any(math.hypot(self.world[0] - wx, self.world[1] - wy) >= 3.0 for wx, wy in old[-1:])
            if moved:
                self.still_since = 0.0
            elif not self.still_since:
                self.still_since = now
            elif now - self.still_since > 3.0:
                self.still_since = 0.0
                self.unsticks += 1
                self.unstick_hist.append(now)
                if self.unstick_key and not self.dry:
                    # first try to break through: Rolling Slam ("q") leaps along the cursor, over small blockers
                    tx = w / 2 + math.cos(self.heading) * 160
                    ty = h * self.me_y + math.sin(self.heading) * 160 * 0.6
                    self._halt(view)
                    view.desk.move(*view.to_view(tx, ty))
                    view.desk.tap(self.unstick_key, cooldown=1.0)
                    time.sleep(0.6)
                # remember the direction that failed, then ask the minimap for the best other way; blind turns only
                # when the minimap has nothing to say (loading, no outline in view)
                self.blocked.append((self.heading, now))
                self.blocked = [(a, t) for a, t in self.blocked if now - t < 30.0]
                recent = [t for t in self.unstick_hist if now - t < 60.0]
                if self.nav in ("minimap", "edge") and (len(recent) >= 3 or self.nav == "edge") and now >= self.follow_until:
                    self.follow_until = now + (1e9 if self.nav == "edge" else 40.0)   # the frontier keeps failing: hug the edge
                    self.follow_side = -self.follow_side
                following = now < self.follow_until
                new = (self.follow_heading(f) if following else
                       self.explore_heading(f) if self.nav == "minimap" else None)
                if following and new is not None:
                    new -= self.follow_side * math.pi / 2              # blocked along the wall: turn away from it, then carry on
                if new is not None:
                    self.heading = new
                    how = "edge" if following else "minimap"
                elif now - getattr(self, "last_unstick", 0.0) < 8.0:
                    self.heading += random.choice((-1, 1)) * (math.pi / 2)        # stuck again at once: go sideways
                    how = "sideways"
                else:
                    self.heading += math.pi + random.uniform(-0.4, 0.4)           # stuck: walk back the way we came
                    how = "about-face"
                self.last_unstick = now
                self.heading_until = now + 6.0
                self._go(view, w, h)
                self.why = f"stuck: slammed, {how} to {math.degrees(self.heading):.0f}"
                return
            if now < self.follow_until:
                a = self.follow_heading(f)
            else:
                a = self.explore_heading(f) if self.nav == "minimap" else (self.fixed_heading if self.nav == "fixed" else None)
            if now < self.follow_until and a is not None:
                if now > self.heading_until:                  # the wall bends: re-aim every wander tick, no jitter
                    self.heading = a
            elif self.nav == "fixed":
                if now > self.heading_until:                  # an unstick turn holds for a few seconds first
                    self.heading = self.fixed_heading + random.uniform(-0.25, 0.25)
            elif a is not None and now > self.heading_until:
                self.heading = a + random.uniform(-0.35, 0.35)
                self.heading_until = now + 4.0
            elif a is None:
                self.heading += random.uniform(-0.9, 0.9)
            if self.move == "click":
                self.before_click = (now, f.astype(np.int16))
            self.motion.reset_on_scroll()      # the camera is about to scroll: do not read that as monsters
            self._go(view, w, h)
            mode = ("along the edge (wall %s, gap %s)" % ("right" if self.follow_side > 0 else "left",
                    "?" if self.wall_gap is None else f"{self.wall_gap:.0f}")) if now < self.follow_until else \
                   ("by minimap" if self.nav == "minimap" else "")
            self.why = f"wander {mode} heading {math.degrees(self.heading):.0f}"
        if self.debug_dir and view.tick % 4 == 0:
            img = cv2.cvtColor(f.copy(), cv2.COLOR_RGB2BGR)
            if view.tick % 16 == 0:        # an unannotated frame too, for scoring detectors later
                cv2.imwrite(f"{self.debug_dir}/clean-{view.tick:05d}.jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            for e in enemies:
                cv2.rectangle(img, (e["left"], e["top"]), (e["left"] + e["w"], e["top"] + e["h"]), (0, 0, 255), 2)
            for l in self.loot_now(f):
                cv2.circle(img, (int(l["x"]), int(l["y"])), 10, (0, 255, 255), 2)
            if self.life_box:
                x, y, bw, bh = self.life_box
                cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 255, 0), 1)
            cv2.putText(img, self.why, (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
            cv2.imwrite(f"{self.debug_dir}/arpg-{view.tick:05d}.jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])

    def _exit_label(self, l, w, h) -> bool:
        """A white label in the direction of the exit icon we are walking to, while that icon is close: the zone's
        name over the doorway. Clicking it is how a transition that does not trigger by walking gets taken."""
        if not (self.take_exits and self.exit_dir) or self.exit_dir[1] > 30:
            return False
        a = math.atan2((l["y"] - h * self.me_y) / 0.7, l["x"] - w / 2)
        return math.cos(a - self.exit_dir[0]) > 0.5 and math.hypot(l["x"] - w / 2, l["y"] - h * self.me_y) < 400

    def _sweep(self, view: View, f, w, h):
        """A corkscrew around the character, one step per tick: starts tight (40 px, where a monster standing on us
        would be) and opens a little every step to 170 px over four turns, then starts again."""
        cx, cy = w / 2, h * self.me_y
        prev = getattr(self, "sweep_prev", None)
        if prev and self.highlighted(f, prev[0], prev[1]):
            self.sweep_hit = (prev[0], prev[1], time.time())
            self.why = f"sweep lit {prev[0]:.0f},{prev[1]:.0f}"
            return
        self.sweep_i += 1
        turns = 4; per = 24
        k = self.sweep_i % (turns * per)
        ang = k * 2 * math.pi / per
        rad = 40 + 130 * k / (turns * per - 1)
        sx, sy = cx + math.cos(ang) * rad, cy + math.sin(ang) * rad * 0.7
        view.desk.move(*view.to_view(sx, sy))
        self.sweep_prev = (sx, sy)

    def _go(self, view: View, w: float, h: float):
        """Walk along self.heading: a ground click (click mode) or a held key chord (wasd mode)."""
        if self.dry:
            return
        if self.move == "wasd":
            dx, dy = math.cos(self.heading), math.sin(self.heading)
            keys = set()
            if abs(dx) > 0.38:
                keys.add("d" if dx > 0 else "a")
            if abs(dy) > 0.38:
                keys.add("s" if dy > 0 else "w")
            if keys != self.moving:
                # the explorer re-aims every tick and a chord that flips each time reads as tapping: once a chord is
                # down, keep it at least a second unless we are coming to a halt
                if self.moving and keys and time.time() - self.chord_at < 1.0:
                    return
                self.moving = keys
                self.chord_at = time.time()
                view.desk.set_held(keys)
            return
        tx = w / 2 + math.cos(self.heading) * self.wander * self.scale
        ty = h * self.me_y + math.sin(self.heading) * self.wander * self.scale * 0.6
        view.desk.click(*view.to_view(tx, ty))

    def _halt(self, view: View):
        if self.moving and not self.dry:
            self.moving = set()
            view.desk.set_held(set())

    def stop(self, view: View):
        self.moving = set()
        if self.holding and not self.dry:
            view.desk.mouse_up()
            self.holding = False
        view.desk.release_all()       # no Escape here: in this game it opens the pause menu

    def status(self) -> str:
        return f"{'BAG FULL ' if self.inventory_full else ''}enemies={self.enemies} life={self.life:.0%} attacks={self.attacks} retal={self.retaliations} confirmed={self.confirmed} unsticks={self.unsticks} deaths={self.deaths} {self.why}"
