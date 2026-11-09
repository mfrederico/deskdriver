"""Cheap vision over the desk's raw frames: colour blobs, templates, motion, bars. OpenCV where it helps, numpy otherwise."""
import math
import time
import urllib.request

import cv2
import numpy as np


class Frames:
    """Pulls the latest frame from deskd as an RGB numpy array. `scale` 1, 0.5 or 0.25 of the view."""

    def __init__(self, base: str = "http://127.0.0.1:7777", scale: float = 0.5):
        self.base = base
        self.scale = scale
        self.last_id = -1
        self.size = (0, 0)

    def grab(self) -> np.ndarray:
        r = urllib.request.urlopen(f"{self.base}/raw?scale={self.scale}", timeout=2)
        data = r.read()
        w, h = map(int, r.headers["X-Desk-Image-Size"].split("x"))
        self.last_id = int(r.headers.get("X-Desk-Frame", -1))
        self.size = (w, h)
        return np.frombuffer(data, np.uint8).reshape(h, w, 3)

    def to_view(self, x: float, y: float) -> tuple[int, int]:
        """A point in this frame's pixels -> the desk's view pixels (what clicks and moves take)."""
        return round(x / self.scale), round(y / self.scale)


def mask_rgb(frame: np.ndarray, rgb, tol: int = 40) -> np.ndarray:
    """Pixels within `tol` of a colour on every channel (uint8 0/255 mask)."""
    c = np.asarray(rgb, dtype=np.int16)
    d = np.abs(frame.astype(np.int16) - c).max(axis=2)
    return ((d <= tol) * 255).astype(np.uint8)


def mask_hsv(frame: np.ndarray, h_lo: int, h_hi: int, s_min: int = 80, v_min: int = 120) -> np.ndarray:
    """Pixels whose hue (0-179, OpenCV scale) lies in [h_lo, h_hi] (wrapping allowed) with enough saturation and value."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    hue = (h >= h_lo) & (h <= h_hi) if h_lo <= h_hi else (h >= h_lo) | (h <= h_hi)
    return ((hue & (s >= s_min) & (v >= v_min)) * 255).astype(np.uint8)


def blobs(mask: np.ndarray, min_area: int = 6, max_area: int = 10 ** 9) -> list[dict]:
    """Connected components of a mask as [{x, y, area, w, h}] (centroids), largest first."""
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if min_area <= a <= max_area:
            out.append({"x": float(cents[i][0]), "y": float(cents[i][1]), "area": a,
                        "w": int(stats[i, cv2.CC_STAT_WIDTH]), "h": int(stats[i, cv2.CC_STAT_HEIGHT]),
                        "left": int(stats[i, cv2.CC_STAT_LEFT]), "top": int(stats[i, cv2.CC_STAT_TOP])})
    out.sort(key=lambda b: -b["area"])
    return out


def find_template(frame: np.ndarray, template: np.ndarray, threshold: float = 0.8) -> tuple[float, int, int] | None:
    """Best match of a small RGB template: (score, x, y) of its top-left, or None under the threshold."""
    res = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return (float(mx), int(loc[0]), int(loc[1])) if mx >= threshold else None


def bar_fill(frame: np.ndarray, box: tuple[int, int, int, int], rgb, tol: int = 60, axis: str = "x") -> float:
    """How full a HUD bar is (0..1): the share of columns (axis x) or rows (axis y) in `box` holding the bar colour."""
    x, y, w, h = box
    m = mask_rgb(frame[y:y + h, x:x + w], rgb, tol) > 0
    if axis == "x":
        cols = m.any(axis=0)
        return float(cols.mean()) if cols.size else 0.0
    rows = m.any(axis=1)
    return float(rows.mean()) if rows.size else 0.0


class Motion:
    """Frame-to-frame change: where things moved, and how much."""

    def __init__(self):
        self.prev = None

    def reset_on_scroll(self):
        """The camera is about to scroll: forget the last frame so the scroll is not read as motion."""
        self.prev = None

    def update(self, frame: np.ndarray, thresh: int = 28) -> np.ndarray:
        g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        if self.prev is None:
            self.prev = g
            return np.zeros_like(g)
        d = cv2.absdiff(g, self.prev)
        self.prev = g
        return ((d > thresh) * 255).astype(np.uint8)


class CamMotion(Motion):
    """Motion with the camera's scroll and zoom taken out, so things that move on their own show up while the
    player walks. ORB features on a half-size grey give a similarity (shift + zoom) by RANSAC; the previous frame
    is warped onto this one, and a pixel counts only if it lies outside the min..max of its warped neighbourhood
    (the neighbourhood widens with scroll speed: tall trees and cliffs shift more than the ground)."""

    def __init__(self, area=(0.12, 0.06, 0.84, 0.8)):
        super().__init__()
        self.area = area                  # play area as fractions (x0, y0, x1, y1): the HUD never moves
        self.orb = cv2.ORB_create(600, scaleFactor=1.3, nlevels=4, edgeThreshold=15, patchSize=15, fastThreshold=8)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.prev_feats = None
        self.shift = (0.0, 0.0)           # last camera scroll in frame pixels
        self.inliers = 0

    def reset_on_scroll(self):
        """Plain Motion forgets its frame when the camera scrolls; this one is built for scrolling."""

    def _feats(self, g):
        sm = cv2.resize(g, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        h, w = sm.shape
        x0, y0, x1, y1 = self.area
        m = np.zeros_like(sm)
        m[int(h * y0): int(h * y1), int(w * x0): int(w * x1)] = 255
        k, d = self.orb.detectAndCompute(sm, m)
        return np.float32([p.pt for p in k]) * 2, d

    def update(self, frame: np.ndarray, thresh: int = 34) -> np.ndarray:
        g = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY), (5, 5), 0)
        feats = self._feats(g)
        prev, pf = self.prev, self.prev_feats
        self.prev, self.prev_feats = g, feats
        if prev is None or pf is None or pf[1] is None or feats[1] is None or prev.shape != g.shape:
            return np.zeros_like(g)
        ms = self.bf.match(pf[1], feats[1])
        if len(ms) < 25:
            self.inliers = 0
            return np.zeros_like(g)
        A = pf[0][[m.queryIdx for m in ms]]
        B = feats[0][[m.trainIdx for m in ms]]
        M, inl = cv2.estimateAffinePartial2D(A, B, method=cv2.RANSAC, ransacReprojThreshold=2.0)
        self.inliers = int(inl.sum()) if inl is not None else 0
        if M is None or self.inliers < 25:
            return np.zeros_like(g)       # no trustworthy camera fit (fades, menus): see nothing rather than everything
        h, w = g.shape
        self.shift = (float(M[0, 2]), float(M[1, 2]))
        warped = cv2.warpAffine(prev, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
        k = 5 + 2 * int(math.hypot(*self.shift) * 0.12)
        ker = np.ones((k, k), np.uint8)
        lo, hi = cv2.erode(warped, ker), cv2.dilate(warped, ker)
        out = np.maximum(cv2.subtract(lo, g), cv2.subtract(g, hi))
        return ((out > thresh // 2) * 255).astype(np.uint8)


class Track:
    """A short history of one thing's position, for its velocity and heading."""

    def __init__(self, n: int = 6):
        self.pts: list[tuple[float, float, float]] = []
        self.n = n

    def add(self, x: float, y: float):
        self.pts.append((x, y, time.time()))
        self.pts = self.pts[-self.n:]

    def velocity(self) -> tuple[float, float]:
        if len(self.pts) < 2:
            return 0.0, 0.0
        (x0, y0, t0), (x1, y1, t1) = self.pts[0], self.pts[-1]
        dt = max(1e-3, t1 - t0)
        return (x1 - x0) / dt, (y1 - y0) / dt

    @property
    def last(self):
        return self.pts[-1][:2] if self.pts else None
