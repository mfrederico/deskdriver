#!/bin/bash
for p in $(pgrep -f "bin/desk-refle[x]") $(pgrep -f "bin/desk-strategis[t]"); do kill "$p" 2>/dev/null; done
sleep 1; curl -s -X POST localhost:7777/release -d '{"agent":"reflex"}' >/dev/null; echo stopped
