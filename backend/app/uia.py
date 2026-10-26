"""
Windows UI Automation: asks the foreground app for its real buttons, fields,
links and menu items with exact rectangles. Jarvis numbers them on the
screenshot ("set-of-marks") so the model says "click [7]" instead of guessing
pixel coordinates from an image - the single biggest reliability upgrade
over v2's pure-vision clicking.

Optional: if pywinauto isn't installed (or the app exposes no accessibility
tree, like some games), Jarvis falls back to plain vision coordinates.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .platform_utils import IS_WINDOWS

logger = logging.getLogger(__name__)

INTERACTIVE = {
    "Button",
    "Edit",
    "Hyperlink",
    "ListItem",
    "MenuItem",
    "TabItem",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "TreeItem",
    "DataItem",
    "SplitButton",
    "Document",
    "MenuBar",
    "Slider",
    "Spinner",
}


@dataclass
class Element:
    id: int
    name: str
    control_type: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom in screen pixels

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


def foreground_title() -> str:
    if not IS_WINDOWS:
        return ""
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def interactive_elements(max_elements: int = 90, time_budget: float = 2.5) -> list[Element]:
    if not IS_WINDOWS:
        return []
    try:
        import ctypes

        from pywinauto.uia_element_info import UIAElementInfo
    except Exception:
        logger.warning("pywinauto unavailable; numbered controls disabled", exc_info=True)
        return []

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return []
    deadline = time.monotonic() + time_budget
    out: list[Element] = []
    seen: set[tuple[int, int, int, int]] = set()
    try:
        root = UIAElementInfo(hwnd)  # positional: the kwarg is handle_or_elem
        walker = getattr(root, "iter_descendants", None)
        nodes = walker() if walker else root.descendants()
        for info in nodes:
            if time.monotonic() > deadline or len(out) >= max_elements:
                break
            try:
                ctype = info.control_type
                if ctype not in INTERACTIVE:
                    continue
                r = info.rectangle
                rect = (r.left, r.top, r.right, r.bottom)
                if r.right - r.left < 4 or r.bottom - r.top < 4 or rect in seen:
                    continue
                if not info.visible or not info.enabled:
                    continue
                name = (info.name or "").strip().replace("\n", " ")
                if not name and ctype not in ("Edit", "Document", "ComboBox"):
                    continue
                seen.add(rect)
                out.append(Element(len(out) + 1, name[:60], ctype, rect))
            except Exception:
                continue
    except Exception:
        # Logged loudly: without UI Automation, clicks fall back to pixel guesses.
        logger.warning("UI Automation walk failed", exc_info=True)
    return out
