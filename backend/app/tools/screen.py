"""
Screen tools: see the screen and operate any app with mouse and keyboard.

screenshot() returns the image with every clickable control the app exposes
outlined and numbered. click_element(n) then clicks the exact control. Every
GUI action automatically returns a fresh annotated screenshot, so the model
always sees the result of what it just did without an extra round trip.
"""

from __future__ import annotations

import time
from typing import Any

from PIL import ImageDraw, ImageFont

from .. import actuator, capture, pixel_eyes, uia, winapi
from . import ToolResult, tool

STATE: dict[str, Any] = {"frame": None, "elements": []}

_COLORS = ["#ff3b6b", "#18e0ff", "#ffd400", "#7cff4f", "#ff8a00", "#c77dff"]


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _observe(note: str = "", monitor: int = 1, mark: bool = True) -> ToolResult:
    time.sleep(0.35)  # let the UI settle after an action
    frame = capture.grab(monitor)
    elements = uia.interactive_elements() if mark else []
    seen_note = ""
    if mark and frame.full is not None and pixel_eyes.should_look(len(elements)):
        elements, seen_note = _add_pixel_targets(frame, elements)
    img = frame.image.copy()
    lines: list[str] = []
    if elements:
        draw = ImageDraw.Draw(img)
        font = _font(13)
        for el in elements:
            l_, t_ = frame.to_image(el.rect[0], el.rect[1])
            r_, b_ = frame.to_image(el.rect[2], el.rect[3])
            if r_ < 0 or b_ < 0 or l_ > img.width or t_ > img.height:
                continue
            color = _COLORS[el.id % len(_COLORS)]
            seen = el.control_type in ("SeenText", "SeenIcon")
            draw.rectangle([l_, t_, r_, b_], outline=color, width=1 if seen else 2)
            label = str(el.id)
            tw = draw.textlength(label, font=font)
            draw.rectangle([l_, t_, l_ + tw + 6, t_ + 16], fill=color)
            draw.text((l_ + 3, t_ + 1), label, fill="#000000", font=font)
            cx, cy = frame.to_image(*el.center)
            if el.control_type == "SeenText":
                lines.append(f"[{el.id}] text on screen '{el.name}' at ({cx},{cy})")
            elif el.control_type == "SeenIcon":
                lines.append(f"[{el.id}] icon/shape (no label; check the image) at ({cx},{cy})")
            else:
                lines.append(f"[{el.id}] {el.control_type} '{el.name}' at ({cx},{cy})")
    STATE["frame"] = frame
    STATE["elements"] = elements
    title = uia.foreground_title()
    header = [note] if note else []
    header.append(
        f"Screen {frame.scaled_size[0]}x{frame.scaled_size[1]} (coordinates below are in this space). "
        f"Focused window: '{title or 'unknown'}'."
    )
    if seen_note:
        header.append(seen_note)
    if lines:
        header.append("Clickable controls (prefer click_element with these ids):\n" + "\n".join(lines))
    else:
        header.append("No accessible controls detected - use click(x, y) based on the image.")
    return ToolResult("\n".join(header), image_b64=capture.to_b64_jpeg(img))


def _add_pixel_targets(frame, elements: list) -> tuple[list, str]:
    """The app exposes few controls: read its pixels for text labels and icons, numbered after the real ones."""
    fg = winapi.foreground()
    left, top = frame.offset
    region = (
        fg.rect if fg and not fg.minimized else (left, top, left + frame.original_size[0], top + frame.original_size[1])
    )
    try:
        boxes = pixel_eyes.targets(frame.full, region, frame.offset, [e.rect for e in elements])
    except Exception:
        return elements, ""
    if not boxes:
        return elements, ""
    start = len(elements)
    extra = [
        uia.Element(start + i + 1, b.text, "SeenText" if b.kind == "text" else "SeenIcon", b.rect)
        for i, b in enumerate(boxes)
    ]
    return elements + extra, (
        "This app exposes few accessible controls, so text and icons were read from the pixels "
        f"([{start + 1}] onward). Clicking them uses their exact on-screen position."
    )


def _frame():
    if STATE["frame"] is None:
        STATE["frame"] = capture.grab()
    return STATE["frame"]


@tool(
    "screenshot",
    "Look at the screen. Returns the image with clickable controls outlined and numbered. "
    "Use before interacting with any app's UI.",
    {"monitor": {"type": "integer", "description": "1 = primary (default)"}},
    category="screen",
    ui_thread=True,
)
def screenshot(monitor: int = 1) -> ToolResult:
    return _observe(monitor=monitor)


@tool(
    "click_element",
    "Click a numbered control from the latest screenshot. Most reliable way to click.",
    {"id": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right"]}, "double": {"type": "boolean"}},
    ["id"],
    category="screen",
    ui_thread=True,
    mutates=True,
)
def click_element(id: int, button: str = "left", double: bool = False) -> ToolResult:  # noqa: A002
    match = next((e for e in STATE["elements"] if e.id == int(id)), None)
    if not match:
        return ToolResult(f"No element [{id}] in the latest screenshot. Take a new screenshot.", ok=False)
    x, y = match.center
    actuator.click(x, y, button=button, clicks=2 if double else 1)
    return _observe(f"Clicked [{id}] {match.control_type} '{match.name}'.")


@tool(
    "click",
    "Click at x,y in the latest screenshot's coordinate space (use when there is no numbered control for the target).",
    {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "button": {"type": "string", "enum": ["left", "right", "middle"]},
        "double": {"type": "boolean"},
    },
    ["x", "y"],
    category="screen",
    ui_thread=True,
    mutates=True,
)
def click(x: int, y: int, button: str = "left", double: bool = False) -> ToolResult:
    sx, sy = _frame().to_screen(x, y)
    actuator.click(sx, sy, button=button, clicks=2 if double else 1)
    return _observe(f"Clicked at ({x},{y}).")


@tool(
    "type_text",
    "Type text into whatever has keyboard focus. Check the focused window first so you never type into an "
    "existing document by accident. Optionally click a numbered field first and/or press Enter after.",
    {"text": {"type": "string"}, "element_id": {"type": "integer"}, "press_enter": {"type": "boolean"}},
    ["text"],
    category="screen",
    ui_thread=True,
    mutates=True,
)
def type_text(text: str, element_id: int | None = None, press_enter: bool = False) -> ToolResult:
    if element_id is not None:
        match = next((e for e in STATE["elements"] if e.id == int(element_id)), None)
        if match:
            actuator.click(*match.center)
            time.sleep(0.15)
    actuator.type_text(text)
    if press_enter:
        actuator.hotkey(["enter"])
    return _observe(f"Typed {len(text)} characters{' and pressed Enter' if press_enter else ''}.")


@tool(
    "press_keys",
    "Press a key or a shortcut, e.g. ['enter'], ['ctrl','s'], ['alt','tab'], ['win'].",
    {"keys": {"type": "array", "items": {"type": "string"}}},
    ["keys"],
    category="screen",
    ui_thread=True,
    mutates=True,
)
def press_keys(keys: list[str]) -> ToolResult:
    actuator.hotkey(keys)
    return _observe(f"Pressed {'+'.join(actuator.normalize_keys(keys))}.")


@tool(
    "scroll",
    "Scroll the mouse wheel (negative = down). Optionally over a point first.",
    {
        "amount": {"type": "integer", "description": "e.g. -5 (down) or 5 (up), in notches"},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
    },
    ["amount"],
    category="screen",
    ui_thread=True,
)
def scroll(amount: int, x: int | None = None, y: int | None = None) -> ToolResult:
    pos = _frame().to_screen(x, y) if x is not None and y is not None else (None, None)
    actuator.scroll(int(amount) * 120, *pos)
    return _observe(f"Scrolled {amount}.")


@tool(
    "drag",
    "Drag from one point to another (latest screenshot coordinates).",
    {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"}},
    ["x1", "y1", "x2", "y2"],
    category="screen",
    ui_thread=True,
    mutates=True,
)
def drag(x1: int, y1: int, x2: int, y2: int) -> ToolResult:
    f = _frame()
    actuator.drag(*f.to_screen(x1, y1), *f.to_screen(x2, y2))
    return _observe("Dragged.")


@tool(
    "wait",
    "Wait for something to load (max 15s), then look at the screen again.",
    {"seconds": {"type": "number"}},
    ["seconds"],
    category="screen",
    ui_thread=True,
)
def wait(seconds: float) -> ToolResult:
    time.sleep(max(0.2, min(float(seconds), 15)))
    return _observe(f"Waited {seconds}s.")
