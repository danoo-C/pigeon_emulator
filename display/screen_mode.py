"""The pygame client's screen-mode logic, with no pygame in it
(docs/gac/plans/phase4_frontends.md), as serial_panel.py is for the Serial
panel: tests/test_frontends.py imports it without a display.

The browser page has the same three decisions in its "screen's logic"
section; the two must agree, and the tests hold both to the same cases.
"""
from typing import Optional, Tuple

#: What the window keeps clear of the desktop's edges: its title bar, the
#: taskbar, a little air. The toolbar and the Serial panel are counted
#: separately, by whoever asks.
DESKTOP_MARGIN_W = 40
DESKTOP_MARGIN_H = 100


def parse_mode(header) -> Optional[Tuple[int, int, int]]:
    """X-Pigeon-Mode: "w,h,generation" -> (w, h, generation), or None for a
    missing or malformed header -- which a frame from an emulator older
    than the header has."""
    try:
        w, h, generation = (int(part) for part in str(header).split(","))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or generation < 0:
        return None
    return w, h, generation


def fit_pixel_size(w: int, h: int, room_w: int, room_h: int, wanted: int) -> int:
    """The largest whole pixel size, at least 1, at which a w x h screen
    fits the room, and never more than the size asked for: a small mode
    stays at yours, a big one does not open wider than the desktop."""
    fits = min(room_w // w, room_h // h)
    return max(1, min(int(wanted) or 1, fits))


def room_for_screen(desktop_w: int, desktop_h: int, beside: int, above: int) -> Tuple[int, int]:
    """The room the guest's screen has on the desktop, with `beside` pixels
    taken by the Serial panel and `above` by the toolbar."""
    return (desktop_w - DESKTOP_MARGIN_W - beside, desktop_h - DESKTOP_MARGIN_H - above)
