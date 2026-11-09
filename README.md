# deskdriver

A virtual mouse, a virtual keyboard and a live screen buffer for agents, on a GNOME / Wayland desktop.
The desktop twin of browserdrive: the same *steps* vocabulary (`click`, `dblclick`, `type`, `press`, `drag`,
`wheel`, `wait`, `screenshot`), played on the real desktop instead of inside a page.

One daemon (`deskd`) owns the devices. Any number of agents drive it: over plain HTTP, the `desk` CLI, or the
`desk-mcp` MCP server (Claude Code, or any MCP client). A **lease** lets a swarm take turns on the one pointer.

## How it works

`deskd` opens one XDG RemoteDesktop + ScreenCast portal session. GNOME asks you once (**Share**); the restore
token is saved in `~/.config/deskdriver/restore_token`, after which it reconnects silently.

- **mouse**: `NotifyPointerMotionAbsolute` / `NotifyPointerButton` / axis events on the portal session, so the
  compositor treats it as a real pointer (window moves, drags, right-clicks, scrolling all work).
- **keyboard**: evdev keycodes for named keys and combos, X keysyms for typed text. Characters the active
  layout cannot produce (é, →, 日本 on a US layout) go through the portal **clipboard** and a ctrl+v, so any
  Unicode still lands. Agents can also read and set the clipboard directly.
- **screen buffer**: the PipeWire stream of the shared monitor, decoded by GStreamer; the latest frame is kept
  in memory and encoded on demand (PNG or JPEG, scaled, cropped).

Nothing runs as root and no udev rules are needed.

**Coordinates.** Every x/y is in *view pixels*: the pixel space of the screenshot the daemon hands out. The frame
is downscaled to fit `--max-width` (default 1920), so on a 4K monitor agents see and act in a 1920-wide space.
One space for seeing and for acting.

## Run

```sh
cd ~/development/deskdriver
.venv/bin/desk start            # background; click Share once
.venv/bin/desk status
.venv/bin/desk shot now.png
.venv/bin/desk click 640 400
.venv/bin/desk type "hello"
.venv/bin/desk paste "日本語 → ✓"   # any Unicode, via the clipboard
.venv/bin/desk clip               # read the clipboard
.venv/bin/desk press ctrl+s
.venv/bin/desk drag 100 100 500 300
.venv/bin/desk scroll 640 400 3
.venv/bin/desk stop
```

Options for the daemon: `desk start -- --max-width 1280 --cursor hidden --source window`. Log:
`~/.local/state/deskdriver/deskd.log`. `desk reconnect --forget` asks GNOME again (e.g. to pick another monitor).

## HTTP API (127.0.0.1:7777)

| call | what |
|---|---|
| `GET /status` | state, screen sizes, pointer, lease holder |
| `GET /screenshot?scale=0.5&region=x,y,w,h&format=jpeg&native=1` | image; headers `X-Desk-Image-Size`, `X-Desk-View-Size` |
| `POST /step` `{"do":"click","x":10,"y":20,"agent":"a1"}` | one step |
| `POST /steps` `{"steps":[…],"agent":"a1"}` | several steps, in order, atomically against other agents |
| `POST /lease` `{"agent":"a1","ttl":60,"wait":30}` | take the input lease (423 if another agent holds it) |
| `POST /release` `{"agent":"a1"}` | give it back |
| `GET /clipboard` / `POST /clipboard` `{"text":"…"}` | read or set the clipboard |
| `POST /reconnect` `{"forget_token":false}` | redo the portal session |

Steps (`do`): `move`/`hover` `x y` · `click` `x y [button count modifiers]` · `dblclick` · `rightclick` ·
`down`/`up` `[x y] [button]` · `drag` `x y to_x to_y [steps step_ms]` · `wheel` `[x y] dy dx` ·
`type` `text [delay_ms method=auto|keys|paste]` · `paste` `text` · `press` `keys` (`"ctrl+s"` or `["ctrl+a","delete"]`) · `keydown`/`keyup` `keys` ·
`wait` `ms` · `screenshot`.

Key names: letters, digits, `enter tab esc backspace delete space up down left right home end pageup pagedown
insert f1..f12 printscreen`, modifiers `ctrl shift alt super`; any single character otherwise (typed by keysym).

## Several agents

The pointer is one thing. Agents that act at the same time will fight over it, so:

1. `desk_lease` (or `POST /lease`) before a sequence of inputs, `desk_release` after. A lease expires on its
   own after `ttl` seconds in case the agent dies. `wait` seconds blocks until the lease is free.
2. Screenshots are never leased; every agent can look at any time.
3. `POST /steps` runs a batch under the input lock, so a batch from agent A never interleaves with agent B.

A draft/polish swarm looks like: the builder takes the lease and lays the page out; releases; the polishers each
`desk_zoom` their region, lease, nudge, release, and re-check.

## Tray icon, dock indicator and status page

The daemon puts a pointer icon in GNOME's top bar (a StatusNotifierItem, shown by Ubuntu's AppIndicator extension):
grey when there is no session, blue when ready, orange with a pulsing dot while an agent is driving, with a label
beside it that reads "desk ready" or "DRIVING · <agent>". Its tooltip adds the agent's note; a click opens the status
page. Right-click opens a menu: the status line, **Open status page**, **Pause agents** (every step is refused with
423 until resumed; the label reads PAUSED), **Reconnect screen** (redoes the portal session, the same as
`desk reconnect`, for when the status reads "CreateSession: ended unexpectedly" after a lock screen or portal restart)
and **Quit deskdriver** (closes the portal session and ends the daemon; MCP
clients then answer "deskd is not reachable" until `desk start`). `desk stop` does the same from a terminal, and
`claude mcp remove desk` unregisters the MCP server from Claude Code altogether. The icon is drawn at 24px; `gsettings set org.gnome.shell.extensions.appindicator icon-size 24` makes the panel
show tray icons at that size (it applies to every tray icon).

`deskdriver.desktop` (pinned to the Ubuntu dock) also shows what the daemon is doing through the Unity LauncherEntry
API that Dash to Dock understands: a progress bar along the icon while an agent holds the input lease (time left),
and a count badge with the steps executed while something is driving. Right-clicking the dock icon lists its desktop
actions: **Reconnect screen**, **Reconnect and ask again** (`desk reconnect --forget`), **Start deskdriver** and
**Quit deskdriver**, so the daemon can be revived without a terminal. Clicking the icon opens
`http://127.0.0.1:7777/`, a live status page with the lease holder and a refreshing view of the screen buffer.

## The reflex layer: playing games

`desk-reflex` is a fast loop on top of the daemon: it reads raw frames from `/raw` (75 fps at half size), runs a
small policy at 8 to 20 Hz, and drives keys and the mouse through the lease like any other agent. A policy is one
file; the language model belongs above it (setting its parameters), never inside it.

```sh
desk-reflex comet --seconds 60 --debug /tmp/frames        # Comet Tag: flips when a rival or spoke is ahead on its lane
desk-reflex arpg --seconds 120 --set flask=1 --dry        # click-to-move ARPG: attack health bars, flask when low, wander
desk-reflex generic --spec specs/comet-tap.json           # any 2D game from a JSON spec: me / threats / goods / controls
```

`--dry` sees without pressing; `--debug DIR` writes annotated frames; `--set k=v` tunes a policy. The vision toolkit
(`deskdriver/reflex/vision.py`) has colour masks (RGB and HSV), connected-component blobs, template matching, HUD
bar fill, frame-difference motion and a position track with velocity, on OpenCV. Every policy refuses to act when
its game is not on screen (the person may have alt-tabbed) and releases held keys when it stops.

**Two speeds.** The reflex policy is the body: combat, flasks, pickups, following the minimap, at 8 to 20 Hz, from
colour and shape alone. A language model is the mind: every few seconds it reads a screenshot (quest tracker, map,
inventory, the skill tree) through the MCP tools and changes what the body is doing, with `--set` parameters, a
fixed heading (`--set nav=fixed --set heading_deg=45`), or by taking the lease itself for a menu task (equip the
club, allocate a skill point, talk to the NPC) and handing it back. Neither layer should do the other's job: the
model is too slow for a fight, the reflex loop cannot read a tooltip.

**TensorFlow.js eyes.** `http://127.0.0.1:7777/tfjs` is a page that shows the live stream and runs a TensorFlow.js
model on it in the browser (COCO-SSD by default, ~20 fps on this GPU), posting what it sees to `POST /detections` in
view pixels; a policy reads `GET /detections` ({items: [{label, score, x, y, w, h}], age_ms}). Swap the model in that
page for any TF.js demo model, or a model trained on labelled frames of a game. Run it with `desk eyes start` (a
headless Chrome, off screen, so it never sees itself in the stream) and `desk eyes stop`; opening the page in a
visible tab shows the boxes but recurses into the shared screen.

## Recording

`desk record start [out.mp4]` records the screen buffer in real time (ffmpeg reading `/stream`, wall-clock timestamps so
idle time is kept); `desk record stop` finalises the file. 720p at 10 fps is about 1.3 MB a minute.

## Claude Code

```sh
claude mcp add --scope user desk -- ~/development/deskdriver/.venv/bin/desk-mcp
```

Set `DESK_AGENT=<name>` in the server's env to name the agent in leases (default `mcp-<pid>`).
Tools: `desk_screenshot desk_zoom desk_move desk_click desk_drag desk_scroll desk_type desk_press desk_steps
desk_wait desk_clipboard desk_lease desk_release desk_status`.

## Limits

- Wayland + GNOME 46 or newer (needs RemoteDesktop portal v2 for the restore token). Other compositors with
  the RemoteDesktop portal (KDE, wlroots + xdg-desktop-portal-wlr) should work but were not tried.
- One shared source per session: a monitor by default. Pick the monitor in the Share dialog.
- The lock screen and GNOME Shell modal dialogs are not reachable; the session ends on logout.
- Combos use keycodes (`ctrl+z` is the physical Z key). Pasted text replaces whatever was on the clipboard.
- Restarting `deskd` after a code change that adds a portal feature may need `desk reconnect --forget`
  (one more Share click), since the saved consent covers only what was asked the first time.
