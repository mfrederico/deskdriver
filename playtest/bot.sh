#!/bin/bash
# usage: bot.sh RUNNAME  (starts the reflex bot with the live control file; debug frames go to $DESK_RUNS/RUNNAME)
HERE=$(cd "$(dirname "$0")" && pwd)
RUNS=${DESK_RUNS:-$HERE/runs}
mkdir -p "$RUNS/$1"
exec "$HERE/../.venv/bin/desk-reflex" arpg --seconds 2400 --debug "$RUNS/$1" --set move=wasd --set wander_every=2.5 --set life_low=0.6 --set goal_deg=90 --set control="$HERE/control.json"
