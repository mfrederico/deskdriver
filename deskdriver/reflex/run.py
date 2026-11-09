"""desk-reflex: run a reflex policy against the desk.  desk-reflex comet --seconds 60 [--debug DIR] [--dry]"""
import argparse
import logging
import os
import sys

from .runner import run


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk-reflex", description="fast game-playing loops on top of deskd")
    ap.add_argument("game", choices=["comet", "arpg", "generic"])
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--url", default=os.environ.get("DESK_URL", "http://127.0.0.1:7777"))
    ap.add_argument("--agent", default=os.environ.get("DESK_AGENT", "reflex"))
    ap.add_argument("--debug", help="directory for annotated frames")
    ap.add_argument("--dry", action="store_true", help="see but never press anything")
    ap.add_argument("--spec", help="generic: JSON spec file")
    ap.add_argument("--stop-file", help="stop when this file appears")
    ap.add_argument("--set", action="append", default=[], help="policy parameter, e.g. --set look=1.2")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    params = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v
    if a.debug:
        os.makedirs(a.debug, exist_ok=True)
    if a.game == "comet":
        from .comet import CometTag
        policy = CometTag(debug_dir=a.debug, dry=a.dry, **params)
    elif a.game == "arpg":
        from .arpg import ClickToMove
        policy = ClickToMove(debug_dir=a.debug, dry=a.dry, **params)
    else:
        from .generic import Generic
        if not a.spec:
            sys.exit("generic needs --spec file.json")
        policy = Generic(a.spec, debug_dir=a.debug, dry=a.dry)
    result = run(policy, seconds=a.seconds, base=a.url, agent=a.agent, stop_file=a.stop_file)
    print(result, policy.status())


if __name__ == "__main__":
    main()
