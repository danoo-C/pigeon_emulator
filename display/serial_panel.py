"""The pygame client's Serial panel, as plain Python (docs/phase5c_plan.md §3).

Nothing here imports pygame: display/display.py draws what these work out,
and tests/test_serial_panel.py runs them without a window. What the machine
writes to its debug port comes from GET /serial -- text, with the index and
time of each line that starts in it (emulator/devices/debug_port.py,
serial_reply) -- and is put together here as the browser page's panel does.
"""
import json
from pathlib import Path

MIN_WIDTH = 160           # the narrowest panel, as in the browser
DEFAULT_WIDTH = 320
MAX_LINES = 2000          # lines kept; the machine itself keeps its last 64 KB
HEADER_H = 24             # the panel's header, under the toolbar
EDGE = 5                  # pixels inside the panel's right edge that grab it
BUTTON_W, BUTTON_H = 50, 18
WHEEL_ROWS = 3            # rows a wheel notch scrolls
POLL_INTERVAL = 0.25      # seconds between asks while the panel is open
NO_TIME = " " * 11        # as wide as "[   0.000] "
DEFAULTS = {"open": True, "width": DEFAULT_WIDTH, "times": True}


def clamp_width(width, screen_w):
    """Between MIN_WIDTH and 7/3 of the screen's width, which is 70% of the
    window: the browser's limit."""
    return max(MIN_WIDTH, min(int(width), screen_w * 7 // 3))


def layout(is_open, width, screen_w, screen_h, bar_h, bar_min_w):
    """The window's size, where the screen starts, and the panel's rect."""
    panel_w = width if is_open else 0
    return {"window": (max(panel_w + screen_w, bar_min_w), screen_h + bar_h),
            "screen_x": panel_w,
            "panel": (0, bar_h, panel_w, screen_h) if is_open else None}


def region(x, y, is_open, width, bar_h):
    """What a point in the window is over: "toolbar", "edge", "panel" or
    "screen". The edge is the panel's last few pixels, so a press on the
    screen's first column is still the guest's."""
    if y < bar_h:
        return "toolbar"
    if is_open and x < width:
        return "edge" if x >= width - EDGE else "panel"
    return "screen"


def on_screen(x, y, screen_x, bar_h, pixel_size, disp_w, disp_h):
    """A window point as the guest's pointer: from the screen's left edge and
    the toolbar's bottom, in the guest's pixels, clamped onto the screen.
    Over the panel that is x 0, as over the toolbar it is y 0."""
    guest_x = (x - screen_x) // pixel_size
    guest_y = (y - bar_h) // pixel_size
    return max(0, min(guest_x, disp_w - 1)), max(0, min(guest_y, disp_h - 1))


def header_buttons(width, bar_h):
    """The header's buttons, right-aligned: {"times": rect, "clear": rect}."""
    y = bar_h + (HEADER_H - BUTTON_H) // 2
    clear = (width - EDGE - 6 - BUTTON_W, y, BUTTON_W, BUTTON_H)
    times = (clear[0] - 6 - BUTTON_W, y, BUTTON_W, BUTTON_H)
    return {"times": times, "clear": clear}


def header_hit(x, y, width, bar_h):
    for name, (bx, by, bw, bh) in header_buttons(width, bar_h).items():
        if bx <= x < bx + bw and by <= y < by + bh:
            return name
    return None


def time_text(t):
    """As the launcher's terminal shows a line's time."""
    return NO_TIME if t is None else f"[{t:8.3f}] "


def check_reply(data):
    """A /serial reply with the right shape, or None."""
    try:
        start, following, lost = (int(data[key]) for key in ("start", "next", "lost"))
        text = data["text"]
        stamps = [(int(index), float(t)) for index, t in data["stamps"]]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(text, str) or start < 0 or following < start or lost < 0:
        return None
    return {"start": start, "next": following, "text": text, "stamps": stamps, "lost": lost}


class Transcript:
    """The panel's lines, each [time or None, text], put together from
    replies as the browser's serialAppend does."""

    def __init__(self):
        self.lines = []
        self.open = False           # the last line has no newline yet
        self.next = 0               # the offset to ask from
        self.changed = 0            # moves on every change, for the drawing

    def clear(self):
        """Empties the view only: the machine's log is untouched."""
        self.lines = []
        self.open = False
        self.changed += 1

    def append(self, reply):
        if reply["start"] < self.next:          # a new machine: its log starts again
            self.lines = []
            self.open = False
        if reply["lost"] > 0:
            self.lines.append([None, f"({reply['lost']} bytes lost)"])
            self.open = False
        times = dict(reply["stamps"])
        text, i = reply["text"], 0
        while i < len(text):
            if not self.open:
                self.lines.append([times.get(i), ""])
                self.open = True
            newline = text.find("\n", i)
            end = len(text) if newline == -1 else newline
            self.lines[-1][1] += text[i:end]
            if newline == -1:
                break
            self.open = False
            i = newline + 1
        del self.lines[:-MAX_LINES]
        if reply["next"] != self.next or reply["lost"] or text:
            self.changed += 1
        self.next = reply["next"]


def rows(lines, cols, times):
    """Every row the panel shows, oldest first, as (time, text). A line's time
    is on its first row only, and a long line wraps under it."""
    cols = max(1, cols)
    out = []
    for t, text in lines:
        prefix = time_text(t) if times else ""
        whole = prefix + text
        for i in range(0, max(len(whole), 1), cols):
            piece = whole[i:i + cols]
            if i == 0 and prefix:
                out.append((piece[:len(prefix)], piece[len(prefix):]))
            else:
                out.append(("", piece))
    return out


def visible_top(total, visible, top):
    """The first row shown: the last screenful while following (top None),
    or where the view was left."""
    last = max(0, total - visible)
    return last if top is None else min(top, last)


def wheel(total, visible, top, notches):
    """The wheel over the panel: each notch up goes WHEEL_ROWS back. Back at
    the end, the view follows new lines again (None)."""
    last = max(0, total - visible)
    moved = max(0, min(visible_top(total, visible, top) - notches * WHEEL_ROWS, last))
    return None if moved >= last else moved


def load_prefs(path):
    """The panel as it was last left, from build/display.json, or the
    defaults for a file that is missing or damaged."""
    prefs = dict(DEFAULTS)
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefs
    if not isinstance(saved, dict):
        return prefs
    if isinstance(saved.get("serial_open"), bool):
        prefs["open"] = saved["serial_open"]
    width = saved.get("serial_width")
    if isinstance(width, int) and not isinstance(width, bool):
        prefs["width"] = width
    if isinstance(saved.get("serial_times"), bool):
        prefs["times"] = saved["serial_times"]
    return prefs


def save_prefs(path, prefs):
    """Into build/display.json, keeping whatever else is in it. True if it
    was written; a failure costs only the memory of it."""
    path = Path(path)
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(kept, dict):
            kept = {}
    except (OSError, ValueError):
        kept = {}
    kept.update({"serial_open": bool(prefs["open"]), "serial_width": int(prefs["width"]),
                 "serial_times": bool(prefs["times"])})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
