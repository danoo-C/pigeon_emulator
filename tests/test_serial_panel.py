"""The Serial panel, in both front ends: docs/phase5c_plan.md.

What the machine writes to its debug port is shown beside its screen. The
browser page's panel logic is plain JavaScript, lifted out of the page and
run under node, as tests/test_input.py runs the outbox; the listeners that
must not post to HID are checked in the page's source. The pygame client's
logic is display/serial_panel.py, which imports no pygame.

    python3 tests/test_serial_panel.py      (or: python3 -m pytest tests/)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module                                        # noqa: E402

PAGE = REPO_ROOT / "display" / "index.html"


# --- the browser ---------------------------------------------------------------------

def page():
    return PAGE.read_text()


def between(text, start, end):
    """The source from the line `start` begins to the line `end` begins."""
    assert start in text and end in text, f"index.html has lost {start!r} or {end!r}"
    return text[text.index(start):text.index(end)]


def panel_logic():
    return between(page(), "// ---- the Serial panel's logic", "// ---- end of the Serial panel's logic")


def panel_wiring():
    return between(page(), "// ---- the Serial panel ----", "// ---- end of the Serial panel ----")


def node(script):
    """Run the panel's logic and then `script` under node; its last line of
    output, as JSON. node is not optional for these tests."""
    binary = shutil.which("node")
    assert binary, "node is needed to run the page's logic (docs/phase5c_plan.md §1)"
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(panel_logic() + "\n" + script)
        path = f.name
    try:
        done = subprocess.run([binary, path], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


def test_the_page_puts_lines_together_from_replies():
    """A line split across two replies is one line, with the time it started."""
    got = node("""
const view = serialView();
serialAppend(view, {start: 0, next: 25, text: "[bios] bios2\\n[bios2] Pro", stamps: [[0, 0], [13, 0.04]], lost: 0});
const half = JSON.parse(JSON.stringify(view));
serialAppend(view, {start: 25, next: 36, text: "gram: none\\n", stamps: [], lost: 0});
console.log(JSON.stringify({half, view}));
""")
    assert got["half"] == {"lines": [{"t": 0, "text": "[bios] bios2"}, {"t": 0.04, "text": "[bios2] Pro"}],
                           "open": True, "next": 25}
    assert got["view"] == {"lines": [{"t": 0, "text": "[bios] bios2"},
                                     {"t": 0.04, "text": "[bios2] Program: none"}],
                           "open": False, "next": 36}


def test_the_page_notes_lost_bytes_and_starts_again_for_a_new_machine():
    got = node("""
const lost = serialView();
serialAppend(lost, {start: 0, next: 10, text: "old line\\n", stamps: [[0, 1]], lost: 0});
serialAppend(lost, {start: 50, next: 54, text: "abc\\n", stamps: [], lost: 40});
const fresh = serialView();
serialAppend(fresh, {start: 0, next: 100, text: "before\\n", stamps: [[0, 9]], lost: 0});
serialAppend(fresh, {start: 0, next: 4, text: "new\\n", stamps: [[0, 0.5]], lost: 0});
console.log(JSON.stringify({lost: lost.lines, fresh: fresh.lines, next: fresh.next}));
""")
    assert got["lost"] == [{"t": 1, "text": "old line"}, {"t": None, "text": "(40 bytes lost)"},
                           {"t": None, "text": "abc"}]
    assert got["fresh"] == [{"t": 0.5, "text": "new"}] and got["next"] == 4


def test_the_page_keeps_the_last_2000_lines():
    got = node("""
const view = serialView();
let text = "", stamps = [];
for (let i = 0; i < 2500; i++) { stamps.push([text.length, i]); text += "line " + i + "\\n"; }
serialAppend(view, {start: 0, next: text.length, text, stamps, lost: 0});
console.log(JSON.stringify({count: view.lines.length, first: view.lines[0], last: view.lines[1999]}));
""")
    assert got == {"count": 2000, "first": {"t": 500, "text": "line 500"},
                   "last": {"t": 2499, "text": "line 2499"}}


def test_the_page_shows_times_as_the_terminal_does():
    got = node("""
console.log(JSON.stringify([serialTime(1.5), serialTime(0), serialTime(1234.5678), serialTime(null)]));
""")
    assert got == ["[   1.500] ", "[   0.000] ", "[1234.568] ", " " * 11]


def test_the_page_follows_new_lines_and_clamps_the_width():
    got = node("""
console.log(JSON.stringify({
  follows: [serialFollows(0, 100, 100), serialFollows(398, 100, 500), serialFollows(0, 100, 500)],
  widths: [serialClampWidth(100, 1000), serialClampWidth(500, 1000), serialClampWidth(900, 1000),
           serialClampWidth(300, 200)],
}));
""")
    assert got == {"follows": [True, True, False], "widths": [160, 500, 700, 160]}


def test_the_page_opens_the_panel_the_first_time_then_as_it_was_left():
    got = node("""
console.log(JSON.stringify([
  serialPrefs(null),
  serialPrefs('{"open": false, "width": 250, "times": false}'),
  serialPrefs('{"open": fal'),
  serialPrefs('{"open": "yes", "width": "wide", "times": 1}'),
  serialPrefs('[1, 2]'),
]));
""")
    defaults = {"open": True, "width": 320, "times": True}
    assert got == [defaults, {"open": False, "width": 250, "times": False}, defaults, defaults, defaults]


def test_the_panel_sits_left_of_the_screen_and_opens_from_the_controls():
    html = page()
    body = html[html.index("<body>"):html.index("<script>")]
    controls = between(body, '<div id="controls">', '<div id="cd">')
    assert 'id="serial-button"' in controls, "no Serial button in the controls"
    main = body[body.index('<div id="main">'):]
    assert main.index('id="serial"') < main.index('id="screen"'), "the panel is not left of the screen"
    for part in ('id="serial-times"', 'id="serial-clear"', 'id="serial-close"', 'id="serial-edge"',
                 'id="serial-text"'):
        assert part in main, f"the panel has no {part}"


def test_nothing_in_the_panel_posts_to_hid():
    wiring = panel_wiring()
    assert "post(" not in wiring, "the Serial panel posts to HID"
    html = page()
    up = re.search(r"window\.addEventListener\('mouseup'.*?\n      \}\);", html, re.DOTALL)
    assert up, "no mouseup listener on the window"
    body = up.group(0)
    assert "if (!(held & (1 << e.button))) return;" in body, \
        "the window's mouseup releases buttons that went down outside the screen"
    assert body.index("held & (1 << e.button)") < body.index("post("), body
    move = re.search(r"window\.addEventListener\('mousemove'.*?\n      \}\);", html, re.DOTALL)
    assert move and "if (serialResizing) return;" in move.group(0), \
        "dragging the panel's edge moves the guest's pointer"
    assert move.group(0).index("serialResizing") < move.group(0).index("post("), move.group(0)


def test_the_panel_polls_serial_while_open_and_keeps_its_settings():
    wiring = panel_wiring()
    assert "fetch('/serial?from=' + view.next)" in wiring
    assert "pollTimer = setTimeout(poll, 250)" in wiring
    assert "if (polling || serialPanel.hidden) return;" in wiring, "it polls while closed"
    assert "localStorage.getItem(SERIAL_KEY)" in wiring and "localStorage.setItem(SERIAL_KEY" in wiring
    assert re.search(r"try \{ prefs = serialPrefs\(localStorage\.getItem\(SERIAL_KEY\)\); \}", wiring), \
        "a blocked localStorage would stop the page"
    assert "wireSerial();" in page()


# --- the pygame client ---------------------------------------------------------------

sys.path.insert(0, str(REPO_ROOT / "display"))
import serial_panel as SP                                              # noqa: E402

BAR = 40
SCREEN_W, SCREEN_H = 192 * 4, 108 * 4


def test_the_window_grows_by_the_panel_and_the_screen_moves_right():
    assert SP.layout(False, 320, SCREEN_W, SCREEN_H, BAR, 600) == {
        "window": (SCREEN_W, SCREEN_H + BAR), "screen_x": 0, "panel": None}
    assert SP.layout(True, 320, SCREEN_W, SCREEN_H, BAR, 600) == {
        "window": (320 + SCREEN_W, SCREEN_H + BAR), "screen_x": 320,
        "panel": (0, BAR, 320, SCREEN_H)}
    assert SP.layout(True, 160, 192, 108, BAR, 600)["window"] == (600, 148), \
        "the toolbar's width still counts"


def test_the_guests_pointer_is_measured_from_the_screen():
    assert SP.on_screen(320 + 16, BAR + 8, 320, BAR, 4, 192, 108) == (4, 2)
    assert SP.on_screen(10, 100, 320, BAR, 4, 192, 108) == (0, 15), "over the panel it's x 0"
    assert SP.on_screen(9999, 9999, 320, BAR, 4, 192, 108) == (191, 107)
    assert SP.on_screen(16, BAR + 8, 0, BAR, 4, 192, 108) == (4, 2), "with the panel closed"


def test_what_a_point_is_over():
    over = lambda x, y, is_open=True: SP.region(x, y, is_open, 320, BAR)
    assert [over(100, 10), over(100, 100), over(316, 100), over(319, 100), over(320, 100)] == [
        "toolbar", "panel", "edge", "edge", "screen"]
    assert over(100, 100, is_open=False) == "screen"
    buttons = SP.header_buttons(320, BAR)
    for name, (x, y, w, h) in buttons.items():
        assert SP.header_hit(x + w // 2, y + h // 2, 320, BAR) == name
        assert x + w < 320 - SP.EDGE, f"{name} overlaps the edge"
    assert SP.header_hit(10, BAR + 5, 320, BAR) is None


def test_the_width_is_clamped_as_in_the_browser():
    assert [SP.clamp_width(w, SCREEN_W) for w in (100, 400, 5000)] == [160, 400, SCREEN_W * 7 // 3]
    assert SCREEN_W * 7 // 3 / (SCREEN_W * 7 // 3 + SCREEN_W) == 0.7


def test_the_transcript_puts_lines_together_as_the_page_does():
    t = SP.Transcript()
    t.append({"start": 0, "next": 25, "text": "[bios] bios2\n[bios2] Pro", "stamps": [(0, 0), (13, 0.04)],
              "lost": 0})
    assert (t.lines, t.open) == ([[0, "[bios] bios2"], [0.04, "[bios2] Pro"]], True)
    t.append({"start": 25, "next": 36, "text": "gram: none\n", "stamps": [], "lost": 0})
    assert (t.lines, t.open, t.next) == ([[0, "[bios] bios2"], [0.04, "[bios2] Program: none"]], False, 36)
    t.append({"start": 80, "next": 84, "text": "abc\n", "stamps": [], "lost": 44})
    assert t.lines[-2:] == [[None, "(44 bytes lost)"], [None, "abc"]]
    t.append({"start": 0, "next": 4, "text": "new\n", "stamps": [(0, 0.5)], "lost": 0})
    assert (t.lines, t.next) == ([[0.5, "new"]], 4), "a new machine starts the view again"
    changed = t.changed
    t.clear()
    assert t.lines == [] and t.next == 4 and t.changed > changed


def test_the_transcript_keeps_the_last_2000_lines():
    t = SP.Transcript()
    text = "".join(f"line {i}\n" for i in range(2500))
    stamps, at = [], 0
    for i in range(2500):
        stamps.append((at, float(i)))
        at += len(f"line {i}\n")
    t.append({"start": 0, "next": len(text), "text": text, "stamps": stamps, "lost": 0})
    assert len(t.lines) == 2000 and t.lines[0] == [500.0, "line 500"] and t.lines[-1] == [2499.0, "line 2499"]


def test_rows_wrap_with_the_time_on_a_lines_first_row():
    assert SP.rows([[1.5, "abcdefghij"], [None, ""]], 15, True) == [
        ("[   1.500] ", "abcd"), ("", "efghij"), (" " * 11, "")]
    assert SP.rows([[1.5, "abcdefghij"], [None, ""]], 4, False) == [
        ("", "abcd"), ("", "efgh"), ("", "ij"), ("", "")]


def test_the_view_follows_new_lines_until_the_wheel_moves_it():
    assert SP.visible_top(100, 10, None) == 90
    assert SP.wheel(100, 10, None, 1) == 87
    assert SP.visible_top(130, 10, 87) == 87, "new rows don't move a view scrolled back"
    assert SP.wheel(100, 10, 87, -1) is None, "back at the end, it follows again"
    assert SP.wheel(100, 10, 2, 5) == 0
    assert SP.visible_top(5, 10, None) == 0 and SP.wheel(5, 10, None, 3) is None


def test_a_reply_is_checked_before_it_is_used():
    good = {"start": 3, "next": 9, "text": "hello\n", "stamps": [[3, 1.25]], "lost": 3}
    assert SP.check_reply(good) == {"start": 3, "next": 9, "text": "hello\n", "stamps": [(3, 1.25)],
                                    "lost": 3}
    for bad in ({**good, "next": 2}, {**good, "text": 5}, {**good, "stamps": [[1]]},
                {k: v for k, v in good.items() if k != "lost"}, {**good, "start": "x"},
                [1, 2], "text", None):
        assert SP.check_reply(bad) is None, bad


def test_the_panel_is_remembered_in_a_file_and_a_bad_one_gives_the_defaults():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "build" / "display.json"
        assert SP.load_prefs(path) == SP.DEFAULTS == {"open": True, "width": 320, "times": True}
        assert SP.save_prefs(path, {"open": False, "width": 250, "times": False})
        assert SP.load_prefs(path) == {"open": False, "width": 250, "times": False}
        path.write_text('{"other": 1, "serial_open": "no", "serial_width": true}')
        assert SP.load_prefs(path) == SP.DEFAULTS
        assert SP.save_prefs(path, {"open": True, "width": 400, "times": True})
        assert json.loads(path.read_text())["other"] == 1, "the file's other settings were lost"
        path.write_text("{damaged")
        assert SP.load_prefs(path) == SP.DEFAULTS
        assert SP.save_prefs(Path(d) / "build" / "display.json" / "x", SP.DEFAULTS) is False


def pygame_client():
    """display/display.py, made without __init__: no window and no servers."""
    import threading
    import pygame                                                      # noqa: F401
    import display as client
    c = object.__new__(client.DisplayClient)
    c.disp_w, c.disp_h, c.pixel_size = 192, 108, 4
    c.serial_open, c.serial_width, c.serial_times = True, 320, True
    c._screen_x = 320
    c.buttons = []
    c.serial = SP.Transcript()
    c._serial_lock = threading.Lock()
    c._serial_rows = (None, [])
    c.sent, c.resized, c.saved = [], [], []
    c._send_mouse_pos = lambda x, y: c.sent.append(("position", x, y))
    c._send_mouse_button = lambda button, pressed: c.sent.append(("button", button, pressed))
    c._resize_window = lambda: c.resized.append(c.serial_width)
    c._save_serial_prefs = lambda: c.saved.append((c.serial_open, c.serial_width, c.serial_times))
    c._serial_geometry = lambda: (40, 10, 14)
    return client, c


def event(kind, **fields):
    import pygame
    return pygame.event.Event(getattr(pygame, kind), **fields)


def test_presses_over_the_panel_never_reach_the_guest():
    _, c = pygame_client()
    c._mouse_down(event("MOUSEBUTTONDOWN", button=1, pos=(100, 200)))
    c._mouse_up(event("MOUSEBUTTONUP", button=1, pos=(400, 200)))
    c._mouse_down(event("MOUSEBUTTONDOWN", button=3, pos=(100, 200)))
    c._mouse_up(event("MOUSEBUTTONUP", button=3, pos=(100, 200)))
    assert c.sent == []
    c._mouse_down(event("MOUSEBUTTONDOWN", button=1, pos=(400, 200)))      # the screen
    c._mouse_down(event("MOUSEBUTTONDOWN", button=4, pos=(100, 200)))      # a legacy wheel, on the panel
    c._mouse_up(event("MOUSEBUTTONUP", button=1, pos=(100, 200)))
    assert c.sent == [("position", 400, 200), ("button", 0, True), ("button", 0, False)], \
        "a press on the screen lost its release to a press on the panel"


def test_the_guests_pointer_starts_at_the_screen_not_the_window():
    client, c = pygame_client()
    assert client.DisplayClient._window_to_virtual_coords(c, 320 + 40, BAR + 20) == (10, 5)
    c.serial_open, c._screen_x = False, 0
    assert client.DisplayClient._window_to_virtual_coords(c, 40, BAR + 20) == (10, 5)


def test_dragging_the_edge_resizes_the_window_once_when_it_ends():
    _, c = pygame_client()
    c._mouse_down(event("MOUSEBUTTONDOWN", button=1, pos=(318, 200)))
    for x in (360, 420, 500):
        c._mouse_motion(event("MOUSEMOTION", pos=(x, 200), rel=(0, 0), buttons=(1, 0, 0)))
    assert c._serial_drag == 500 and c.resized == [], "the window was made again during the drag"
    c._mouse_up(event("MOUSEBUTTONUP", button=1, pos=(500, 200)))
    assert (c.serial_width, c.resized, c.saved, c.sent) == (500, [500], [(True, 500, True)], [])
    c._mouse_down(event("MOUSEBUTTONDOWN", button=1, pos=(497, 200)))
    c._mouse_up(event("MOUSEBUTTONUP", button=1, pos=(9000, 200)))
    assert c.serial_width == SCREEN_W * 7 // 3


def test_the_wheel_over_the_panel_scrolls_it_and_elsewhere_reaches_the_guest():
    _, c = pygame_client()
    c.serial.append({"start": 0, "next": 500, "text": "".join(f"line {i}\n" for i in range(50)),
                     "stamps": [], "lost": 0})
    c._wheel(event("MOUSEWHEEL", x=0, y=1), (100, 200))
    assert c._serial_top == 37 and c.sent == []
    c._wheel(event("MOUSEWHEEL", x=0, y=-5), (100, 200))
    assert c._serial_top is None
    c._wheel(event("MOUSEWHEEL", x=0, y=1), (400, 200))
    assert c.sent == [("button", 5, True), ("button", 5, False)]


def test_the_headers_clear_and_times():
    _, c = pygame_client()
    c.serial.append({"start": 0, "next": 3, "text": "hi\n", "stamps": [(0, 0.0)], "lost": 0})
    buttons = SP.header_buttons(320, BAR)
    x, y, w, h = buttons["clear"]
    c._mouse_down(event("MOUSEBUTTONDOWN", button=1, pos=(x + 2, y + 2)))
    c._mouse_up(event("MOUSEBUTTONUP", button=1, pos=(x + 2, y + 2)))
    assert c.serial.lines == [] and c.serial.next == 3 and c.sent == []
    x, y, w, h = buttons["times"]
    c._mouse_down(event("MOUSEBUTTONDOWN", button=1, pos=(x + 2, y + 2)))
    assert c.serial_times is False and c.saved == [(True, 320, False)]


def test_the_poller_adds_what_serial_answers_and_ignores_the_rest():
    client, c = pygame_client()
    c.base_url = "http://display"
    asked = []

    class Reply:
        def __init__(self, body, ok=True):
            self.body, self.ok = body, ok

        def json(self):
            return self.body

    answers = [Reply({"start": 0, "next": 6, "text": "hello\n", "stamps": [[0, 2.5]], "lost": 0}),
               Reply({"nonsense": True}), Reply(None, ok=False)]

    class Session:
        def get(self, url, params, timeout):
            asked.append((url, params["from"]))
            return answers.pop(0)

    c.serial_session = Session()
    for _ in range(3):
        c._serial_poll_once()
    assert asked == [("http://display/serial", 0), ("http://display/serial", 6), ("http://display/serial", 6)]
    assert c.serial.lines == [[2.5, "hello"]]
    assert client.PREFS_PATH == REPO_ROOT / "build" / "display.json"



def test_the_window_draws_the_panel_left_of_the_screen_and_the_button_toggles_it():
    """Drawn for real, with SDL's dummy video driver instead of a window."""
    import tempfile
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    import threading
    import pygame
    client, c = pygame_client()
    for stub in ("_resize_window", "_save_serial_prefs", "_serial_geometry"):
        delattr(c, stub)
    pygame.display.init()
    pygame.font.init()
    c.font, c.mono = pygame.font.SysFont(None, 22), pygame.font.SysFont("monospace", 13)
    c.cd_url, c.status_ttl, c.status_message, c._picker = None, 0, "", None
    c._cd_lock, c._cd_status = threading.Lock(), None
    c._display_connected, c._display_connected_lock = True, threading.Lock()
    c._hid_connected, c._hid_connected_lock = True, threading.Lock()
    red = bytes((255, 0, 0, 255)) * (192 * 108)
    saved_path = client.PREFS_PATH
    with tempfile.TemporaryDirectory() as d:
        client.PREFS_PATH = Path(d) / "display.json"
        try:
            c._build_buttons()
            c._resize_window()
            assert c.screen.get_size() == SP.layout(True, 320, SCREEN_W, SCREEN_H, BAR, c._bar_min_width)["window"]
            assert c._screen_x == 320
            c.serial.append({"start": 0, "next": 6, "text": "hello\n", "stamps": [(0, 1.0)], "lost": 0})
            c._render(red, (0, 0))
            for x in (320 + 10, 320 + SCREEN_W - 5):        # its first columns and its last
                assert tuple(c.screen.get_at((x, BAR + 10)))[:3] == (255, 0, 0), \
                    f"the screen isn't right of the panel: nothing at x {x}"
            assert tuple(c.screen.get_at((10, BAR + 200)))[:3] == client.PANEL_BG, "no panel on the left"
            c._toggle_serial()
            assert (c.serial_open, c._screen_x) == (False, 0)
            assert c.screen.get_size() == SP.layout(False, 320, SCREEN_W, SCREEN_H, BAR, c._bar_min_width)["window"]
            c._render(red, (0, 0))
            assert tuple(c.screen.get_at((10, BAR + 200)))[:3] == (255, 0, 0)
            assert json.loads(client.PREFS_PATH.read_text())["serial_open"] is False
        finally:
            client.PREFS_PATH = saved_path
            pygame.display.quit()


def test_the_event_loop_hands_the_mouse_to_the_handlers():
    source = (REPO_ROOT / "display" / "display.py").read_text()
    loop = source[source.index("    def run(self):"):source.index("    def _render(")]
    for call in ("self._mouse_down(event)", "self._mouse_up(event)", "self._mouse_motion(event)",
                 "self._wheel(event, pygame.mouse.get_pos())"):
        assert call in loop, f"run() no longer calls {call}"
    assert "if self._serial_drag is None and now - self._last_mouse_pos_time" in loop, \
        "the pointer's position goes to the guest while the panel's edge is dragged"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the Serial panel"))
