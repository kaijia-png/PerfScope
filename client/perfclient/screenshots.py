"""Periodic screenshots during a session, lined up with the metric timeline.

Captured on their own thread so a slow screencap (0.3-1 s on many phones) never delays
metric sampling. Each frame is shrunk to a small JPEG before it is stored.
"""
import io
import threading
import time

DEFAULT_WIDTH = 360
JPEG_QUALITY = 60


def shrink(image: bytes, width: int = DEFAULT_WIDTH) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(image))
    im = im.convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.BILINEAR)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def parse_config(cfg) -> dict:
    """config `screenshots: {interval: 1, width: 360}`; `true` means every sampling interval;
    absent / false / interval 0 turns it off."""
    sc = cfg.get("screenshots")
    if not sc:
        return {}
    if sc is True:
        sc = {}
    interval = float(sc.get("interval") or cfg.get("interval") or 1)
    return {"interval": max(interval, 0.2), "width": int(sc.get("width") or DEFAULT_WIDTH)}


class ScreenshotRecorder:
    def __init__(self, device, store, session_id, interval, width, on_error):
        self.device, self.store, self.sid = device, store, session_id
        self.interval, self.width = interval, width
        self.on_error = on_error      # (message) -> None, reported once until it recovers
        self._stop = threading.Event()
        self._thread = None
        self._failing = False

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="screenshots")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                raw = self.device.screenshot()
                # timestamp the moment of capture, not when the transfer finished
                self.store.add_shot(self.sid, int(t0 * 1000), shrink(raw, self.width))
                self._failing = False
            except NotImplementedError:
                self.on_error("这个平台不支持截图")
                return
            except Exception as e:
                if not self._failing:
                    self.on_error(f"截图失败: {e}")
                    self._failing = True
            self._stop.wait(max(0.0, self.interval - (time.time() - t0)))
