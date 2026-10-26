"""
Pixel eyes: numbered click targets for apps that don't describe themselves.

UI Automation gives exact buttons for most Windows apps, but plenty of apps
expose almost nothing: games, many Electron apps, remote desktops, video
players, canvas tools. Before this, Jarvis fell back to guessing pixel
coordinates from a downscaled screenshot.

Now, when the accessibility tree is thin, Jarvis reads the window's pixels:
- every word/label on screen via OCR (Windows' built-in OCR engine when
  available, RapidOCR otherwise), grouped into clickable phrases, and
- icon-sized shapes (toolbar buttons, close/settings icons) via edge detection.

These become extra numbered targets on the screenshot, so the model can say
"click [14]" for the 'Export' label in a canvas app exactly like it does for
real buttons. All of it runs locally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PIL import Image

from .platform_utils import IS_WINDOWS

logger = logging.getLogger(__name__)

MODE = os.environ.get("JARVIS_PIXEL_EYES", "auto").strip().lower()  # auto | always | off
THIN_TREE = 8  # fewer UI Automation controls than this in the window -> also read the pixels
MAX_TEXT = 70
MAX_ICONS = 30

# WinRT wants its own apartment; the GUI thread is a COM STA for pywinauto.
_OCR_POOL = ThreadPoolExecutor(1, thread_name_prefix="jarvis-ocr")
_lock = threading.Lock()
_engine_name: str | None = None
_rapid = None


@dataclass
class Box:
    text: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom in the input image's pixels
    kind: str = "text"  # text | icon


# ------------------------------------------------------------------ OCR engines


def _winrt_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winrt.windows.media.ocr  # noqa: F401

        return True
    except Exception:
        return False


async def _winrt_ocr_async(path: str) -> list[Box]:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("No OCR language installed in Windows")
    file = await StorageFile.get_file_from_path_async(path)
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    out: list[Box] = []
    for line in result.lines:
        for w in line.words:
            r = w.bounding_rect
            out.append(Box(w.text, (int(r.x), int(r.y), int(r.x + r.width), int(r.y + r.height))))
    return out


def _winrt_ocr(img: Image.Image) -> list[Box]:
    # Windows OCR refuses images larger than OcrEngine.MaxImageDimension (2600 px on current builds).
    scale = min(1.0, 2600 / max(img.size))
    src = img if scale == 1.0 else img.resize((int(img.width * scale), int(img.height * scale)))
    fd, path = tempfile.mkstemp(suffix=".png", prefix="jarvis-ocr-")
    os.close(fd)
    try:
        src.save(path, format="PNG", compress_level=1)
        words = asyncio.run(_winrt_ocr_async(path))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if scale != 1.0:
        words = [Box(w.text, tuple(int(v / scale) for v in w.rect)) for w in words]  # type: ignore[misc]
    return group_words(words)


def _rapid_ocr(img: Image.Image) -> list[Box]:
    global _rapid
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    if _rapid is None:
        _rapid = RapidOCR()
    result, _ = _rapid(np.array(img.convert("RGB")), return_word_box=True)
    out: list[Box] = []
    for item in result or []:
        box, text, score = item[0], item[1], item[2]
        if score < 0.5 or not text.strip():
            continue
        chars = item[3] if len(item) > 4 else None
        letters = item[4] if len(item) > 4 else None
        if chars and letters and len(chars) == len(letters):
            out.extend(split_by_gaps(chars, letters))
        else:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            out.append(Box(text, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))))
    return out


def ocr(img: Image.Image) -> list[Box]:
    """Clickable text phrases in the image, best engine first."""
    global _engine_name
    with _lock:
        engines = []
        if _engine_name in (None, "windows") and _winrt_available():
            engines.append(("windows", _winrt_ocr))
        engines.append(("rapidocr", _rapid_ocr))
        for name, fn in engines:
            try:
                boxes = _OCR_POOL.submit(fn, img).result(timeout=20)
                if _engine_name != name:
                    logger.info("Pixel eyes: using %s OCR", name)
                _engine_name = name
                return boxes
            except ImportError:
                continue
            except Exception:
                logger.warning("%s OCR failed", name, exc_info=True)
                if name == "windows":
                    _engine_name = "rapidocr"
        return []


def engine_name() -> str:
    if _engine_name:
        return _engine_name
    if _winrt_available():
        return "windows"
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return "rapidocr"
    except Exception:
        return "none"


# ------------------------------------------------------------------ grouping


def group_words(words: list[Box]) -> list[Box]:
    """Join words on the same line that sit close together ('Save' 'changes' -> 'Save changes'),
    but keep widely spaced items apart (menu bars: 'File   Edit   View')."""
    words = sorted((w for w in words if w.text.strip()), key=lambda w: (round(w.rect[1] / 6), w.rect[0]))
    out: list[Box] = []
    for w in words:
        if out:
            p = out[-1]
            h = max(p.rect[3] - p.rect[1], w.rect[3] - w.rect[1], 1)
            same_line = abs((p.rect[1] + p.rect[3]) / 2 - (w.rect[1] + w.rect[3]) / 2) < h * 0.5
            gap = w.rect[0] - p.rect[2]
            if same_line and -2 <= gap < h * 0.75:
                out[-1] = Box(
                    f"{p.text} {w.text}",
                    (p.rect[0], min(p.rect[1], w.rect[1]), w.rect[2], max(p.rect[3], w.rect[3])),
                )
                continue
        out.append(Box(w.text, w.rect))
    return out


def split_by_gaps(char_boxes: list, letters: list[str]) -> list[Box]:
    """RapidOCR returns one box per line; split it where the space between letters is wide."""
    rects = [
        (min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b)) for b in char_boxes
    ]
    h = max(1, sorted(r[3] - r[1] for r in rects)[len(rects) // 2])
    out: list[Box] = []
    cur_text, cur = letters[0], list(rects[0])
    for r, ch in zip(rects[1:], letters[1:], strict=False):
        if r[0] - cur[2] >= h * 0.6:
            out.append(Box(cur_text, tuple(cur)))  # type: ignore[arg-type]
            cur_text, cur = ch, list(r)
        else:
            cur_text += ch
            cur = [min(cur[0], r[0]), min(cur[1], r[1]), max(cur[2], r[2]), max(cur[3], r[3])]
    out.append(Box(cur_text, tuple(cur)))  # type: ignore[arg-type]
    return out


# ------------------------------------------------------------------ icons


def find_icons(img: Image.Image, avoid: list[tuple[int, int, int, int]], limit: int = MAX_ICONS) -> list[Box]:
    """Icon-sized, roughly square shapes with clear edges (toolbar buttons, close/settings glyphs)."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    # RETR_LIST, not EXTERNAL: a window frame is one big closed contour that would hide everything inside it.
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands: list[tuple[float, tuple[int, int, int, int]]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (12 <= w <= 64 and 12 <= h <= 64) or not (0.6 <= w / h <= 1.67):
            continue
        rect = (x, y, x + w, y + h)
        if any(overlap(rect, a) > 0.3 for a in avoid):
            continue
        fill = cv2.contourArea(c) / float(w * h)
        cands.append((fill, rect))
    picked: list[Box] = []
    for _fill, rect in sorted(cands, key=lambda t: -t[0]):
        if any(overlap(rect, p.rect) > 0.2 for p in picked):
            continue
        picked.append(Box("", rect, "icon"))
        if len(picked) >= limit:
            break
    return picked


def overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over the smaller box's area."""
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return iw * ih / small


# ------------------------------------------------------------------ entry point

_NOISE_TEXT = re.compile(r"^[\W_]{1,3}$")


def should_look(n_uia: int) -> bool:
    if MODE == "off":
        return False
    return MODE == "always" or n_uia < THIN_TREE


def targets(
    full: Image.Image,
    region: tuple[int, int, int, int],
    screen_offset: tuple[int, int],
    taken: list[tuple[int, int, int, int]],
) -> list[Box]:
    """Targets inside `region` (screen coordinates) of a full-resolution monitor image.
    Returned rects are in screen coordinates; anything overlapping `taken` (UIA controls) is dropped."""
    t0 = time.monotonic()
    ox, oy = screen_offset
    left = max(0, region[0] - ox)
    top = max(0, region[1] - oy)
    right = min(full.width, region[2] - ox)
    bottom = min(full.height, region[3] - oy)
    if right - left < 40 or bottom - top < 40:
        return []
    crop = full.crop((left, top, right, bottom))
    local_taken = [(a - ox - left, b - oy - top, c - ox - left, d - oy - top) for a, b, c, d in taken]
    texts = [
        b
        for b in ocr(crop)
        if len(b.text.strip()) >= 2
        and not _NOISE_TEXT.match(b.text.strip())
        and not any(overlap(b.rect, t) > 0.5 for t in local_taken)
    ][:MAX_TEXT]
    icons = find_icons(crop, [b.rect for b in texts] + local_taken)
    out = [
        Box(
            b.text.strip()[:60],
            (b.rect[0] + left + ox, b.rect[1] + top + oy, b.rect[2] + left + ox, b.rect[3] + top + oy),
            b.kind,
        )
        for b in texts + icons
    ]
    logger.debug("pixel eyes: %d text, %d icons in %.2fs", len(texts), len(icons), time.monotonic() - t0)
    return out
