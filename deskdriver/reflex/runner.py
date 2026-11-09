"""The loop: grab a frame, let the policy act, keep the rate, log, stop cleanly."""
import logging
import os
import signal
import time

from .control import Desk
from .vision import Frames

log = logging.getLogger("reflex")


class View:
    """What a policy gets each tick: the frame, the frame grabber (for coordinate conversion), the input, the clock."""

    def __init__(self, frames: Frames, desk: Desk):
        self.frames = frames
        self.desk = desk
        self.frame = None
        self.t = 0.0
        self.tick = 0
        self.notes: dict = {}

    def to_view(self, x, y):
        return self.frames.to_view(x, y)


class Policy:
    """Subclass this. `step(view)` runs every tick; set `self.done = True` to stop the loop."""
    name = "policy"
    hz = 20.0
    scale = 0.5

    def __init__(self):
        self.done = False

    def start(self, view: View):
        pass

    def step(self, view: View):
        raise NotImplementedError

    def stop(self, view: View):
        pass

    def status(self) -> str:
        return ""


def run(policy: Policy, seconds: float = 60, base: str = "http://127.0.0.1:7777", agent: str = "reflex",
        stop_file: str | None = None, log_every: float = 2.0) -> dict:
    frames = Frames(base, policy.scale)
    desk = Desk(base, agent)
    view = View(frames, desk)
    desk.lease(ttl=max(30, seconds + 30), note=f"reflex: {policy.name}")
    stopping = {"now": False}
    signal.signal(signal.SIGINT, lambda *a: stopping.__setitem__("now", True))
    signal.signal(signal.SIGTERM, lambda *a: stopping.__setitem__("now", True))
    period = 1.0 / policy.hz
    t0 = time.time()
    ticks = 0
    late = 0
    last_log = t0
    try:
        view.frame = frames.grab()
        view.t = 0.0
        policy.start(view)
        while not policy.done and not stopping["now"] and time.time() - t0 < seconds:
            if stop_file and os.path.exists(stop_file):
                break
            tick_start = time.time()
            view.frame = frames.grab()
            view.t = tick_start - t0
            view.tick = ticks
            policy.step(view)
            ticks += 1
            spent = time.time() - tick_start
            if spent > period:
                late += 1
            else:
                time.sleep(period - spent)
            if time.time() - last_log >= log_every:
                last_log = time.time()
                log.info("t=%5.1fs tick=%d late=%d %s", view.t, ticks, late, policy.status())
    finally:
        try:
            policy.stop(view)
        finally:
            desk.release_all()
            desk.release()
    return {"seconds": round(time.time() - t0, 1), "ticks": ticks, "late": late, "hz": round(ticks / max(0.1, time.time() - t0), 1)}
