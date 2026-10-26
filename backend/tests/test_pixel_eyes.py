"""Pixel eyes: grouping OCR words into click targets, icons, and the region/offset maths."""

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import pixel_eyes
from app.pixel_eyes import Box


def test_group_words_joins_phrases_but_not_menu_items():
    words = [
        Box("File", (10, 10, 40, 28)),
        Box("Edit", (80, 10, 110, 28)),  # wide gap: separate menu item
        Box("Save", (300, 100, 340, 118)),
        Box("changes", (345, 100, 410, 118)),  # normal word space: same button
        Box("Cancel", (300, 140, 360, 158)),  # next line
    ]
    got = [b.text for b in pixel_eyes.group_words(words)]
    assert got == ["File", "Edit", "Save changes", "Cancel"]


def test_split_by_gaps():
    def box(x0, x1):
        return [[x0, 0], [x1, 0], [x1, 18], [x0, 18]]

    chars = [box(0, 7), box(8, 14), box(15, 20), box(21, 28), box(45, 52), box(53, 60)]
    out = pixel_eyes.split_by_gaps(chars, list("FileEd"))
    assert [b.text for b in out] == ["File", "Ed"]
    assert out[1].rect[0] == 45


def test_overlap():
    assert pixel_eyes.overlap((0, 0, 10, 10), (5, 5, 20, 20)) == 0.25
    assert pixel_eyes.overlap((0, 0, 10, 10), (20, 20, 30, 30)) == 0


def test_targets_maps_to_screen_and_skips_known_controls(monkeypatch):
    img = Image.new("RGB", (1000, 800), "white")
    seen = {}

    def fake_ocr(crop):
        seen["size"] = crop.size
        return [Box("Export", (10, 10, 70, 30)), Box("Play", (200, 200, 240, 220)), Box("|", (300, 300, 304, 320))]

    monkeypatch.setattr(pixel_eyes, "ocr", fake_ocr)
    monkeypatch.setattr(pixel_eyes, "find_icons", lambda *_a, **_k: [])
    # window at screen (1100, 50)-(1600, 450) on a second monitor whose origin is (1000, 0)
    taken = [(1300, 250, 1350, 280)]  # a real UIA button already covering "Play"
    out = pixel_eyes.targets(img, (1100, 50, 1600, 450), (1000, 0), taken)
    assert seen["size"] == (500, 400)
    assert [b.text for b in out] == ["Export"]
    assert out[0].rect == (1110, 60, 1170, 80)


def test_should_look(monkeypatch):
    monkeypatch.setattr(pixel_eyes, "MODE", "auto")
    assert pixel_eyes.should_look(2) and not pixel_eyes.should_look(30)
    monkeypatch.setattr(pixel_eyes, "MODE", "off")
    assert not pixel_eyes.should_look(0)


def test_icons_found_on_a_toolbar():
    pytest.importorskip("cv2")
    img = Image.new("RGB", (400, 120), "#202020")
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 397, 117], outline="#808080", width=1)  # a window frame around the toolbar
    for x in (20, 80, 140):
        d.rounded_rectangle([x, 30, x + 32, 62], radius=6, outline="#e0e0e0", width=2)
        d.line([x + 8, 46, x + 24, 46], fill="#e0e0e0", width=3)
    icons = pixel_eyes.find_icons(img, avoid=[])
    lefts = sorted(b.rect[0] for b in icons)
    assert len(icons) >= 3 and all(any(abs(lx - x) < 8 for lx in lefts) for x in (20, 80, 140))


def test_real_ocr_reads_rendered_text():
    pytest.importorskip("rapidocr_onnxruntime")
    img = Image.new("RGB", (700, 200), "#1e1e1e")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        pytest.skip("no font")
    d.text((40, 40), "File      Edit      View", fill="white", font=f)
    d.text((400, 140), "Export", fill="white", font=f)
    boxes = pixel_eyes._rapid_ocr(img)
    texts = [b.text for b in boxes]
    assert "Export" in texts and "File" in texts and "Edit" in texts
