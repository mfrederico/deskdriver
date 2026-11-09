# Path of Exile 2 playtest: how to revive it

State as of 2026-11-07 12:12 (parked)
- Character: DeskReflexNine, Warrior level 6, Standard. Game closed normally at 12:11 (last menu click hit Exit).
- Where: The Red Vale (Act 1, monster level 5), far east edge. Waypoints lit: Clearfell Encampment, Clearfell,
  Grelwood, Red Vale. Quest "Secrets in the Dark": Una summoned at the Tree of Souls; 1 of 3 Runes of Power taken;
  quest log says "Investigate the Obelisks of Rust". Next: explore the Red Vale for the other two obelisks.
- Gear: set I Carrion Mangler 2H mace (Bleed on Mace Strike), set II Heavy Smithing Hammer + buckler (X swaps).
  Passives: 3x Melee Damage, Armour. Bag nearly empty (spare club, Heated Smithing Hammer to sell).
- Deaths today: 6, mostly to unseen ranged/storm damage in the Red Vale. A flee rule is the next defensive fix.
- Last session ended because the character went invisible and could not move after a Rolling Slam unstick into
  terrain (network was fine: 25 ms, no loss). Respawn at checkpoint did not clear it; relogging will.

Resume in one go (debug runs in playtest/runs, or $DESK_RUNS; game in front, logged in):
   playtest/bot.sh RUNNAME > RUNNAME.log &   # reflex bot with --set control=playtest/control.json, move=wasd
   playtest/nav.sh >> navigator.log &          # tev1:0.8b navigator + Client.txt game log
   playtest/stopall.sh                         # stop both and free the lease (never pkill by pattern: it kills the shell)

Start everything
1. Desk daemon (tray should show "desk ready"):  `desk status`   ; if not ready: `desk start` or `desk reconnect`
   (a Share dialog appears once; click Share).
2. Game: `xdg-open steam://rungameid/2694490`, Log In (Steam), pick DeskReflexNine, Play.
3. Make sure the GAME is the front window. Never run the bot while a terminal is in front: terminal text reads as
   loot labels and the bot clicks it.
4. Run the reflex bot (from this directory):
   S=/tmp/poe-run; mkdir -p $S
   .venv/bin/desk-reflex arpg --seconds 900 --debug $S/run1 --set move=wasd --set wander_every=1.5 --set life_low=0.6
   Useful extras: `--set nav=fixed --set heading_deg=N` to force a direction for a while (screen angle, 0 = right,
   90 = down). Stop with Ctrl-C; if killed hard, free the lease: `curl -X POST localhost:7777/release -d '{"agent":"reflex"}'`.
5. Watch the status line: BAG FULL means a strategist must sell/equip/drop; "game not on screen" means the game is
   not the front window.

Pause handshake (no more kill/restart for chores)
   desk-strategist $S/control.json pause      # waits for a quiet moment, bot halts + releases the lease
   ... take the lease, do the chore, release ...
   desk-strategist $S/control.json resume     # bot re-leases and carries on
   desk-strategist $S/control.json status     # the bot's status line (life, enemies, quiet_s, paused, bag_full)
   The running bot must have been started with --set control=$S/control.json (it writes control.json.status).

Local navigator (tev via Ollama, 2026-11-07)
   .venv/bin/desk-strategist $S/control.json loop --model tev1:0.8b --every 7 \
       --context "Zone: The Red Vale (lvl 5). Quest: collect 2 more Runes of Power from Obelisks of Rust in this zone."
   Reads control.json.status (frontiers, exits, blocked, life trend, stuck count, came_from) every second, asks
   Ollama's /v1/systemone one typed question (goal: frontier/retrace/exit; danger: score) every 7 s or on a stuck
   burst / life drop, and writes goal_deg / nav / take_exits into control.json. tev1:0.8b answers in ~1-5 s;
   tev1:latest (4B) takes ~20 s on the shared GPU; gemma3:4b can read the minimap strip but takes ~70 s.
   Game log: the navigator also follows PoE2's own Client.txt (Steam/steamapps/common/Path of Exile 2/logs/,
   deskdriver/reflex/gamelog.py): zone name + monster level, character level, deaths, NPC dialogue. That text is
   prepended to every question ("Zone: The Red Vale (monster level 5). Character level 5. Last NPC lines: Una: ...").
   Reading this file is allowed; reading the game's memory or packets is not (ToS, bans): never do that.
   Run it with $S/nav.sh (stop: pkill -f nav.sh; pkill -f "bin/desk-strategis[t]").
   The strategist (Claude) still owns quests, gear, town trips and the --context text.
   Tray "Pause agents" (PAUSED in the panel) makes every step return 423: the bot now holds still until it lifts.

What the strategist (the LLM over deskdriver) does by hand
- Read the quest tracker under the minimap; enter areas by clicking their labels; use "Create a Portal to Town"
  (swirl button left of the skill bar, ~(1480,977)) to go back; talk to NPCs; take rewards; right-click uncut gems
  to engrave; socket in the Skills panel (G); bind on the hotbar; spend passives (P, then P again and YES).
- Gear rule (user, 11-07): the strategist weighs DEFENCE, not DPS alone. Compare effective life (life + energy
  shield + armour/evasion + block from a shield) against the damage gain; a 2H weapon drops the shield's block and
  armour. Keep the defensive set (club + buckler) in weapon set II so X is a turtle switch; if deaths climb, make
  it the default. Deaths per hour is the tiebreaker.
- Loot colours: green quest > brown unique > yellow rare > teal gem > blue magic > white.
- Waypoints (learned 2026-11-07): click the Waypoint stone (camp: left of the Mud Burrow portal) and the TELEPORT
  map of the act opens. Green figure = where you stand, blue swirl = a discovered waypoint (hover: name, monster
  level, "Click to travel to ..."), house = town, grey ring = undiscovered, "?" with a quest pin = the next quest
  area (hover shows its name and the quest). A dotted route is drawn from the nearest waypoint to the quest area:
  that route's compass direction is the direction to explore inside the zone (minimap north = screen up).
  Alt shows points of interest. Close with Esc or the X at the top. Act 1 as of today: Clearfell waypoint (lvl 2)
  sits just right of the camp on the map; the Grelwood (lvl 4, "Secrets in the Dark") is north-east of it.
- Checkpoints (blue pillar, label "Checkpoint"): the respawn point. Walk up to one and click it: the CHECKPOINTS
  overlay opens, a map of the whole zone with every checkpoint as a blue icon. Icons with a gold teardrop pointer
  sit at zone exits and point the way out (Clearfell: left pointer = back to the camp, two upward pointers to the
  north, the north-east one is the Grelwood side). Clicking an icon teleports you there, but only to checkpoints
  you have already reached (unreached ones silently ignore the click). The first click on an icon CENTRES the
  map on it; a second click on the now-centred icon (screen centre, ~(956,512)) performs the jump. Moving closes
  the overlay. Bot flag
  `--set goal_deg=A` biases the explorer toward screen angle A and lets it walk into the exit icon on that side;
  `--set control=FILE` makes it re-read FILE (JSON: goal_deg, take_exits, nav, heading_deg) every 2 s, and the
  debug dir gets minimap-strip.jpg (the minimap every 5 s, last minute) for the strategist to read.
- CPU: the TensorFlow.js eyes (a headless Chrome, `desk eyes start`) are not used by this bot. Keep them off during
  a playtest (`desk eyes stop`); they cost a core.

Code map (deskdriver/reflex/arpg.py)
- bar_blobs / highlighted / fiery / beams_now / loot_now (+ templates/zone-*.png): what is an enemy, a drop, a label.
- track_minimap / explore_heading: dead-reckoned position + visited memory + boundary frontier steering.
- step(): presence check, flasks, retaliation, attack, sweep, beams, loot, wander/unstick (Rolling Slam, about-face).
- Debug frames go to the --debug dir: arpg-NNNNN.jpg (annotated) and clean-NNNNN.jpg (raw, for scoring detectors).
