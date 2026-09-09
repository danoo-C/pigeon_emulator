"""Display bridge for the pigeon emulator.

This module keeps the emulator RAM display region in an in-memory frame
buffer and serves it to external clients via a background FastAPI server.

The emulator should call `DisplayIO.update()` periodically. The update
function creates a snapshot from RAM and stores it in memory as the
current frame (converted to RGBA byte order for web clients).
"""

from threading import Lock, Thread
from typing import Optional

# Optional FastAPI imports are done lazily to avoid hard dependency when not used

from memory_map import DISPLAY_START, DISPLAY_SIZE, DISPLAY_W, DISPLAY_H


class DisplayIO:
    def __init__(self, ram):
        self.ram = ram
        self.display_start = DISPLAY_START
        self.display_size = DISPLAY_SIZE
        # In-memory frame buffer (RGBA byte order) for FastAPI serving
        self._frame_lock = Lock()
        self._frame: Optional[bytes] = None
        self._server_thread: Optional[Thread] = None

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
        out = bytearray(len(data))
        # process 4 bytes per pixel
        for i in range(0, len(data), 4):
            b = data[i]
            g = data[i + 1]
            r = data[i + 2]
            a = data[i + 3]
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            out[i + 3] = a
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

        def _run():
            try:
                from fastapi import FastAPI, Response
                from fastapi.responses import HTMLResponse
                from fastapi.middleware.cors import CORSMiddleware
                import uvicorn
            except Exception as e:
                raise RuntimeError("FastAPI/uvicorn not installed: install fastapi and uvicorn") from e

            app = FastAPI()
            app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])

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
                return {"w": DISPLAY_W, "h": DISPLAY_H, "size": self.display_size}

            if serve_frontend:
                from pathlib import Path

                @app.get("/", response_class=HTMLResponse)
                async def index():
                    # Serve the small frontend HTML from the display folder
                    try:
                        here = Path(__file__).resolve().parent / "display" / "index.html"
                        return HTMLResponse(content=here.read_text(encoding="utf-8"))
                    except Exception:
                        return HTMLResponse(content="<html><body>Frontend not found</body></html>")

            uvicorn.run(app, host=host, port=port, log_level="warning")

        self._server_thread = Thread(target=_run, daemon=True)
        self._server_thread.start()