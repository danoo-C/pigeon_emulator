#!/usr/bin/env python3
"""Pygame front-end for the pigeon emulator's DisplayIO FastAPI server.

Polls GET /frame for the current RGBA framebuffer and renders it scaled
by an adjustable pixel size. Includes buttons to clear the display and
to increase/decrease the pixel size.

Requires: pygame, requests
    pip install pygame requests

Usage:
    python display_client.py [--host 127.0.0.1] [--port 8000] [--fps 30]
"""
import argparse
import threading
import time

import pygame
import requests

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

    def _send_key(self, key_code: int):
        """Send a keyboard key code to HID server."""
        if not self._is_hid_connected():
            return
        try:
            self.hid_session.post(
                f"{self.hid_url}/key",
                json={"code": key_code},
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
                    # Button indices: 1=left, 2=middle, 3=right, 4=scroll up, 5=scroll down
                    # Map to HID indices: 0=left, 1=right, 2=middle, 3=back, 4=forward
                    if event.button == 1:
                        self._send_mouse_button(0, True)
                    elif event.button == 3:
                        self._send_mouse_button(1, True)
                    elif event.button == 2:
                        self._send_mouse_button(2, True)

                    # Check if click is on a button (only if above button bar)
                    if event.pos[1] < BUTTON_BAR_HEIGHT:
                        for button in self.buttons:
                            if button.handle_click(event.pos):
                                break
                elif event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        self._send_mouse_button(0, False)
                    elif event.button == 3:
                        self._send_mouse_button(1, False)
                    elif event.button == 2:
                        self._send_mouse_button(2, False)
                elif event.type == pygame.KEYDOWN:
                    # Send the key code (pygame.key.key_code() returns printable char or name)
                    # For simplicity, send the key value; the emulator can interpret it
                    # Common ASCII codes: A-Z are 65-90, 0-9 are 48-57, space is 32, enter is 13, etc.
                    self._send_key(event.key)

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


def main():
    parser = argparse.ArgumentParser(description="Pigeon emulator display + input client")
    parser.add_argument("--host", default="127.0.0.1", help="Display server host")
    parser.add_argument("--port", type=int, default=8000, help="Display server port")
    parser.add_argument("--hid-host", default="127.0.0.1", help="HID server host")
    parser.add_argument("--hid-port", type=int, default=8001, help="HID server port")
    parser.add_argument("--fps", type=int, default=30, help="Display update FPS")
    parser.add_argument("--pixel-size", type=int, default=8, help="Initial pixel size magnification")
    args = parser.parse_args()

    client = DisplayClient(args.host, args.port, args.fps, args.pixel_size, args.hid_host, args.hid_port)
    client.run()


if __name__ == "__main__":
    main()