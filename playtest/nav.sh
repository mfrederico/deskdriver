#!/bin/bash
# usage: nav.sh ["quest context for the strategist"]
HERE=$(cd "$(dirname "$0")" && pwd)
exec "$HERE/../.venv/bin/desk-strategist" "$HERE/control.json" loop --model tev1:0.8b --every 12 --context "${1:-Quest: collect the 2 remaining Runes of Power from Obelisks of Rust somewhere in this zone; one was already taken near the entrance. The exit back to the Grelwood is not wanted.}"
