"""The screen buffer: a PipeWire stream of the shared screen, decoded with GStreamer, latest frame kept."""
import io
import logging
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

log = logging.getLogger("desk.screen")


class Screen:
    def __init__(self, fd: int, node: int):
        Gst.init(None)
        self.lock = threading.Lock()
        self.frame: np.ndarray | None = None   # (h, w, 3) RGB
        self.size = (0, 0)
        self.count = 0
        self.ts = 0.0
        self.error: str | None = None
        self._ready = threading.Event()
        desc = (f"pipewiresrc fd={fd} path={node} do-timestamp=true keepalive-time=500 "
                "! videoconvert ! video/x-raw,format=RGBx "
                "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
        self.pipeline = Gst.parse_launch(desc)
        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.pipeline.set_state(Gst.State.PLAYING)

    def wait_first_frame(self, timeout: float = 15) -> bool:
        return self._ready.wait(timeout)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def _on_error(self, _bus, msg):
        err, dbg = msg.parse_error()
        self.error = f"{err.message} ({dbg})"
        log.error("stream error: %s", self.error)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        stride = len(data) // h
        arr = np.frombuffer(data, np.uint8)[: stride * h].reshape(h, stride)[:, : w * 4].reshape(h, w, 4)
        with self.lock:
            self.frame = np.ascontiguousarray(arr[:, :, :3])
            self.size = (w, h)
            self.count += 1
            self.ts = time.time()
        self._ready.set()
        return Gst.FlowReturn.OK

    def snapshot(self) -> tuple[np.ndarray, float] | tuple[None, float]:
        with self.lock:
            return (self.frame, self.ts)

    def image(self, scale: float = 1.0, region: tuple[int, int, int, int] | None = None,
              fmt: str = "png", quality: int = 85) -> tuple[bytes, tuple[int, int]]:
        """Encode the latest frame. `region` is (x, y, w, h) in frame pixels, applied before scaling."""
        frame, _ = self.snapshot()
        if frame is None:
            raise RuntimeError("no frame yet")
        if region:
            x, y, w, h = region
            frame = frame[max(0, y): y + h, max(0, x): x + w]
        img = Image.fromarray(frame, "RGB")
        if scale != 1.0:
            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                             Image.LANCZOS if scale < 1 else Image.NEAREST)
        out = io.BytesIO()
        if fmt == "jpeg":
            img.save(out, "JPEG", quality=quality)
        else:
            img.save(out, "PNG", compress_level=3)
        return out.getvalue(), img.size
