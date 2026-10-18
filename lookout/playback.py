"""GTK-independent FFmpeg playback; callbacks run on the decoder worker thread.

FFmpeg receives the authenticated URL as an argument, so RTSP credentials are
visible in its process command line to the same user (and privileged users).
No shell is used, stderr is discarded, and FFmpeg report files are disabled.
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
import hashlib
import math
import os
import selectors
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .onvif import Camera, OnvifError, stream_uri


class Stream:
    """A single-use stream delivering packed RGB24 frames, without a frame queue.

    Callbacks must return promptly; the UI must dispatch/coalesce them itself.
    stop() never joins the worker. It suppresses subsequent callbacks, but cannot
    retract an in-flight callback or UI work already queued by one. The UI should
    also discard callbacks from replaced streams. Create a new Stream to restart.
    Normal interpreter exit stops and reaps any remaining decoder.

    connect_timeout is one budget from start() through the first complete frame,
    including retries. Once live, only _READ_TIMEOUT without a frame times out;
    a disconnected decoder stops without reconnecting.
    """

    WIDTH = 640
    HEIGHT = 360
    FPS = 10

    _READ_TIMEOUT = 10.0

    def __init__(
        self,
        camera: Camera,
        on_frame: Callable[[bytes], None],
        on_status: Callable[[str], None],
        demo: bool = False,
        *,
        connect_timeout: float = 10.0,
    ):
        if not math.isfinite(connect_timeout):
            raise ValueError("connect_timeout must be finite")
        self.camera = camera
        self.on_frame = on_frame
        self.on_status = on_status
        self.demo = demo
        self.connect_timeout = connect_timeout
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        """Start once, returning immediately. Repeated starts are harmless."""
        with self._lock:
            if self._thread is not None or self._stopped.is_set():
                return
            self._thread = threading.Thread(
                target=self._run, args=(time.monotonic() + self.connect_timeout,),
                name="lookout-playback", daemon=True,
            )
            atexit.register(self._at_exit)
            self._thread.start()

    def stop(self) -> None:
        """Cancel reads/retries and kill the decoder without waiting for it."""
        self._stopped.set()
        with self._lock:
            if self._process is not None:
                self._kill(self._process)

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        # This decoder writes no files, so immediate kill needs no flush/grace
        # period. Reaping stays on the worker, never on the GTK thread.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _at_exit(self) -> None:
        self.stop()
        # Also cover exit while Popen is still creating the child. Popen is not
        # under _lock, so stop() cannot be delayed by process creation.
        if self._thread is not None:
            self._thread.join()

    def _emit(self, callback: Callable, value: bytes | str) -> None:
        if not self._stopped.is_set():
            try:
                callback(value)
            except Exception:
                # Callback/exception text may contain credentials. Do not log it
                # or leave a decoder running for a consumer that has failed.
                self.stop()

    def _command(self) -> list[str]:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "quiet", "-nostdin",
            # Also cap automatic pools, including the lavfi input filter graph.
            "-cpucount", "2",
            "-filter_threads", "2", "-filter_complex_threads", "2",
        ]
        filters = (
            f"fps={self.FPS},"
            f"scale={self.WIDTH}:{self.HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={self.WIDTH}:{self.HEIGHT}:(ow-iw)/2:(oh-ih)/2"
        )
        if self.demo:
            hue = int.from_bytes(hashlib.sha256(self.camera.id.encode()).digest()[:4], "big") % 360
            command += [
                "-re", "-f", "lavfi", "-threads", "2", "-i",
                f"testsrc2=size={self.WIDTH}x{self.HEIGHT}:rate={self.FPS}",
            ]
            filters += f",hue=h={hue}"
        else:
            uri = stream_uri(self.camera)
            if urlsplit(uri).scheme.lower() in ("rtsp", "rtsps"):
                # The installed RTSP demuxer uses timeout, not stimeout.
                command += [
                    "-rtsp_transport", "tcp", "-allowed_media_types", "video",
                    "-timeout", "5000000",
                ]
            else:
                # RTSP is a demuxer, not an AVIO protocol: it rejects rw_timeout.
                command += ["-rw_timeout", "5000000"]
            command += [
                # Keep the first probed keyframe; nobuffer can discard it and
                # force an entire GOP wait before a preview can be decoded.
                "-flags", "low_delay", "-analyzeduration", "100000",
                "-probesize", "32768", "-threads", "2", "-i", uri,
            ]
        return command + [
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", filters,
            "-pix_fmt", "rgb24", "-c:v", "rawvideo", "-threads", "2",
            "-f", "rawvideo", "pipe:1",
        ]

    def _read_frames(self, process: subprocess.Popen, deadline: float) -> str | None:
        frame_size = self.WIDTH * self.HEIGHT * 3
        frame = bytearray()
        live = False
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not self._stopped.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "Timed out"
                try:
                    if not selector.select(min(0.1, remaining)):
                        continue
                    data = os.read(fd, frame_size - len(frame))
                except BlockingIOError:
                    continue
                except OSError:
                    return "Stream unavailable" if live else None
                # A ready pipe is not permission to accept a frame after expiry.
                now = time.monotonic()
                if now >= deadline:
                    return "Timed out"
                if not data:
                    return "Stream unavailable" if live else None
                frame.extend(data)
                if len(frame) == frame_size:
                    # Only a complete frame resets the watchdog; a trickle of
                    # partial data must not keep a broken stream alive forever.
                    deadline = now + self._READ_TIMEOUT
                    if not live:
                        live = True
                        self._emit(self.on_status, "Live")
                    self._emit(self.on_frame, bytes(frame))
                    frame.clear()
        return None

    def _run(self, deadline: float) -> None:
        backoff = 0.1
        try:
            self._emit(self.on_status, "Connecting")
            while not self._stopped.is_set():
                if time.monotonic() >= deadline:
                    self._emit(self.on_status, "Timed out")
                    return
                process = None
                error = None
                launched = threading.Event()

                def launch():
                    nonlocal process, error
                    try:
                        command = self._command()
                        if self._stopped.is_set() or time.monotonic() >= deadline:
                            return
                        process = subprocess.Popen(
                            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
                            env={key: value for key, value in os.environ.items() if key != "FFREPORT"},
                        )
                        with self._lock:
                            self._process = process
                    except (OSError, subprocess.SubprocessError, OnvifError, ValueError) as exc:
                        error = exc
                    finally:
                        launched.set()

                # Popen itself cannot be interrupted. Keep deadline/status handling
                # on this worker, then reap even a child created after expiry/stop.
                launcher = threading.Thread(target=launch, name="lookout-launch", daemon=True)
                launcher.start()
                try:
                    while not self._stopped.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._emit(self.on_status, "Timed out")
                            return
                        if not launched.wait(min(0.1, remaining)):
                            continue
                        if time.monotonic() >= deadline:
                            self._emit(self.on_status, "Timed out")
                            return
                        if isinstance(error, (FileNotFoundError, OnvifError, ValueError)):
                            self._emit(self.on_status, "Stream unavailable")
                            return
                        if process is not None:
                            status = self._read_frames(process, deadline)
                            if status is not None:
                                self._emit(self.on_status, status)
                                return
                        break
                except OSError:
                    # Pipe/selector setup can fail before any frame is read.
                    pass
                finally:
                    launcher.join()
                    if process is not None:
                        with self._lock:
                            self._process = None
                            self._kill(process)
                        process.wait()
                        process.stdout.close()
                if self._stopped.is_set():
                    break
                if self._stopped.wait(min(backoff, max(0.0, deadline - time.monotonic()))):
                    break
                backoff = min(1.0, backoff * 2)
        finally:
            atexit.unregister(self._at_exit)
