#!/usr/bin/env python3
"""Pygame front-end for the pigeon emulator's DisplayIO FastAPI server.

Polls GET /frame for the current RGBA framebuffer and renders it scaled
by an adjustable pixel size. Includes buttons to clear the display, to
increase/decrease the pixel size, and to work the CD drive
(docs/cd-drive.md): load a disc from the emulator's own folders, load one
from anywhere with a native file dialog, and eject.

Requires pygame and requests, which the emulator itself does NOT need:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements-client.txt

Usage:
    .venv/bin/python display/display.py [--host 127.0.0.1] [--port 8000] [--fps 30]

For display only, with no extra packages at all, open the browser front-end
at http://127.0.0.1:8000 instead -- it is served by the emulator itself.
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

try:
    import pygame
    import requests
except ImportError as exc:
    sys.exit(
        f"{exc.name} is not installed, and this client needs it.\n"
        "\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements-client.txt\n"
        "  .venv/bin/python display/display.py\n"
        "\n"
        "Installing into the system Python usually fails on modern distros\n"
        "(PEP 668, 'externally-managed-environment') -- use a venv as above.\n"
        "\n"
        "For display without pygame, open http://127.0.0.1:8000 in a browser."
    )

# The pigeon keycode space is defined once, in the emulator; this client
# owns only the pygame -> pigeon mapping. Raw pygame keycodes must never be
# sent: they are above 2**30 for non-printable keys, and the guest's key
# FIFO is one byte wide.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from emulator.devices import keycodes as K          # noqa: E402

PYGAME_TO_PIGEON = {
    pygame.K_LEFT: K.KEY_LEFT,       pygame.K_RIGHT: K.KEY_RIGHT,
    pygame.K_UP: K.KEY_UP,           pygame.K_DOWN: K.KEY_DOWN,
    pygame.K_HOME: K.KEY_HOME,       pygame.K_END: K.KEY_END,
    pygame.K_PAGEUP: K.KEY_PGUP,     pygame.K_PAGEDOWN: K.KEY_PGDN,
    pygame.K_INSERT: K.KEY_INSERT,   pygame.K_DELETE: K.KEY_DELETE,
    pygame.K_BACKSPACE: K.KEY_BACKSPACE, pygame.K_TAB: K.KEY_TAB,
    pygame.K_RETURN: K.KEY_ENTER,    pygame.K_KP_ENTER: K.KEY_ENTER,
    pygame.K_ESCAPE: K.KEY_ESC,      pygame.K_SPACE: K.KEY_SPACE,
    pygame.K_LSHIFT: K.KEY_LSHIFT,   pygame.K_RSHIFT: K.KEY_RSHIFT,
    pygame.K_LCTRL: K.KEY_LCTRL,     pygame.K_RCTRL: K.KEY_RCTRL,
    pygame.K_LALT: K.KEY_LALT,       pygame.K_RALT: K.KEY_RALT,
}
PYGAME_TO_PIGEON.update({getattr(pygame, f"K_F{n}"): K.key_f(n) for n in range(1, 13)})


def to_pigeon_key(key_code, text=""):
    """pygame keycode -> pigeon keycode, or None if we do not carry it.

    `text` is the event's `unicode` field, and it is the only part of a
    pygame key event that knows about the keyboard LAYOUT or about shift.
    `event.key` is the PHYSICAL key: it reports K_9 whether or not shift
    is down, and K_9 is 57, which is also ord('9') -- so a client that
    trusts it passes the guest a '9' when the user typed '('. Reading
    only event.key is why no shifted character could be typed at all:
    not '(' or ')', not '*', '^' or '+', and no capital letter.

    Named keys are matched FIRST, before the text. Their unicode is a
    control character -- '\r' for Return, '\x1b' for Escape, '\x08' for
    Backspace -- and one of them, Space, is printable and would otherwise
    take a second path to the same answer.

    `text` is empty on KEYUP, which is what the final branch is for.
    """
    if key_code in PYGAME_TO_PIGEON:
        return PYGAME_TO_PIGEON[key_code]
    if len(text) == 1 and K.is_printable(ord(text)):
        return ord(text)
    if K.is_printable(key_code):        # printable ASCII passes straight through
        return key_code
    return None


BUTTON_BAR_HEIGHT = 40
MIN_PIXEL_SIZE = 1
MAX_PIXEL_SIZE = 16
RETRY_INTERVAL = 1.0  # seconds between reconnect attempts
REQUEST_TIMEOUT = (0.5, 1.0)  # (connect timeout, read timeout) - fail fast on a dead server
MOUSE_POS_RATE_LIMIT = 30  # max updates per second to HID server

BG_COLOR = (20, 20, 20)
BAR_COLOR = (40, 40, 40)
BUTTON_COLOR = (70, 70, 70)
BUTTON_HOVER_COLOR = (100, 100, 100)
BUTTON_TEXT_COLOR = (230, 230, 230)
DISCONNECTED_COLOR = (200, 80, 80)
DISABLED_BUTTON_COLOR = (50, 50, 50)
DISABLED_TEXT_COLOR = (120, 120, 120)
DIM_TEXT_COLOR = (160, 160, 170)

# The CD drive's picker, drawn over the framebuffer rather than delegated
# to a toolkit, so "Load from server" needs nothing beyond pygame.
PICKER_BG = (28, 28, 34)
PICKER_BORDER = (90, 90, 100)
PICKER_SELECTED = (55, 85, 125)
PICKER_ROW_H = 24
# Other front ends can change what is in the drive, so this client asks.
CD_POLL_INTERVAL = 2.0
CD_TIMEOUT = (0.5, 5.0)


def _fmt_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"

# pygame button index -> HID button index (4 and 5 are the legacy scroll
# wheel, which the HID event byte has no encoding for, so they are dropped)
MOUSE_BUTTON_MAP = {1: 0, 3: 1, 2: 2}


class Button:
    def __init__(self, rect, label, callback):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.callback = callback
        self.enabled = True

    def draw(self, surface, font, mouse_pos):
        if not self.enabled:
            color, ink = DISABLED_BUTTON_COLOR, DISABLED_TEXT_COLOR
        else:
            hovered = self.rect.collidepoint(mouse_pos)
            color = BUTTON_HOVER_COLOR if hovered else BUTTON_COLOR
            ink = BUTTON_TEXT_COLOR
        pygame.draw.rect(surface, color, self.rect, border_radius=4)
        text = font.render(self.label, True, ink)
        text_rect = text.get_rect(center=self.rect.center)
        surface.blit(text, text_rect)

    def handle_click(self, pos):
        # A disabled button still OWNS the click -- it must not fall
        # through to whatever is behind it -- it just does nothing.
        if self.rect.collidepoint(pos):
            if self.enabled:
                self.callback()
            return True
        return False


class DisplayClient:
    def __init__(self, host: str, port: int, fps: int, pixel_size: int = 4, hid_host: str = "127.0.0.1", hid_port: int = 8001):
        self.base_url = f"http://{host}:{port}"
        self.hid_url = f"http://{hid_host}:{hid_port}"
        self.fps = fps
        self.pixel_size = pixel_size

        # Persistent sessions for both display and HID servers
        self.session = requests.Session()
        self.hid_session = requests.Session()

        info = self._wait_for_server()
        self.disp_w = info["w"]
        self.disp_h = info["h"]
        self.frame_size = info["size"]

        # The CD server's address comes from /info, the same way the
        # browser page learns it, so a --cd-port on the emulator needs no
        # matching flag here. An emulator from before the drive sends none,
        # and the CD buttons are then disabled rather than failing on click.
        self.cd_url = info.get("cd_url")
        # Two sessions, because requests.Session is not safe to share
        # across threads: one for the poll thread, one for button actions.
        self.cd_poll_session = requests.Session()
        self.cd_action_session = requests.Session()
        self._cd_status = None
        self._cd_lock = threading.Lock()
        self._picker = None             # the "Load from server" overlay, when open
        self._eject_button = None

        # Wait for HID server too before starting pygame (or fail gracefully if it's not running yet)
        self._hid_available = True
        try:
            self._wait_for_hid_server(timeout_sec=2)
        except Exception:
            print("HID server not available yet; will retry when it comes online")
            self._hid_available = False

        pygame.init()
        pygame.display.set_caption("Pigeon Display")
        self.font = pygame.font.SysFont(None, 22)
        self.clock = pygame.time.Clock()

        self.buttons = []
        self._build_buttons()
        self._resize_window()

        self.status_message = ""
        self.status_ttl = 0

        # Physical pygame key -> the pigeon code sent when it went down,
        # so the release can match the press. See the KEYUP handler.
        self._key_sent = {}

        # Background frame fetcher
        self._latest_frame = b"\x00" * self.frame_size
        self._frame_lock = threading.Lock()
        self._display_connected = True
        self._display_connected_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._fetch_thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self._fetch_thread.start()

        # HID input state
        self._hid_connected = self._hid_available
        self._hid_connected_lock = threading.Lock()
        self._last_mouse_pos_time = 0
        self._mouse_pos_interval = 1.0 / MOUSE_POS_RATE_LIMIT
        # True while a press that started on the toolbar is still held, so
        # its release is not forwarded to the guest either.
        self._toolbar_drag = False

        # Background HID keep-alive thread: periodically checks/reconnects to HID server
        # independently of the render loop, same as display fetch thread
        self._hid_thread = threading.Thread(target=self._hid_keep_alive_loop, daemon=True)
        self._hid_thread.start()

        self._cd_thread = threading.Thread(target=self._cd_poll_loop, daemon=True)
        self._cd_thread.start()

    # --- networking (display server) -------------------------------------------

    def _wait_for_server(self):
        """Block until the display server responds to /info, retrying once a second."""
        printed_waiting = False
        while True:
            try:
                resp = self.session.get(f"{self.base_url}/info", timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                if printed_waiting:
                    print("Connected to display server.")
                return resp.json()
            except Exception:
                if not printed_waiting:
                    print(f"Waiting for display server at {self.base_url} ...")
                    printed_waiting = True
                time.sleep(RETRY_INTERVAL)

    def _wait_for_hid_server(self, timeout_sec=2):
        """Try to connect to HID server with a timeout (used at startup)."""
        start = time.time()
        while time.time() - start < timeout_sec:
            try:
                resp = self.hid_session.get(f"{self.hid_url}/state", timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                print(f"Connected to HID server at {self.hid_url}")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(f"HID server at {self.hid_url} did not respond within {timeout_sec}s")

    def _fetch_loop(self):
        """Continuously fetch display frames as fast as the server can serve them,
        independent of the render/display fps. Retries once a second while
        the server is unreachable instead of hammering it."""
        while not self._stop_event.is_set():
            try:
                resp = self.session.get(f"{self.base_url}/frame", timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.content
                with self._frame_lock:
                    self._latest_frame = data
                self._set_display_connected(True)
            except Exception:
                self._set_display_connected(False)
                time.sleep(RETRY_INTERVAL)

    def _set_display_connected(self, value: bool):
        with self._display_connected_lock:
            self._display_connected = value

    def _is_display_connected(self) -> bool:
        with self._display_connected_lock:
            return self._display_connected

    def _set_hid_connected(self, value: bool):
        with self._hid_connected_lock:
            self._hid_connected = value

    def _is_hid_connected(self) -> bool:
        with self._hid_connected_lock:
            return self._hid_connected

    def _hid_keep_alive_loop(self):
        """Background thread that periodically checks HID server connection.
        Retries every RETRY_INTERVAL (1 second) if disconnected, same cadence
        as the display fetch loop. This ensures HID reconnects quickly when
        the server comes online, without blocking the render thread."""
        while not self._stop_event.is_set():
            if not self._is_hid_connected():
                try:
                    resp = self.hid_session.get(f"{self.hid_url}/state", timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    self._set_hid_connected(True)
                except Exception:
                    pass
            time.sleep(RETRY_INTERVAL)

    def _get_latest_frame(self) -> bytes:
        with self._frame_lock:
            return self._latest_frame

    def _send_clear(self):
        try:
            self.session.post(f"{self.base_url}/clear", timeout=REQUEST_TIMEOUT)
            self._set_status("Cleared")
        except Exception as e:
            self._set_status(f"Clear failed: {e}")

    # --- HID input (mouse, keyboard) -----

    def _window_to_virtual_coords(self, win_x: int, win_y: int) -> tuple:
        """Convert window coordinates to virtual pixel coordinates.
        
        Accounts for:
        - The button bar at the top (BUTTON_BAR_HEIGHT)
        - Pixel size magnification (divides by pixel_size)
        
        Returns (virt_x, virt_y) clamped to the virtual framebuffer bounds.
        """
        # Subtract the button bar offset
        virt_y = win_y - BUTTON_BAR_HEIGHT
        virt_x = win_x

        # Scale down by pixel size (map window pixels back to virtual pixels)
        virt_x = virt_x // self.pixel_size
        virt_y = virt_y // self.pixel_size

        # Clamp to framebuffer bounds
        virt_x = max(0, min(virt_x, self.disp_w - 1))
        virt_y = max(0, min(virt_y, self.disp_h - 1))

        return virt_x, virt_y

    def _send_mouse_pos(self, win_x: int, win_y: int):
        """Send current mouse position (in virtual coords) to HID server."""
        if not self._is_hid_connected():
            return
        virt_x, virt_y = self._window_to_virtual_coords(win_x, win_y)
        try:
            self.hid_session.post(
                f"{self.hid_url}/mouse_pos",
                json={"x": virt_x, "y": virt_y},
                timeout=REQUEST_TIMEOUT,
            )
            self._set_hid_connected(True)
        except Exception:
            self._set_hid_connected(False)

    def _send_mouse_button(self, button: int, pressed: bool):
        """Send a mouse button press/release event to HID server.
        
        button: 0=left, 1=right, 2=middle, 3=back, 4=forward
        """
        if not self._is_hid_connected():
            return
        try:
            self.hid_session.post(
                f"{self.hid_url}/mouse_event",
                json={"button": button, "pressed": pressed},
                timeout=REQUEST_TIMEOUT,
            )
            self._set_hid_connected(True)
        except Exception:
            self._set_hid_connected(False)

    def _send_key(self, key_code: int, pressed: bool = True):
        """Forward one key transition. `key_code` must already be a pigeon
        keycode -- see to_pigeon_key(); raw pygame codes are rejected by
        the HID device."""
        if not self._is_hid_connected():
            return
        try:
            self.hid_session.post(
                f"{self.hid_url}/key",
                json={"code": key_code, "pressed": pressed},
                timeout=REQUEST_TIMEOUT,
            )
            self._set_hid_connected(True)
        except Exception:
            self._set_hid_connected(False)

    # --- UI ---------------------------------------------------------------

    def _set_status(self, msg, frames=60):
        self.status_message = msg
        self.status_ttl = frames

    def _change_pixel_size(self, delta):
        new_size = max(MIN_PIXEL_SIZE, min(MAX_PIXEL_SIZE, self.pixel_size + delta))
        if new_size != self.pixel_size:
            self.pixel_size = new_size
            self._resize_window()
        self._set_status(f"Pixel size: {self.pixel_size}")

    def _build_buttons(self):
        """Lay the bar out from the font's own metrics.

        The first three buttons used to sit at typed x positions, with the
        "px:" label and the status text at 185 and 280. That held while
        the bar had three short buttons; "Load from server" alone is wider
        than all three together, so positions are measured now.
        """
        x = 10
        buttons = []

        def add(label, callback, min_w=40):
            nonlocal x
            w = max(min_w, self.font.size(label)[0] + 18)
            button = Button((x, 6, w, 28), label, callback)
            buttons.append(button)
            x += w + 6
            return button

        add("Clear", self._send_clear, min_w=70)
        add("-", lambda: self._change_pixel_size(-1))
        add("+", lambda: self._change_pixel_size(1))
        self._px_x = x + 4
        x = self._px_x + self.font.size("px: 16")[0] + 16

        load_server = add("Load from server", self._load_from_server)
        load_pc = add("Load from PC", self._load_from_pc)
        self._eject_button = add("Eject", self._eject)
        if not self.cd_url:
            for button in (load_server, load_pc, self._eject_button):
                button.enabled = False
        self._eject_button.enabled = False      # nothing is in the drive yet

        self._info_x = x + 6
        self.buttons = buttons
        # Wide enough for every button and a disc label of ordinary length.
        # At pixel size 1 the framebuffer is 192 px wide and the old 260 px
        # minimum already cut the bar off after "+".
        self._bar_min_width = (self._info_x
                               + self.font.size("disc: a-typical-name.bin (99.9 KB)")[0]
                               + 12)

    def _resize_window(self):
        width = max(self.disp_w * self.pixel_size, self._bar_min_width)
        height = self.disp_h * self.pixel_size + BUTTON_BAR_HEIGHT
        self.screen = pygame.display.set_mode((width, height))

    # --- the CD drive ---------------------------------------------------------

    def _cd_call(self, method, path, session=None, **kwargs):
        """One request to the CD server, unwrapping FastAPI's {detail}.

        The detail is the useful part: a 403 says which config key keeps
        the file out, and a bare status code would hide exactly that.
        """
        if not self.cd_url:
            raise RuntimeError("no CD drive on this emulator")
        kwargs.setdefault("timeout", CD_TIMEOUT)
        session = session or self.cd_action_session
        resp = session.request(method, f"{self.cd_url}{path}", **kwargs)
        if not resp.ok:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except ValueError:
                pass
            raise RuntimeError(detail or f"{resp.status_code} {resp.reason}")
        return resp.json()

    def _cd_set(self, status):
        with self._cd_lock:
            self._cd_status = status

    def _cd_snapshot(self):
        with self._cd_lock:
            return self._cd_status

    def _cd_poll_loop(self):
        """The browser page can work the same drive, so this client is not
        the only thing that changes what is in it. Without the poll the two
        front ends disagree about what is loaded until a button is touched."""
        while not self._stop_event.is_set():
            if self.cd_url:
                try:
                    self._cd_set(self._cd_call("GET", "/cd/status",
                                               session=self.cd_poll_session))
                except Exception:
                    self._cd_set(None)
            self._stop_event.wait(CD_POLL_INTERVAL)

    def _disc_label(self):
        status = self._cd_snapshot()
        if status is None:
            return "CD: ..."
        if not status.get("present"):
            return "no disc"
        return f"disc: {status['name']} ({_fmt_size(status['size'])})"

    def _insert_path(self, path):
        try:
            status = self._cd_call("POST", "/cd/insert", json={"path": str(path)})
            self._cd_set(status)
            self._set_status(f"CD: {status['name']}")
        except Exception as e:
            self._set_status(f"CD: {e}", frames=180)

    def _load_from_server(self):
        try:
            discs = self._cd_call("GET", "/cd/list")
        except Exception as e:
            self._set_status(f"CD: {e}", frames=180)
            return
        if not discs:
            self._set_status("CD: nothing in cd_dirs -- drop a file in cds/", frames=180)
            return
        self._open_picker(discs)

    def _load_from_pc(self):
        """A real folder browser, via tkinter.

        tkinter is NOT a pip package -- on most Linux distributions it is
        the system package python3-tk -- so it cannot go in
        requirements-client.txt, and it is imported here, lazily, rather
        than at the top of the file. Missing, it costs this one button and
        says why; "Load from server" never touches it and keeps working.
        """
        try:
            import tkinter
            from tkinter import filedialog
        except ImportError:
            self._set_status("Load from PC needs python3-tk (apt install python3-tk)",
                             frames=240)
            return

        status = self._cd_snapshot() or {}
        # Open where the drive actually accepts discs from, rather than
        # offering paths that will come back 403.
        start = status.get("root") or str(Path.home())
        try:
            root = tkinter.Tk()
            root.withdraw()
            try:
                chosen = filedialog.askopenfilename(title="Load a disc", initialdir=start)
            finally:
                root.destroy()
        except Exception as e:               # e.g. TclError: no display
            self._set_status(f"File dialog failed: {e}", frames=180)
            return
        if chosen:
            self._insert_path(chosen)

    def _eject(self):
        try:
            self._cd_set(self._cd_call("POST", "/cd/eject"))
            self._set_status("CD: ejected")
        except Exception as e:
            self._set_status(f"CD: {e}", frames=180)

    # --- the "Load from server" picker ------------------------------------------

    def _open_picker(self, discs):
        # Anything held down in the guest is released first. The picker
        # swallows every key while it is open, so a key pressed before it
        # opened would never see its release and would stay stuck down in
        # the guest's bitmap -- the same hazard index.html handles on blur.
        for code in list(self._key_sent.values()):
            self._send_key(code, False)
        self._key_sent.clear()
        self._picker = {"items": discs, "index": 0, "top": 0, "rows": 1, "rects": [],
                        "panel": None}

    def _close_picker(self):
        self._picker = None

    def _picker_choose(self, index):
        items = self._picker["items"]
        if 0 <= index < len(items):
            path = items[index]["path"]
            self._close_picker()
            self._insert_path(path)

    def _picker_event(self, event):
        """Handle an event while the picker is open. True if it was consumed.

        Everything keyboard and mouse is consumed: nothing reaches the
        guest behind the overlay.
        """
        p = self._picker
        count = len(p["items"])
        if event.type == pygame.KEYDOWN:
            k = event.key
            if k == pygame.K_ESCAPE:
                self._close_picker()
            elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self._picker_choose(p["index"])
            elif k == pygame.K_UP:
                p["index"] = max(0, p["index"] - 1)
            elif k == pygame.K_DOWN:
                p["index"] = min(count - 1, p["index"] + 1)
            elif k == pygame.K_PAGEUP:
                p["index"] = max(0, p["index"] - p["rows"])
            elif k == pygame.K_PAGEDOWN:
                p["index"] = min(count - 1, p["index"] + p["rows"])
            elif k == pygame.K_HOME:
                p["index"] = 0
            elif k == pygame.K_END:
                p["index"] = count - 1
            return True
        if event.type == pygame.KEYUP:
            return True
        if event.type == pygame.MOUSEWHEEL:
            p["index"] = max(0, min(count - 1, p["index"] - event.y))
            return True
        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button in (4, 5):        # legacy wheel events
                return True
            panel = p["panel"]
            if panel is None or not panel.collidepoint(event.pos):
                self._close_picker()          # a click outside dismisses it
                return True
            for rect, index in p["rects"]:
                if rect.collidepoint(event.pos):
                    self._picker_choose(index)
                    break
            return True
        if event.type == pygame.MOUSEBUTTONUP:
            return True
        return False

    def _draw_picker(self, mouse_pos):
        p = self._picker
        width = self.screen.get_width()
        height = self.screen.get_height() - BUTTON_BAR_HEIGHT
        pad = 10
        panel = pygame.Rect(pad, BUTTON_BAR_HEIGHT + pad, width - 2 * pad, height - 2 * pad)
        p["panel"] = panel
        pygame.draw.rect(self.screen, PICKER_BG, panel)
        pygame.draw.rect(self.screen, PICKER_BORDER, panel, 1)

        title = self.font.render("Load from server   arrows / click, Enter, Esc",
                                 True, DIM_TEXT_COLOR)
        self.screen.blit(title, (panel.x + 10, panel.y + 8))

        list_top = panel.y + 32
        rows = max(1, (panel.bottom - 8 - list_top) // PICKER_ROW_H)
        p["rows"] = rows
        # Keep the selection on screen.
        if p["index"] < p["top"]:
            p["top"] = p["index"]
        if p["index"] >= p["top"] + rows:
            p["top"] = p["index"] - rows + 1

        p["rects"] = []
        for row in range(rows):
            index = p["top"] + row
            if index >= len(p["items"]):
                break
            item = p["items"][index]
            rect = pygame.Rect(panel.x + 6, list_top + row * PICKER_ROW_H,
                               panel.width - 12, PICKER_ROW_H - 2)
            p["rects"].append((rect, index))
            if index == p["index"]:
                pygame.draw.rect(self.screen, PICKER_SELECTED, rect, border_radius=3)
            elif rect.collidepoint(mouse_pos):
                pygame.draw.rect(self.screen, BUTTON_COLOR, rect, border_radius=3)
            name = self.font.render(item["name"], True, BUTTON_TEXT_COLOR)
            size = self.font.render(_fmt_size(item["size"]), True, DIM_TEXT_COLOR)
            self.screen.blit(name, (rect.x + 8, rect.y + 3))
            self.screen.blit(size, (rect.right - size.get_width() - 8, rect.y + 3))

        if len(p["items"]) > rows:
            more = self.font.render(f"{p['index'] + 1} / {len(p['items'])}", True,
                                    DIM_TEXT_COLOR)
            self.screen.blit(more, (panel.right - more.get_width() - 10, panel.y + 8))

    # --- main loop ----------------------------------------------------

    def run(self):
        running = True
        while running:
            mouse_pos = pygame.mouse.get_pos()

            # Rate-limited mouse position updates (max 30/sec)
            now = time.time()
            if now - self._last_mouse_pos_time >= self._mouse_pos_interval:
                self._send_mouse_pos(mouse_pos[0], mouse_pos[1])
                self._last_mouse_pos_time = now

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    continue
                # While the disc picker is open it owns the keyboard and the
                # mouse, so nothing reaches the guest behind the overlay.
                if self._picker is not None and self._picker_event(event):
                    continue
                if event.type == pygame.MOUSEBUTTONDOWN:
                    # A click on the toolbar belongs to the toolbar. It used
                    # to be forwarded to the guest as well, as a press at a
                    # coordinate clamped to y=0 -- so pressing "Clear" also
                    # injected a phantom click into the running program.
                    if event.pos[1] < BUTTON_BAR_HEIGHT:
                        if event.button == 1:          # left click only
                            for button in self.buttons:
                                if button.handle_click(event.pos):
                                    break
                        self._toolbar_drag = True
                    else:
                        hid_button = MOUSE_BUTTON_MAP.get(event.button)
                        if hid_button is not None:
                            self._send_mouse_button(hid_button, True)
                elif event.type == pygame.MOUSEBUTTONUP:
                    # Release the press we actually forwarded, and only that.
                    if self._toolbar_drag:
                        self._toolbar_drag = False
                    else:
                        hid_button = MOUSE_BUTTON_MAP.get(event.button)
                        if hid_button is not None:
                            self._send_mouse_button(hid_button, False)
                elif event.type == pygame.KEYDOWN:
                    code = to_pigeon_key(event.key, event.unicode)
                    if code is not None:
                        self._key_sent[event.key] = code
                        self._send_key(code, True)
                elif event.type == pygame.KEYUP:
                    # Release whatever was PRESSED, not what this physical
                    # key would translate to now. Shift is usually let go
                    # first, so translating again here would release '9'
                    # for a '(' that was pressed -- leaving '(' held down
                    # in the guest's key bitmap for good.
                    code = self._key_sent.pop(event.key, None)
                    if code is None:
                        code = to_pigeon_key(event.key)
                    if code is not None:
                        self._send_key(code, False)

            frame = self._get_latest_frame()
            self._render(frame, mouse_pos)
            self.clock.tick(self.fps)

        self._stop_event.set()
        pygame.quit()

    def _render(self, frame_bytes, mouse_pos):
        self.screen.fill(BG_COLOR)

        # Draw the framebuffer
        try:
            surface = pygame.image.frombuffer(frame_bytes, (self.disp_w, self.disp_h), "RGBA")
            if self.pixel_size != 1:
                surface = pygame.transform.scale(
                    surface, (self.disp_w * self.pixel_size, self.disp_h * self.pixel_size)
                )
            self.screen.blit(surface, (0, BUTTON_BAR_HEIGHT))
        except ValueError:
            # Frame size didn't match expected dimensions; skip this frame
            pass

        # Draw button bar
        bar_rect = pygame.Rect(0, 0, self.screen.get_width(), BUTTON_BAR_HEIGHT)
        pygame.draw.rect(self.screen, BAR_COLOR, bar_rect)
        for button in self.buttons:
            button.draw(self.screen, self.font, mouse_pos)

        size_label = self.font.render(f"px: {self.pixel_size}", True, BUTTON_TEXT_COLOR)
        self.screen.blit(size_label, (self._px_x, 12))

        disc = self._cd_snapshot()
        self._eject_button.enabled = bool(self.cd_url and disc and disc.get("present"))

        # A status message borrows the disc label's slot while it lasts:
        # they say the same kind of thing, and the bar has one place for it.
        if self.status_ttl > 0:
            status_label = self.font.render(self.status_message, True, BUTTON_TEXT_COLOR)
            self.screen.blit(status_label, (self._info_x, 12))
            self.status_ttl -= 1
        elif self.cd_url:
            disc_label = self.font.render(self._disc_label(), True, DIM_TEXT_COLOR)
            self.screen.blit(disc_label, (self._info_x, 12))

        # Show connection status for both servers
        status_x = self.screen.get_width() - 200
        if not self._is_display_connected():
            display_label = self.font.render("Display: Reconnecting...", True, DISCONNECTED_COLOR)
            self.screen.blit(display_label, (status_x, 12))
            status_x -= 150

        if not self._is_hid_connected():
            hid_label = self.font.render("HID: Reconnecting...", True, DISCONNECTED_COLOR)
            self.screen.blit(hid_label, (status_x, 12))

        if self._picker is not None:
            self._draw_picker(mouse_pos)

        pygame.display.flip()


FALLBACK = {"host": "127.0.0.1", "display_port": 8000, "hid_port": 8001}


def server_defaults():
    """Read host/ports from the emulator's config.json.

    Read as plain JSON rather than importing emulator.config, so this
    client stays a standalone process with no emulator imports. Any
    problem with the file just means the built-in defaults -- the client
    should still start and let you point it somewhere with flags.
    """
    settings = dict(FALLBACK)
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        for key in settings:
            if key in loaded:
                settings[key] = loaded[key]
    except (OSError, ValueError, TypeError):
        pass
    return settings


def main():
    defaults = server_defaults()
    parser = argparse.ArgumentParser(
        description="Pigeon emulator display + input client",
        epilog="Host and ports default to config.json at the repo root.")
    parser.add_argument("--host", default=defaults["host"], help="Display server host")
    parser.add_argument("--port", type=int, default=defaults["display_port"],
                        help="Display server port")
    parser.add_argument("--hid-host", default=None,
                        help="HID server host (defaults to --host)")
    parser.add_argument("--hid-port", type=int, default=defaults["hid_port"],
                        help="HID server port")
    parser.add_argument("--fps", type=int, default=30, help="Display update FPS")
    parser.add_argument("--pixel-size", type=int, default=8,
                        help="Initial pixel size magnification")
    args = parser.parse_args()

    pixel_size = max(MIN_PIXEL_SIZE, min(MAX_PIXEL_SIZE, args.pixel_size))
    hid_host = args.hid_host if args.hid_host is not None else args.host

    client = DisplayClient(args.host, args.port, args.fps, pixel_size,
                           hid_host, args.hid_port)
    client.run()


if __name__ == "__main__":
    main()