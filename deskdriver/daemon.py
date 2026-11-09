"""deskd: owns the virtual mouse, keyboard and screen buffer; agents drive it over local HTTP.

Coordinates: every x/y an agent sends or reads is in *view* pixels, the pixel
space of the screenshot the daemon hands out (the frame, downscaled to fit
--max-width). One space for seeing and for acting.
"""
import argparse
import base64
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .keys import MODIFIERS, char_keysym, parse_combo, KEYCODES
from .portal import Portal, PortalError
from .screen import Screen
from .indicator import Indicator
from .tray import Tray

log = logging.getLogger("desk")

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "deskdriver")


class StepError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class Desk:
    def __init__(self, max_width: int, cursor: str, source: str):
        self.max_width = max_width
        self.portal = Portal(cursor_mode=cursor, source_type=source)
        self.portal.on_closed = self._reconnect
        self.screen: Screen | None = None
        self.input_lock = threading.Lock()
        self.lease_lock = threading.RLock()
        self.lease: dict | None = None     # {agent, expires, note}
        self.pointer = (0, 0)
        self.error: str | None = None
        self.steps_done = 0
        self.last_step_ts = 0.0
        self.paused = False
        self.detections = {"t": 0, "frame": 0, "items": [], "source": None}

    # -- lifecycle ---------------------------------------------------------------------------
    def connect(self):
        self.error = None
        if self.screen:
            self.screen.stop()
            self.screen = None
        try:
            self.portal.open()
            self.screen = Screen(self.portal.pw_fd, self.portal.stream_id)
            if not self.screen.wait_first_frame(20):
                raise PortalError("the screen stream sent no frame" + (f": {self.screen.error}" if self.screen.error else ""))
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.error("connect failed: %s", e)
            raise

    def _reconnect(self):
        time.sleep(1)
        try:
            log.info("reconnecting")
            self.connect()
        except Exception:  # noqa: BLE001
            pass

    def quit(self):
        """Close the portal session and end the daemon (the tray's Quit, or SIGTERM)."""
        try:
            self.portal.close()
        finally:
            time.sleep(0.3)
            os._exit(0)

    def state(self) -> str:
        if self.portal.state == "ready" and self.screen and self.screen.frame is not None:
            return "ready"
        return self.portal.state

    # -- geometry ----------------------------------------------------------------------------
    def view(self) -> tuple[int, int, float]:
        """(view_w, view_h, view/frame scale)"""
        fw, fh = self.screen.size if self.screen else (0, 0)
        if not fw:
            return 0, 0, 1.0
        k = min(1.0, self.max_width / fw)
        return round(fw * k), round(fh * k), k

    def to_logical(self, x: float, y: float) -> tuple[float, float]:
        vw, vh, _ = self.view()
        lw, lh = self.portal.logical
        if not lw or not lh:
            lw, lh = self.screen.size
        if not vw:
            raise StepError("no screen yet", 503)
        return x / vw * lw, y / vh * lh

    def status(self) -> dict:
        vw, vh, k = self.view()
        return {
            "state": self.state(), "error": self.error,
            "screen": {"view": [vw, vh], "frame": list(self.screen.size) if self.screen else [0, 0],
                        "logical": list(self.portal.logical), "scale": k},
            "frames": self.screen.count if self.screen else 0,
            "frame_age_ms": round((time.time() - self.screen.ts) * 1000) if self.screen and self.screen.ts else None,
            "pointer": list(self.pointer), "lease": self._lease_info(), "steps": self.steps_done,
            "clipboard": self.portal.clipboard, "paused": self.paused,
        }

    # -- leases: who may drive the input right now --------------------------------------------
    def _lease_info(self):
        with self.lease_lock:
            if self.lease and self.lease["expires"] > time.time():
                return {"agent": self.lease["agent"], "note": self.lease.get("note"),
                        "expires_in": round(self.lease["expires"] - time.time(), 1), "ttl": self.lease.get("ttl")}
            return None

    def acquire(self, agent: str, ttl: float, note: str | None = None, wait: float = 0) -> dict:
        deadline = time.time() + wait
        while True:
            with self.lease_lock:
                now = time.time()
                if not self.lease or self.lease["expires"] <= now or self.lease["agent"] == agent:
                    self.lease = {"agent": agent, "expires": now + ttl, "note": note, "ttl": ttl}
                    return {"ok": True, "lease": self._lease_info()}
                holder = dict(self.lease)
            if time.time() >= deadline:
                raise StepError(f"input is leased to '{holder['agent']}' for another "
                                f"{holder['expires'] - time.time():.0f}s", 423)
            time.sleep(0.2)

    def release(self, agent: str):
        with self.lease_lock:
            if self.lease and self.lease["agent"] == agent:
                self.lease = None
        return {"ok": True}

    def _check_lease(self, agent: str | None):
        with self.lease_lock:
            if self.lease and self.lease["expires"] > time.time() and self.lease["agent"] != agent:
                raise StepError(f"input is leased to '{self.lease['agent']}'", 423)

    # -- steps -------------------------------------------------------------------------------
    def run_steps(self, steps: list[dict], agent: str | None) -> list[dict]:
        if self.state() != "ready":
            raise StepError(f"desk is not ready ({self.state()}): {self.error or 'waiting for the screen share'}", 503)
        if self.paused:
            raise StepError("agents are paused from the tray menu; resume there to drive again", 423)
        self._check_lease(agent)
        results = []
        with self.input_lock:
            for s in steps:
                t0 = time.time()
                r = self._step(s) or {}
                r.setdefault("ok", True)
                r["do"] = s.get("do")
                r["ms"] = round((time.time() - t0) * 1000)
                results.append(r)
                self.steps_done += 1
                self.last_step_ts = time.time()
        return results

    def _num(self, s, key, default=None):
        v = s.get(key, default)
        if v is None:
            raise StepError(f"'{s.get('do')}' needs '{key}'")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise StepError(f"'{key}' must be a number") from None

    def _move(self, x, y):
        lx, ly = self.to_logical(x, y)
        self.portal.move_abs(lx, ly)
        self.pointer = (round(x), round(y))

    def _click(self, button: str, count: int = 1, hold_ms: float = 25):
        for i in range(count):
            self.portal.button(button, True)
            time.sleep(hold_ms / 1000)
            self.portal.button(button, False)
            if i + 1 < count:
                time.sleep(0.07)

    @staticmethod
    def _typeable(ch: str) -> bool:
        return ch in "\n\t" or 0x20 <= ord(ch) < 0x7F

    def _paste(self, chunk: str):
        self.portal.set_clipboard(chunk)
        time.sleep(0.05)
        for kind, code in parse_combo("ctrl+v"):
            self.portal.keycode(code, True)
        for kind, code in reversed(parse_combo("ctrl+v")):
            self.portal.keycode(code, False)
        time.sleep(0.08)

    def _type_text(self, text: str, delay: float, method: str = "auto") -> dict:
        """Type what the keyboard layout can produce; paste the rest through the clipboard."""
        p = self.portal
        pasted = dropped = typed = 0
        if method == "paste":
            runs = [(False, text)] if text else []
        elif method == "keys":
            runs = [(True, text)] if text else []
        else:
            runs = []
            for ch in text:
                t = self._typeable(ch)
                if runs and runs[-1][0] == t:
                    runs[-1] = (t, runs[-1][1] + ch)
                else:
                    runs.append((t, ch))
        for typeable, chunk in runs:
            if not typeable and p.clipboard:
                self._paste(chunk)
                pasted += len(chunk)
                continue
            for ch in chunk:
                if ch == "\n":
                    p.keycode(KEYCODES["enter"], True); p.keycode(KEYCODES["enter"], False)
                elif ch == "\t":
                    p.keycode(KEYCODES["tab"], True); p.keycode(KEYCODES["tab"], False)
                elif not self._typeable(ch) and method != "keys":
                    dropped += 1
                    continue
                else:
                    sym = char_keysym(ch)
                    p.keysym(sym, True)
                    p.keysym(sym, False)
                typed += 1
                if delay:
                    time.sleep(delay)
        out = {"chars": len(text), "typed": typed, "pasted": pasted}
        if dropped:
            out["dropped"] = dropped
            out["note"] = "characters the keyboard layout cannot type were dropped (no clipboard access)"
        return out

    def _step(self, s: dict) -> dict | None:
        do = (s.get("do") or "").lower()
        p = self.portal
        if do in ("move", "hover"):
            self._move(self._num(s, "x"), self._num(s, "y"))
            time.sleep(0.02)
        elif do in ("click", "dblclick", "rightclick", "middleclick"):
            if "x" in s and "y" in s:
                self._move(self._num(s, "x"), self._num(s, "y"))
                time.sleep(0.03)
            button = {"rightclick": "right", "middleclick": "middle"}.get(do, s.get("button", "left"))
            count = 2 if do == "dblclick" else int(s.get("count", 1))
            mods = [m for m in (s.get("modifiers") or [])]
            held = []
            for m in mods:
                kind, code = parse_combo(m)[0]
                p.keycode(code, True) if kind == "code" else p.keysym(code, True)
                held.append((kind, code))
            try:
                self._click(button, count)
            finally:
                for kind, code in reversed(held):
                    p.keycode(code, False) if kind == "code" else p.keysym(code, False)
        elif do in ("down", "mousedown"):
            if "x" in s and "y" in s:
                self._move(self._num(s, "x"), self._num(s, "y"))
                time.sleep(0.02)
            p.button(s.get("button", "left"), True)
        elif do in ("up", "mouseup"):
            if "x" in s and "y" in s:
                self._move(self._num(s, "x"), self._num(s, "y"))
                time.sleep(0.02)
            p.button(s.get("button", "left"), False)
        elif do == "drag":
            x1, y1 = self._num(s, "x"), self._num(s, "y")
            x2, y2 = self._num(s, "to_x", s.get("toX")), self._num(s, "to_y", s.get("toY"))
            button = s.get("button", "left")
            n = max(2, int(s.get("steps", 16)))
            pause = float(s.get("step_ms", 12)) / 1000
            self._move(x1, y1)
            time.sleep(0.05)
            p.button(button, True)
            time.sleep(0.08)
            for i in range(1, n + 1):
                self._move(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n)
                time.sleep(pause)
            time.sleep(0.08)
            p.button(button, False)
        elif do in ("wheel", "scroll"):
            if "x" in s and "y" in s:
                self._move(self._num(s, "x"), self._num(s, "y"))
                time.sleep(0.02)
            dx, dy = int(s.get("dx", 0)), int(s.get("dy", 0))
            if "pixels" in s:
                p.wheel_smooth(float(s.get("px", 0)), float(s["pixels"]))
            else:
                # one "step" = one notch; spread them so apps see distinct events
                for _ in range(abs(dy)):
                    p.wheel_steps(dy=1 if dy > 0 else -1)
                    time.sleep(0.015)
                for _ in range(abs(dx)):
                    p.wheel_steps(dx=1 if dx > 0 else -1)
                    time.sleep(0.015)
        elif do in ("type", "fill"):
            text = s.get("text")
            if text is None:
                raise StepError("'type' needs 'text'")
            return self._type_text(str(text), float(s.get("delay_ms", 4)) / 1000, s.get("method", "auto"))
        elif do == "paste":
            text = s.get("text")
            if text is None:
                raise StepError("'paste' needs 'text'")
            return self._type_text(str(text), 0, "paste")
        elif do in ("press", "key", "keys"):
            combos = s.get("keys") or s.get("key") or s.get("combo")
            if combos is None:
                raise StepError("'press' needs 'keys' (e.g. 'ctrl+s' or ['ctrl+a', 'delete'])")
            if isinstance(combos, str):
                combos = [combos]
            for combo in combos:
                keys = parse_combo(combo)
                for kind, code in keys:
                    p.keycode(code, True) if kind == "code" else p.keysym(code, True)
                    time.sleep(0.01)
                for kind, code in reversed(keys):
                    p.keycode(code, False) if kind == "code" else p.keysym(code, False)
                time.sleep(float(s.get("delay_ms", 30)) / 1000)
        elif do in ("keydown", "keyup"):
            combo = s.get("keys") or s.get("key")
            for kind, code in parse_combo(combo):
                p.keycode(code, do == "keydown") if kind == "code" else p.keysym(code, do == "keydown")
        elif do in ("wait", "sleep"):
            time.sleep(min(float(s.get("ms", 500)), 30000) / 1000)
        elif do == "screenshot":
            png, size = self.screen.image(self.view()[2])
            return {"png_base64": base64.b64encode(png).decode(), "size": list(size)}
        else:
            raise StepError(f"unknown step '{do}'. Steps: move hover click dblclick rightclick middleclick "
                            "down up drag wheel type paste press keydown keyup wait screenshot")
        return None

    # -- screenshots ---------------------------------------------------------------------------
    def shot(self, scale: float | None, region: tuple | None, fmt: str, quality: int, native: bool):
        if not self.screen or self.screen.frame is None:
            raise StepError(f"no screen yet ({self.state()})", 503)
        vw, vh, k = self.view()
        if native:
            k_region, k_out = 1.0, 1.0
        else:
            k_region, k_out = k, k
        if region:
            x, y, w, h = region
            # region is given in view pixels; convert to frame pixels
            region = (round(x / k_region), round(y / k_region), round(w / k_region), round(h / k_region))
        out_scale = k_out * (scale if scale else 1.0)
        data, size = self.screen.image(out_scale if not native else (scale or 1.0), region, fmt, quality)
        return data, size


STATUS_PAGE = """<!doctype html><meta charset=utf-8><title>deskdriver</title>
<style>body{margin:0;background:#0b0e14;color:#dfe5f1;font:15px system-ui;display:flex;flex-direction:column;height:100vh}
header{display:flex;gap:18px;align-items:center;padding:10px 16px;background:#141a26;border-bottom:1px solid #26304a}
.dot{width:12px;height:12px;border-radius:50%;background:#555}.dot.on{background:#4f8cff;box-shadow:0 0 12px #4f8cff}
.dot.drive{background:#ff9f43;box-shadow:0 0 14px #ff9f43}b{color:#fff}img{flex:1;min-height:0;object-fit:contain;background:#000}
small{color:#8892a8}</style>
<header><span class=dot id=dot></span><span id=state>connecting</span><span id=lease></span><span id=steps></span>
<small id=age></small></header><img id=shot alt="">
<script>
const $=id=>document.getElementById(id);let busy=false;
async function tick(){try{const s=await (await fetch('/status')).json();
const drive=s.lease||(s.frame_age_ms!=null&&s.steps>window._steps);window._steps=s.steps;
$('dot').className='dot '+(s.state!=='ready'?'':drive?'drive':'on');
$('state').innerHTML=s.state==='ready'?'<b>ready</b> '+s.screen.view.join('x'):s.state;
$('lease').innerHTML=s.lease?'driving: <b>'+s.lease.agent+'</b> '+(s.lease.note||'')+' ('+Math.round(s.lease.expires_in)+'s)':'idle';
$('steps').textContent='steps '+s.steps;$('age').textContent=s.frame_age_ms!=null?'frame '+s.frame_age_ms+'ms ago':'';
if(!busy&&s.state==='ready'){busy=true;const i=new Image();i.onload=()=>{$('shot').src=i.src;busy=false};i.onerror=()=>busy=false;
i.src='/screenshot?format=jpeg&quality=70&scale=0.6&t='+Date.now();}}catch(e){$('state').textContent='deskd not reachable'}}
if(location.search.includes('live')){$('shot').src='/stream?scale=0.6';busy=true;}
setInterval(tick,700);tick();
</script>"""


TFJS_PAGE = """<!doctype html><meta charset=utf-8><title>deskdriver · tfjs eyes</title>
<style>body{margin:0;background:#0b0e14;color:#dfe5f1;font:14px system-ui}header{padding:8px 12px;background:#141a26}
#wrap{position:relative;display:inline-block}canvas{position:absolute;left:0;top:0}img{display:block;max-width:100vw}</style>
<header>TensorFlow.js eyes: <b id=model>loading coco-ssd…</b> · <span id=stat></span> · posts to /detections at <span id=fps>0</span> fps</header>
<div id=wrap><img id=src src="/stream?fps=15&scale=0.5"><canvas id=ov></canvas></div>
<pre id=list></pre>
<script>if(location.search.includes('quiet')){document.getElementById('wrap').style.cssText='position:absolute;left:-10000px;top:0';}</script>
<script src="https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@4.22.0/dist/tf.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@tensorflow-models/coco-ssd@2.2.3/dist/coco-ssd.min.js"></script>
<script>
const img=document.getElementById('src'),ov=document.getElementById('ov'),ctx=ov.getContext('2d');
const scale=2; // the stream is half the view; detections are posted in view pixels
let model=null,n=0,t0=performance.now();
console.log('eyes: page up, tf '+(window.tf?tf.version.tfjs:'MISSING')+' backend '+(window.tf?tf.getBackend():'-'));
img.addEventListener('error',()=>console.log('eyes: stream image error'));
cocoSsd.load({base:'lite_mobilenet_v2'}).then(m=>{model=m;console.log('eyes: model loaded');document.getElementById('model').textContent='coco-ssd (lite_mobilenet_v2)';loop();}).catch(e=>console.log('eyes: model failed '+e.message));
async function loop(){
  if(img.naturalWidth){ov.width=img.naturalWidth;ov.height=img.naturalHeight;
    try{const preds=await model.detect(img,20,0.35);
      ctx.clearRect(0,0,ov.width,ov.height);ctx.strokeStyle='#4f8cff';ctx.fillStyle='#4f8cff';ctx.lineWidth=2;ctx.font='13px system-ui';
      const items=preds.map(p=>{const [x,y,w,h]=p.bbox;ctx.strokeRect(x,y,w,h);ctx.fillText(p.class+' '+p.score.toFixed(2),x+2,y+12);
        return {label:p.class,score:+p.score.toFixed(3),x:Math.round(x*scale),y:Math.round(y*scale),w:Math.round(w*scale),h:Math.round(h*scale)};});
      fetch('/detections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items,source:'coco-ssd'})});
      n++;if(n==1||n%200==0)console.log('eyes: tick '+n+' objects '+items.length);document.getElementById('stat').textContent=items.length+' objects';
      document.getElementById('list').textContent=items.map(i=>i.label+' '+i.score+' @'+i.x+','+i.y+' '+i.w+'x'+i.h).join(String.fromCharCode(10));
      if(n%10==0){document.getElementById('fps').textContent=(10000/(performance.now()-t0)).toFixed(1);t0=performance.now();}
    }catch(e){document.getElementById('stat').textContent='error: '+e.message;if(n<3)console.log('eyes: detect error '+e.message);}}
  else if(n==0&&!window._w){window._w=1;console.log('eyes: waiting for the first stream frame (naturalWidth 0)');}
  setTimeout(loop, 10);}
</script>"""


# -- HTTP -----------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    desk: Desk = None  # set by main
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter
        log.debug("http " + fmt, *args)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError as e:
            raise StepError(f"bad JSON body: {e}") from None

    def _run(self, fn):
        try:
            fn()
        except StepError as e:
            self._json({"ok": False, "error": str(e)}, e.status)
        except (PortalError, ValueError) as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/status", "/health"):
            return self._run(lambda: self._json({"ok": True, **self.desk.status()}))
        if u.path == "/":
            body = STATUS_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/clipboard":
            return self._run(lambda: self._json({"ok": True, "text": self.desk.portal.read_clipboard()}))
        if u.path == "/detections":
            return self._run(lambda: self._json({"ok": True, **self.desk.detections,
                                                 "age_ms": round((time.time() - self.desk.detections["t"]) * 1000) if self.desk.detections["t"] else None}))
        if u.path == "/tfjs":
            body = TFJS_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/raw":
            # the latest frame as raw RGB bytes (no encoding), for reflex loops: ?scale=0.5|0.25|1
            def go():
                if not self.desk.screen or self.desk.screen.frame is None:
                    raise StepError("no frame yet", 503)
                frame, ts = self.desk.screen.snapshot()
                step = {"1": 1, "0.5": 2, "0.25": 4}.get(q.get("scale", "0.5"), 2)
                sub = frame[::step, ::step]
                data = sub.tobytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Desk-Image-Size", f"{sub.shape[1]}x{sub.shape[0]}")
                self.send_header("X-Desk-Frame", str(self.desk.screen.count))
                self.send_header("X-Desk-Scale", str(1.0 / step))
                self.end_headers()
                self.wfile.write(data)
            return self._run(go)
        if u.path == "/stream":
            # MJPEG live view of the screen buffer (multipart/x-mixed-replace), ~fps frames per second
            def go():
                fps = max(1.0, min(30.0, float(q.get("fps", 10))))
                scale = float(q.get("scale", 0.5))
                quality = int(q.get("quality", 70))
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                last = -1
                while True:
                    if not self.desk.screen or self.desk.screen.frame is None:
                        time.sleep(0.5)
                        continue
                    if self.desk.screen.count != last:
                        last = self.desk.screen.count
                        data, _ = self.desk.shot(scale, None, "jpeg", quality, False)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                        self.wfile.flush()
                    time.sleep(1.0 / fps)
            try:
                return self._run(go)
            except (BrokenPipeError, ConnectionResetError):
                return
        if u.path in ("/screenshot", "/screen"):
            def go():
                region = None
                if "region" in q:
                    region = tuple(int(float(v)) for v in q["region"].split(","))
                    if len(region) != 4:
                        raise StepError("region must be x,y,w,h")
                fmt = "jpeg" if q.get("format", "png").lower() in ("jpg", "jpeg") else "png"
                data, size = self.desk.shot(float(q["scale"]) if "scale" in q else None, region, fmt,
                                            int(q.get("quality", 85)), q.get("native") in ("1", "true"))
                self.send_response(200)
                self.send_header("Content-Type", f"image/{fmt}")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Desk-Image-Size", f"{size[0]}x{size[1]}")
                vw, vh, _ = self.desk.view()
                self.send_header("X-Desk-View-Size", f"{vw}x{vh}")
                self.end_headers()
                self.wfile.write(data)
            return self._run(go)
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        def go():
            body = self._body()
            agent = body.get("agent") or q.get("agent") or self.headers.get("X-Desk-Agent")
            if u.path == "/step":
                step = {k: v for k, v in body.items() if k != "agent"}
                self._json({"ok": True, "results": self.desk.run_steps([step], agent)})
            elif u.path == "/steps":
                steps = body.get("steps")
                if not isinstance(steps, list) or not steps:
                    raise StepError("'steps' must be a non-empty list")
                self._json({"ok": True, "results": self.desk.run_steps(steps, agent)})
            elif u.path == "/lease":
                if not agent:
                    raise StepError("'agent' is required")
                self._json(self.desk.acquire(agent, float(body.get("ttl", 60)), body.get("note"),
                                             float(body.get("wait", 0))))
            elif u.path == "/release":
                if not agent:
                    raise StepError("'agent' is required")
                self._json(self.desk.release(agent))
            elif u.path == "/detections":
                items = body.get("items")
                if not isinstance(items, list):
                    raise StepError("'items' must be a list of {label, score, x, y, w, h} in view pixels")
                self.desk.detections = {"t": time.time(), "frame": int(body.get("frame", 0)), "items": items[:200],
                                        "source": str(body.get("source", "tfjs"))[:40]}
                self._json({"ok": True, "n": len(items)})
            elif u.path == "/clipboard":
                if "text" not in body:
                    raise StepError("'text' is required")
                self.desk.portal.set_clipboard(str(body["text"]))
                self._json({"ok": True})
            elif u.path == "/reconnect":
                if body.get("forget_token"):
                    Portal.forget_token()
                threading.Thread(target=self.desk._reconnect, daemon=True).start()
                self._json({"ok": True, "state": self.desk.state()})
            else:
                raise StepError("not found", 404)
        self._run(go)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/lease":
            agent = q.get("agent") or self.headers.get("X-Desk-Agent") or ""
            return self._run(lambda: self._json(self.desk.release(agent)))
        self._json({"ok": False, "error": "not found"}, 404)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deskd", description="virtual mouse, keyboard and screen buffer for agents")
    ap.add_argument("--host", default=os.environ.get("DESK_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DESK_PORT", "7777")))
    ap.add_argument("--max-width", type=int, default=int(os.environ.get("DESK_MAX_WIDTH", "1920")),
                    help="screenshots (and the coordinate space) are downscaled to fit this width")
    ap.add_argument("--cursor", choices=["embedded", "hidden"], default=os.environ.get("DESK_CURSOR", "embedded"),
                    help="draw the pointer into screenshots or not")
    ap.add_argument("--source", choices=["monitor", "window", "any", "virtual"], default=os.environ.get("DESK_SOURCE", "monitor"))
    ap.add_argument("--forget", action="store_true", help="drop the saved permission and ask again")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if a.forget:
        Portal.forget_token()

    desk = Desk(a.max_width, a.cursor, a.source)
    Handler.desk = desk
    Indicator(desk)
    Tray(desk)
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    server.daemon_threads = True
    log.info("deskd listening on http://%s:%d", a.host, a.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        desk.connect()
        vw, vh, _ = desk.view()
        log.info("screen ready: %dx%d view (%dx%d frame)", vw, vh, *desk.screen.size)
    except Exception as e:  # noqa: BLE001
        log.error("not connected yet (%s); POST /reconnect to try again", e)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        desk.portal.close()
        server.shutdown()


if __name__ == "__main__":
    main()
