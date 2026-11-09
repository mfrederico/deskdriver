"""Watch a person play: save frames and the on-screen cursor track, for calibrating detectors and for behaviour cloning.

    desk-watch DIR [--fps 5] [--seconds 300]

Writes DIR/frames/NNNNN.jpg (half-scale RGB) and DIR/track.csv with t, frame, cursor x, cursor y (view pixels, or -1),
plus the pixels under the cursor, so a later pass can answer "what was under the cursor when it turned red?".
The cursor is found by its colour (the game's pink pointer) in the frame the daemon shares.
"""
import argparse
import csv
import os
import sys
import time

import cv2
import numpy as np

from .vision import Frames, blobs, mask_hsv


def find_cursor(f: np.ndarray):
    """The pink/magenta pointer: a small saturated blob. Returns (x, y) in frame pixels or None."""
    m = mask_hsv(f, 140, 165, s_min=120, v_min=150)
    bs = [b for b in blobs(m, min_area=15, max_area=900) if b["w"] < 40 and b["h"] < 40]
    if not bs:
        return None
    b = bs[0]
    return b["left"], b["top"]        # the hotspot is the pointer's top-left corner


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk-watch")
    ap.add_argument("dir")
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--url", default=os.environ.get("DESK_URL", "http://127.0.0.1:7777"))
    a = ap.parse_args(argv)
    os.makedirs(os.path.join(a.dir, "frames"), exist_ok=True)
    F = Frames(a.url, 0.5)
    t0 = time.time()
    n = 0
    with open(os.path.join(a.dir, "track.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "frame", "cx", "cy", "r", "g", "b", "red_near"])
        while time.time() - t0 < a.seconds:
            tick = time.time()
            f = F.grab()
            c = find_cursor(f)
            if c:
                x, y = c
                px = f[min(f.shape[0] - 1, y + 2), min(f.shape[1] - 1, x + 2)]
                box = f[max(0, y - 40): y + 40, max(0, x - 40): x + 40].astype(int)
                red = int(((box[..., 0] > 140) & (box[..., 1] < 70) & (box[..., 2] < 70)).sum())
                w.writerow([round(tick - t0, 3), n, x * 2, y * 2, int(px[0]), int(px[1]), int(px[2]), red])
            else:
                w.writerow([round(tick - t0, 3), n, -1, -1, 0, 0, 0, 0])
            cv2.imwrite(os.path.join(a.dir, "frames", f"{n:05d}.jpg"), cv2.cvtColor(f, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 80])
            n += 1
            if n % 50 == 0:
                fh.flush()
                print(f"{n} frames, {time.time() - t0:.0f}s", file=sys.stderr)
            time.sleep(max(0, 1.0 / a.fps - (time.time() - tick)))
    print(f"saved {n} frames to {a.dir}")


if __name__ == "__main__":
    main()
