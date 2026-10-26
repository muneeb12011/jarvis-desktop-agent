"""
Screen capture. Grabs a monitor with mss, downsizes it for the model, and
keeps the geometry needed to map the model's coordinates back to real pixels
(including the monitor's offset on multi-monitor setups).
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image

MAX_EDGE = 1280


@dataclass
class Frame:
    image: Image.Image  # scaled image the model sees
    scaled_size: tuple[int, int]
    original_size: tuple[int, int]
    offset: tuple[int, int]  # monitor's top-left in virtual-desktop pixels
    full: Image.Image | None = None  # unscaled capture, for OCR

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        sx, sy = self.scaled_size
        ox, oy = self.original_size
        return int(self.offset[0] + x * ox / sx), int(self.offset[1] + y * oy / sy)

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        sx, sy = self.scaled_size
        ox, oy = self.original_size
        return int((x - self.offset[0]) * sx / ox), int((y - self.offset[1]) * sy / oy)


def monitor_count() -> int:
    import mss

    with mss.mss() as sct:
        return len(sct.monitors) - 1


def grab(monitor: int = 1) -> Frame:
    import mss

    with mss.mss() as sct:
        monitors = sct.monitors
        mon = monitors[monitor] if 0 < monitor < len(monitors) else monitors[1]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    full = img
    w, h = img.size
    scale = min(1.0, MAX_EDGE / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    return Frame(img, img.size, (w, h), (mon["left"], mon["top"]), full)


def to_b64_jpeg(img: Image.Image, quality: int = 80, max_edge: int | None = None) -> str:
    if max_edge and max(img.size) > max_edge:
        r = max_edge / max(img.size)
        img = img.resize((int(img.width * r), int(img.height * r)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def capture_screen_b64() -> tuple[str, tuple[int, int], tuple[int, int]]:
    """v2-compatible helper."""
    f = grab()
    return to_b64_jpeg(f.image), f.scaled_size, f.original_size
