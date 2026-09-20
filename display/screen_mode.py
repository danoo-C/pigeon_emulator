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


def mode_label(w: int, h: int) -> str:
    """A mode as the picker names it -- the page's modeLabel, the same."""
    return f"{w} x {h}"


def mode_items(modes, w: int, h: int):
    """The Mode picker's list (docs/gac/plans/phase6_console.md §3): /info's
    modes, the current one marked, as the page's modeOptions has them."""
    return [{"name": mode_label(mw, mh) + ("   (now)" if (mw, mh) == (w, h) else ""),
             "w": mw, "h": mh, "current": (mw, mh) == (w, h)}
            for mw, mh in (modes or [[w, h]])]


def parse_rows(header) -> Optional[Tuple[int, int]]:
    """X-Pigeon-Rows: "first,last" -> (first, last), the rows a
    /frame?since=N reply holds (docs/gac/plans/phase8_bandwidth.md); None for
    a reply without one, or a malformed one. The page's parseRows."""
    try:
        first, last = (int(part) for part in str(header).split(","))
    except (TypeError, ValueError):
        return None
    if first < 0 or last < first:
        return None
    return first, last


def apply_band(frame: bytearray, band: bytes, first: int, w: int) -> bytearray:
    """A band of rows into the frame held, made into the one the reply is.
    The page's applyBand, without the swizzle: pygame reads B,G,R,A as it is."""
    at = first * w * 4
    frame[at:at + len(band)] = band
    return frame

