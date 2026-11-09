"""Thin HTTP client for deskd, shared by the CLI and the MCP server."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class DeskError(RuntimeError):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class DeskClient:
    def __init__(self, base: str | None = None, agent: str | None = None, timeout: float = 120):
        self.base = (base or os.environ.get("DESK_URL") or "http://127.0.0.1:7777").rstrip("/")
        self.agent = agent or os.environ.get("DESK_AGENT") or f"pid-{os.getpid()}"
        self.timeout = timeout

    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Desk-Agent": self.agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
                if ctype.startswith("image/"):
                    return raw, dict(r.headers)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or str(e)
            except Exception:  # noqa: BLE001
                msg = str(e)
            raise DeskError(msg, e.code) from None
        except urllib.error.URLError as e:
            raise DeskError(f"deskd is not reachable at {self.base} ({e.reason}). Start it with: desk start") from None

    def status(self) -> dict:
        return self._req("GET", "/status")

    def screenshot(self, scale: float | None = None, region: tuple | None = None, fmt: str = "png",
                   quality: int = 85, native: bool = False) -> tuple[bytes, dict]:
        params = {"scale": scale, "format": fmt, "quality": quality, "native": 1 if native else None}
        if region:
            params["region"] = ",".join(str(int(v)) for v in region)
        return self._req("GET", "/screenshot", params)

    def step(self, do: str, **kw) -> dict:
        r = self._req("POST", "/step", body={"do": do, "agent": self.agent, **kw})
        return r["results"][0]

    def steps(self, steps: list[dict]) -> list[dict]:
        r = self._req("POST", "/steps", body={"steps": steps, "agent": self.agent})
        return r["results"]

    def lease(self, ttl: float = 60, note: str | None = None, wait: float = 0) -> dict:
        return self._req("POST", "/lease", body={"agent": self.agent, "ttl": ttl, "note": note, "wait": wait})

    def release(self) -> dict:
        return self._req("POST", "/release", body={"agent": self.agent})

    def clipboard_get(self) -> str:
        return self._req("GET", "/clipboard")["text"]

    def clipboard_set(self, text: str) -> dict:
        return self._req("POST", "/clipboard", body={"text": text})

    def reconnect(self, forget_token: bool = False) -> dict:
        return self._req("POST", "/reconnect", body={"forget_token": forget_token})
