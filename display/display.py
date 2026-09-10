#!/usr/bin/env python3
"""Pygame front-end for the pigeon emulator's DisplayIO FastAPI server.

Polls GET /frame for the current RGBA framebuffer and renders it scaled
by an adjustable pixel size. Includes buttons to clear the display and
to increase/decrease the pixel size.

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

# pygame button index -> HID button index (4 and 5 are the legacy scroll
# wheel, which the HID event byte has no encoding for, so they are dropped)
MOUSE_BUTTON_MAP = {1: 0, 3: 1, 2: 2}


class Button:
    def __init__(self, rect, label, callback):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.callback = callback

    def draw(self, surface, font, mouse_pos):
        hovered = self.rect.collidepoint(mouse_pos)
        color = BUTTON_HOVER_COLOR if hovered else BUTTON_COLOR
        pygame.draw.rect(surface, color, self.rect, border_radius=4)
        text = font.render(self.label, True, BUTTON_TEXT_COLOR)
        text_rect = text.get_rect(center=self.rect.center)
        surface.blit(text, text_rect)

    def handle_click(self, pos):
        if self.rect.collidepoint(pos):
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
        self.buttons = [
            Button((10, 6, 70, 28), "Clear", self._send_clear),
            Button((90, 6, 40, 28), "-", lambda: self._change_pixel_size(-1)),
            Button((135, 6, 40, 28), "+", lambda: self._change_pixel_size(1)),
        ]

    def _resize_window(self):
        width = max(self.disp_w * self.pixel_size, 260)
        height = self.disp_h * self.pixel_size + BUTTON_BAR_HEIGHT
        self.screen = pygame.display.set_mode((width, height))

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
                elif event.type == pygame.MOUSEBUTTONDOWN:
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
        self.screen.blit(size_label, (185, 12))

        if self.status_ttl > 0:
            status_label = self.font.render(self.status_message, True, BUTTON_TEXT_COLOR)
            self.screen.blit(status_label, (280, 12))
            self.status_ttl -= 1

        # Show connection status for both servers
        status_x = self.screen.get_width() - 200
        if not self._is_display_connected():
            display_label = self.font.render("Display: Reconnecting...", True, DISCONNECTED_COLOR)
            self.screen.blit(display_label, (status_x, 12))
            status_x -= 150

        if not self._is_hid_connected():
            hid_label = self.font.render("HID: Reconnecting...", True, DISCONNECTED_COLOR)
            self.screen.blit(hid_label, (status_x, 12))

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