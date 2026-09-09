"""Display bridge for the pigeon emulator.

This module keeps the emulator RAM display region in an in-memory frame
buffer and serves it to external clients via a background FastAPI server.

The emulator should call `DisplayIO.update()` periodically. The update
function creates a snapshot from RAM and stores it in memory as the
current frame (converted to RGBA byte order for web clients).
"""

from pathlib import Path
from threading import Lock, Thread
from typing import Optional

from ..memory_map import DISPLAY_START, DISPLAY_SIZE, DISPLAY_W, DISPLAY_H

# devices/ -> emulator/ -> repo root, where display/index.html lives.
# This used to be resolved relative to this file's own directory; once the
# module moved into emulator/devices/ that silently pointed at nothing, and
# the miss is swallowed by the except below (you get "Frontend not found",
# not an error).
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_HTML = REPO_ROOT / "display" / "index.html"


class DisplayIO:
    def __init__(self, ram):
        self.ram = ram
        self.display_start = DISPLAY_START
        self.display_size = DISPLAY_SIZE
        # In-memory frame buffer (RGBA byte order) for FastAPI serving
        self._frame_lock = Lock()
        self._frame: Optional[bytes] = None
        self._server_thread: Optional[Thread] = None
        # Told to the browser front end via /info so it knows where to POST
        # input. Keeps config.json the single source of truth for ports.
        self.hid_url: Optional[str] = None

    def update(self) -> bool:
        """Snapshot the current display region from RAM into the in-memory frame buffer.

        Returns True if a new frame was captured.
        """
        data = bytes(self.ram.mem[self.display_start : self.display_start + self.display_size])
        try:
            rgba = self._convert_to_rgba(data)
        except Exception:
            # Keep updates tolerant: don't crash the emulator if conversion fails
            return False
        with self._frame_lock:
            self._frame = rgba
        return True

    def _convert_to_rgba(self, data: bytes) -> bytes:
        # Incoming memory layout is B,G,R,A per pixel; convert to R,G,B,A
        if not data:
            return b"\x00" * self.display_size
        # Swap the R and B channels with strided slice assignment. The
        # equivalent per-byte Python loop cost 2.2 ms/frame at 100x100 --
        # a 450 FPS ceiling that would drop under 15 FPS at 640x480.
        out = bytearray(data)
        out[0::4] = data[2::4]   # R <- B
        out[2::4] = data[0::4]   # B <- R
        return bytes(out)

    def get_frame(self) -> bytes:
        """Return the latest frame in RGBA byte order. If no frame exists, return zeros."""
        with self._frame_lock:
            if self._frame is None:
                return b"\x00" * self.display_size
            return self._frame

    def clear(self) -> None:
        """Clear the in-memory frame."""
        with self._frame_lock:
            self._frame = b"\x00" * self.display_size

    def start_fastapi(self, host: str = "127.0.0.1", port: int = 8000, serve_frontend: bool = True):
        """Start a background FastAPI server that exposes the current frame and control endpoints.

        This is optional and runs in a daemon thread. FastAPI endpoints are async so they do not
        block the emulator; frames are served from an in-memory buffer.
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            return

        # Checked here, on the caller's thread: an ImportError raised inside
        # the daemon thread below is swallowed, leaving the emulator running
        # with no display and no explanation.
        try:
            from fastapi import FastAPI, Response
            from fastapi.responses import HTMLResponse
            from fastapi.middleware.cors import CORSMiddleware
            import uvicorn
        except ImportError as e:
            raise RuntimeError(
                "FastAPI/uvicorn not installed: pip install -r requirements.txt") from e

        def _run():
            app = FastAPI()
            app.add_middleware(
                CORSMiddleware,
                allow_origins=[f"http://{host}:{port}", "http://127.0.0.1", "http://localhost"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
            )

            @app.get("/frame")
            async def frame():
                data = self.get_frame()
                return Response(content=data, media_type="application/octet-stream")

            @app.post("/clear")
            async def clear_endpoint():
                self.clear()
                return {"status": "ok"}

            @app.get("/info")
            async def info():
                return {"w": DISPLAY_W, "h": DISPLAY_H, "size": self.display_size,
                        "hid_url": self.hid_url}

            if serve_frontend:
                @app.get("/", response_class=HTMLResponse)
                async def index():
                    try:
                        return HTMLResponse(content=FRONTEND_HTML.read_text(encoding="utf-8"))
                    except OSError:
                        return HTMLResponse(
                            content=f"<html><body>Frontend not found at "
                                    f"{FRONTEND_HTML}</body></html>")

            uvicorn.run(app, host=host, port=port, log_level="warning")

        self._server_thread = Thread(target=_run, daemon=True)
        self._server_thread.start()