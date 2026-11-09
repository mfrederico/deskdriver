"""Strategist-side helpers for the pause handshake with a running `desk-reflex arpg --set control=FILE`.

    desk-strategist FILE pause [--quiet 4] [--timeout 60]   wait for a quiet moment, ask the bot to pause, wait until it has
    desk-strategist FILE resume                             hand the desk back
    desk-strategist FILE status                             print the bot's last status line
    desk-strategist FILE clip                               the game clipboard (hover an item, ctrl+c first): item text
    desk-strategist FILE loop [--model tev1:0.8b] [--every 7] [--context "zone/quest text"]
        a local navigator: every few seconds (or on a stuck burst / life drop) ask Ollama's typed-decision endpoint
        (/v1/systemone) which frontier to walk, and write the answer into FILE for the running bot.

The bot writes FILE.status every second: {"life", "enemies", "quiet_s", "paused", "bag_full", "why"}.
"""
import argparse
import json
import os
import sys
import time
import urllib.request


def _read(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write(path, **changes):
    c = _read(path)
    c.update(changes)
    if c.get("nav") != "fixed":
        c.pop("heading_deg", None)          # a retreat heading must not outlive the retreat
    with open(path, "w") as fh:
        json.dump(c, fh)


def _compass(deg):
    names = ["right", "down-right", "down", "down-left", "left", "up-left", "up", "up-right"]
    return names[int(((deg + 22.5) % 360) // 45)]


def _ask(host, model, situation, questions, timeout=60):
    body = {"model": model, "state": {"situation": situation}, "questions": questions, "keep_alive": 600}
    req = urllib.request.Request(host + "/v1/systemone", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d.get("answers", d)


def loop(a, st):
    """The navigator loop: text features from the bot -> one typed question -> goal written to the control file."""
    last_ask = 0.0; last_stuck = None; last_life = None; retreat_until = 0.0
    current = None            # (criterion key, deg) of the goal in force, so a timid model does not flip-flop
    from .gamelog import GameLog
    glog = GameLog(character=a.character) if a.gamelog else None
    seen_events = 0
    print(f"navigator: {a.model} via {a.ollama}, every {a.every:.0f}s; context: {a.context or '(none)'}", flush=True)
    while True:
        time.sleep(1.0)
        s = _read(st)
        now = time.time()
        if glog:
            glog.poll()
            if len(glog.events) != seen_events:
                for e in glog.events[seen_events - len(glog.events):] if seen_events < len(glog.events) else glog.events[-1:]:
                    print(f"{time.strftime('%H:%M:%S')} game log: {e}", flush=True)
                seen_events = len(glog.events)
        if not s or now - s.get("t", 0) > 4.0 or s.get("paused"):
            continue
        why = ""
        if last_stuck is not None and s.get("stuck_1m", 0) >= last_stuck + 3:
            why = "stuck burst"
        elif s.get("life_trend", 0) <= -0.15 and s.get("enemies", 0) == 0 and now - last_ask > 3:
            why = "losing life to nothing visible"
        elif now - last_ask >= a.every:
            why = f"timer ({a.every:.0f}s)"
        last_stuck = s.get("stuck_1m", 0); last_life = s.get("life", 1)
        if retreat_until and now > retreat_until:
            _write(a.control, nav="minimap"); retreat_until = 0.0
        if not why:
            continue
        last_ask = now
        fr = s.get("frontiers") or []
        crit = {}
        for i, f in enumerate(fr):
            crit[f"frontier{i}"] = (f"Walk {_compass(f['deg'])} ({f['deg']} deg): edge of the revealed map, "
                                    f"{'lots of' if f['novelty'] >= 8 else 'some' if f['novelty'] >= 4 else 'little'} new ground "
                                    f"(novelty {f['novelty']}), {f['px']} px away.")
        came = s.get("came_from_deg")
        if came is not None:
            crit["retrace"] = f"Go back the way we came ({_compass(came)}, {came} deg): known ground, safe, nothing new."
        for i, e in enumerate(s.get("exits") or []):
            crit[f"exit{i}"] = f"Take the zone exit {_compass(e['deg'])} ({e['deg']} deg), {e['px']} px: leaves this zone."
        if not crit:
            continue
        cur_key = None
        if current is not None and why.startswith("timer"):
            # the option closest to the goal in force gets named as such: switching needs a clear margin
            for k, f in zip([f"frontier{i}" for i in range(len(fr))], fr):
                if abs((f["deg"] - current[1] + 180) % 360 - 180) <= 30:
                    cur_key = k; crit[k] = crit[k] + " This is the current heading; keep it unless another is clearly better."
        sit = (f"{glog.summary() + ' ' if glog else ''}{a.context} Life {int(s.get('life', 0) * 100)}% ({'falling ' + str(int(-s.get('life_trend', 0) * 100)) + '% in 10 s' if s.get('life_trend', 0) < -0.05 else 'steady'}). "
               f"{s.get('enemies', 0)} enemies visible, quiet for {int(s.get('quiet_s', 0))} s, stuck {s.get('stuck_1m', 0)} times in the last minute, "
               f"{s.get('deaths', 0)} deaths this run. Blocked directions recently: {s.get('blocked') or 'none'}. "
               "Screen angles: 0 right, 90 down, 180 left, -90 up. The bot fights whatever it meets; you only choose where it walks.")
        q = {"goal": {"type": "choice", "instructions": "Pick where to walk next to reach the quest objective and reveal new ground without dying. Never take a zone exit unless the context says the objective is in another zone.", "criteria": crit},
             "danger": {"type": "score", "instructions": "How dangerous is the situation right now?", "criteria": ["Safe", "Cautious", "Dangerous", "Critical"]}}
        t0 = time.time()
        try:
            ans = _ask(a.ollama, a.model, sit, q)
        except Exception as e:  # noqa: BLE001
            print(f"{time.strftime('%H:%M:%S')} ask failed: {str(e)[:120]}", flush=True)
            continue
        g = (ans.get("goal") or {}).get("choice"); conf = (ans.get("goal") or {}).get("confidence", 0)
        danger = (ans.get("danger") or {}).get("score", 0)
        probs = (ans.get("goal") or {}).get("probabilities") or {}
        if cur_key and g != cur_key and probs.get(g, 0) < probs.get(cur_key, 0) + 0.10:
            print(f"{time.strftime('%H:%M:%S')} {why}: {g} ({probs.get(g, 0):.0%}) not clearly better than {cur_key} ({probs.get(cur_key, 0):.0%}): keeping", flush=True)
            continue
        orders = {}
        if g and g.startswith("frontier"):
            orders = {"nav": "minimap", "goal_deg": fr[int(g[8:])]["deg"], "take_exits": 0}
        elif g == "retrace" and came is not None:
            orders = {"nav": "fixed", "heading_deg": came, "goal_deg": came, "take_exits": 0}; retreat_until = now + 5
        elif g and g.startswith("exit"):
            orders = {"nav": "minimap", "goal_deg": (s.get("exits") or [])[int(g[4:])]["deg"], "take_exits": 1}
        if danger >= 2.5 and came is not None and s.get("enemies", 0) == 0:
            orders = {"nav": "fixed", "heading_deg": came, "goal_deg": came, "take_exits": 0}; retreat_until = now + 5
        if orders:
            _write(a.control, **orders)
            current = (g, orders.get("goal_deg"))
        print(f"{time.strftime('%H:%M:%S')} {why}: {g} ({conf:.0%}) danger {danger:.1f} -> {orders} [{time.time() - t0:.1f}s]", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk-strategist")
    ap.add_argument("control")
    ap.add_argument("cmd", choices=["pause", "resume", "status", "clip", "loop"])
    ap.add_argument("--model", default="tev1:0.8b")
    ap.add_argument("--every", type=float, default=7.0)
    ap.add_argument("--context", default="", help="what the strategist knows: zone, quest, where the goal lies")
    ap.add_argument("--gamelog", type=int, default=1, help="read PoE2's Client.txt for zone/level/deaths/NPC lines")
    ap.add_argument("--character", default="DeskReflexNine")
    ap.add_argument("--ollama", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--url", default=os.environ.get("DESK_URL", "http://127.0.0.1:7777"))
    ap.add_argument("--quiet", type=float, default=4.0, help="seconds without enemies before asking")
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args(argv)
    st = a.control + ".status"
    if a.cmd == "loop":
        return loop(a, st)
    if a.cmd == "clip":
        # the portal clipboard read fails with EAGAIN now and then: try a few times
        for _ in range(8):
            try:
                r = json.loads(urllib.request.urlopen(a.url + "/clipboard", timeout=5).read())
                if r.get("ok"):
                    print(r.get("text", ""))
                    return 0
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        print("clipboard not readable", file=sys.stderr)
        return 1
    if a.cmd == "status":
        print(json.dumps(_read(st)))
        return 0
    if a.cmd == "resume":
        _write(a.control, pause=False)
        print("resume requested")
        return 0
    t0 = time.time()
    asked = False
    while time.time() - t0 < a.timeout:
        s = _read(st)
        fresh = s and time.time() - s.get("t", 0) < 3.0
        if not fresh:
            print("no live bot (status stale): nothing to pause", file=sys.stderr)
            return 0
        if s.get("paused"):
            print(f"paused: life={s.get('life')} enemies={s.get('enemies')}")
            return 0
        if not asked and s.get("quiet_s", 0) >= a.quiet and s.get("enemies", 0) == 0 and s.get("life", 1) >= 0.7:
            _write(a.control, pause=True)
            asked = True
        time.sleep(0.5)
    print("timed out waiting for a quiet moment", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
