"""Network-free tests: python -B -m unittest discover -s tests -p test_playback.py -v."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from lookout.onvif import Camera, stream_uri
from lookout.playback import Stream


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.camera = Camera(
            "camera-1", "Front", "rtsp://never-contact.invalid/live", "nvr", "Home",
            "test-user", "secret password/@",
        )
        self.streams = []
        self.processes = []
        self.popen = subprocess.Popen
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for stream in self.streams:
            stream.stop()
        for stream in self.streams:
            if stream._thread is not None:
                stream._thread.join(2)
                self.assertFalse(stream._thread.is_alive(), "Playback worker did not stop")
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()

    def stream(self, on_frame=None, on_status=None, demo=False, *, connect_timeout=10.0):
        stream = Stream(
            self.camera, on_frame or Mock(), on_status or Mock(), demo,
            connect_timeout=connect_timeout,
        )
        self.streams.append(stream)
        return stream

    def decoder(self, script):
        """Replace only FFmpeg with a real pipe-producing, network-free child."""
        def spawn(command, **kwargs):
            process = self.popen([sys.executable, "-B", "-c", script], **kwargs)
            self.processes.append(process)
            return process
        return spawn

    def test_rtsp_command_and_thread_limits(self):
        stream = self.stream()
        command = stream._command()
        input_index = command.index("-i")
        self.assertEqual(command[input_index + 1], stream_uri(self.camera))
        for option, value in (
            ("-rtsp_transport", "tcp"), ("-timeout", "5000000"),
            ("-allowed_media_types", "video"),
            ("-flags", "low_delay"), ("-analyzeduration", "100000"), ("-probesize", "32768"),
            ("-cpucount", "2"),
            ("-filter_threads", "2"), ("-filter_complex_threads", "2"),
            ("-pix_fmt", "rgb24"), ("-c:v", "rawvideo"),
        ):
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertEqual(command[command.index("-vf") + 1], (
            "fps=10,scale=640:360:force_original_aspect_ratio=decrease,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2"
        ))
        self.assertEqual(command.count("-threads"), 2)
        self.assertEqual(command[input_index - 2:input_index], ["-threads", "2"])
        self.assertEqual(command[-5:], ["-threads", "2", "-f", "rawvideo", "pipe:1"])
        for option in ("-an", "-sn", "-dn", "-nostdin"):
            self.assertIn(option, command)
        self.assertNotIn("-rw_timeout", command)
        self.assertNotIn("nobuffer", command)
        self.assertNotIn("-stimeout", command)

    def test_timeout_option_matches_input_protocol(self):
        for scheme in ("rtsp", "rtsps", "http", "https"):
            with self.subTest(scheme=scheme):
                self.camera.uri = f"{scheme}://never-contact.invalid/live"
                command = self.stream()._command()
                rtsp = scheme in ("rtsp", "rtsps")
                self.assertEqual("-timeout" in command, rtsp)
                self.assertEqual("-rw_timeout" in command, not rtsp)
                self.assertEqual("-rtsp_transport" in command, rtsp)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_installed_ffmpeg_rtsp_timeout_option(self):
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"],
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-timeout ", result.stdout)
        self.assertNotIn("-rw_timeout ", result.stdout)
        self.assertNotIn("-stimeout ", result.stdout)

    def test_timeout_api_defaults_and_validation(self):
        stream = self.stream()
        self.assertEqual(stream.connect_timeout, 10.0)
        self.assertEqual(stream._READ_TIMEOUT, 10.0)
        with self.assertRaises(TypeError):
            Stream(self.camera, Mock(), Mock(), False, 0.1)
        for timeout in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.stream(connect_timeout=timeout)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_real_demo_frames_stop_and_immediate_replacement(self):
        frames = []
        statuses = []
        callback_threads = []
        received = threading.Event()

        def on_frame(frame):
            frames.append(frame)
            callback_threads.append(threading.get_ident())
            if len(frames) >= 8:
                received.set()

        def on_status(status):
            statuses.append(status)
            callback_threads.append(threading.get_ident())

        with patch("lookout.playback.stream_uri", side_effect=AssertionError("Demo used a URL")):
            stream = self.stream(on_frame, on_status, demo=True)
            started = time.monotonic()
            stream.start()
            stream.start()
            self.assertTrue(received.wait(3), statuses)
            # FFmpeg may flush its first few buffered frames together at startup.
            self.assertGreater(time.monotonic() - started, 0.3, "Demo was not paced")
            process = stream._process
            self.assertIsNotNone(process)
            self.assertEqual(os.getpgid(process.pid), process.pid)
            self.assertEqual(set(callback_threads), {stream._thread.ident})
            self.assertNotEqual(stream._thread.ident, threading.get_ident())
            self.assertEqual(statuses, ["Connecting", "Live"])
            self.assertTrue(all(isinstance(frame, bytes) and len(frame) == 640 * 360 * 3 for frame in frames))

            before_stop = time.monotonic()
            stream.stop()
            self.assertLess(time.monotonic() - before_stop, 0.2)
            counts = (len(frames), len(statuses))
            self.camera = Camera("camera-2", "Back", "not-a-url", "nvr", "Home")
            replacement_frames = []
            replacement_received = threading.Event()

            def replacement_frame(frame):
                replacement_frames.append(frame)
                replacement_received.set()

            replacement = self.stream(replacement_frame, demo=True)
            replacement.start()
            self.assertTrue(replacement_received.wait(3))
            self.assertNotEqual(frames[0], replacement_frames[0], "Camera hue did not change")
            replacement.stop()
            stream.stop()
            stream.start()  # A stopped instance must not restart.
            stream._thread.join(1)
            replacement._thread.join(1)
            self.assertFalse(stream._thread.is_alive())
            self.assertFalse(replacement._thread.is_alive())
            self.assertIsNotNone(process.returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(process.pid, 0)
            self.assertEqual((len(frames), len(statuses)), counts)

    def test_partial_reads_and_truncated_final_frame(self):
        frames, statuses = [], []

        script = (
            "import os\n"
            "data = bytes([17]) * (640 * 360 * 3) + bytes([239]) * (640 * 360 * 3) + b'partial'\n"
            "for offset in range(0, len(data), 997):\n"
            "    os.write(1, data[offset:offset + 997])\n"
        )
        with patch("lookout.playback.subprocess.Popen", side_effect=self.decoder(script)) as spawn:
            stream = self.stream(frames.append, statuses.append, connect_timeout=0.5)
            stream.start()
            stream._thread.join(1)
            self.assertFalse(stream._thread.is_alive())
            self.assertEqual(frames, [bytes([17]) * (640 * 360 * 3), bytes([239]) * (640 * 360 * 3)])
            self.assertEqual(statuses, ["Connecting", "Live", "Stream unavailable"])
            self.assertEqual(spawn.call_count, 1)
            stream.start()
            self.assertEqual(spawn.call_count, 1, "Disconnected streams must not restart")

    def test_initial_stall_uses_connection_budget_not_live_watchdog(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                statuses = []
                frame = Mock()
                script = (
                    "import os, signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "while True:\n"
                    + ("    os.write(1, b'x')\n" if partial else "")
                    + "    time.sleep(0.03)\n"
                )
                with patch("lookout.playback.subprocess.Popen", side_effect=self.decoder(script)) as spawn:
                    stream = self.stream(frame, statuses.append, connect_timeout=0.2)
                    stream._READ_TIMEOUT = 0.05
                    started = time.monotonic()
                    stream.start()
                    stream._thread.join(1)
                    elapsed = time.monotonic() - started
                    self.assertFalse(stream._thread.is_alive())
                    self.assertGreaterEqual(elapsed, 0.2)
                    self.assertLess(elapsed, 0.6)
                    self.assertEqual(self.processes[-1].returncode, -signal.SIGKILL)
                    self.assertEqual(spawn.call_count, 1)
                    frame.assert_not_called()
                    self.assertEqual(statuses, ["Connecting", "Timed out"])

    def test_failed_launches_share_one_budget_and_errors_are_private(self):
        statuses = []
        stream = self.stream(on_status=statuses.append, connect_timeout=0.25)
        error = OSError(f"Cannot launch {stream_uri(self.camera)}")

        def failed_launch(*args, **kwargs):
            time.sleep(0.04)
            raise error

        with patch("lookout.playback.subprocess.Popen", side_effect=failed_launch) as spawn, \
                patch.object(stream._stopped, "wait", wraps=stream._stopped.wait) as wait:
            started = time.monotonic()
            stream.start()
            stream._thread.join(1)
            elapsed = time.monotonic() - started
            self.assertFalse(stream._thread.is_alive())
            self.assertGreaterEqual(spawn.call_count, 2)
            self.assertLessEqual(spawn.call_count, 3)
            worker = stream._thread
            calls = spawn.call_count
            stream.start()
            self.assertIs(stream._thread, worker)
            self.assertEqual(spawn.call_count, calls)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 0.6)
        delays = [call.args[0] for call in wait.call_args_list]
        self.assertTrue(all(0 <= delay <= 0.1 for delay in delays))
        self.assertLess(delays[-1], 0.1, "Backoff must be capped by the remaining budget")
        self.assertEqual(statuses, ["Connecting", "Timed out"])
        self.assertNotIn("secret", " ".join(statuses))
        self.assertNotIn("test-user", " ".join(statuses))
        stream.on_frame.assert_not_called()

    def test_truncated_frames_across_retries_do_not_reset_budget(self):
        statuses = []
        script = "import os; os.write(1, b'partial')"
        with patch("lookout.playback.subprocess.Popen", side_effect=self.decoder(script)) as spawn:
            stream = self.stream(on_status=statuses.append, connect_timeout=0.35)
            stream.WIDTH = stream.HEIGHT = 2
            started = time.monotonic()
            stream.start()
            stream._thread.join(1)
            elapsed = time.monotonic() - started
            self.assertFalse(stream._thread.is_alive())
            self.assertGreaterEqual(spawn.call_count, 2)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 0.7)
        self.assertEqual(statuses, ["Connecting", "Timed out"])
        stream.on_frame.assert_not_called()
        for process in self.processes:
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdout.closed)

    def test_pipe_setup_failures_are_bounded_and_reaped(self):
        statuses = []
        child = self.decoder("import time; time.sleep(30)")
        with patch("lookout.playback.subprocess.Popen", side_effect=child) as spawn, \
                patch("lookout.playback.os.set_blocking", side_effect=OSError("secret")):
            stream = self.stream(on_status=statuses.append, connect_timeout=0.25)
            stream.start()
            stream._thread.join(1)
            self.assertFalse(stream._thread.is_alive())
            self.assertGreaterEqual(spawn.call_count, 2)
            self.assertEqual(statuses, ["Connecting", "Timed out"])
            stream.on_frame.assert_not_called()
            for process in self.processes:
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertTrue(process.stdout.closed)

    def test_complete_frame_read_after_deadline_is_discarded(self):
        child = self.decoder("import os, time; os.write(1, b'abcdefghijkl'); time.sleep(30)")
        process = child([], stdout=subprocess.PIPE, bufsize=0, start_new_session=True)
        stream = self.stream()
        stream.WIDTH = stream.HEIGHT = 2
        clock = Mock(wraps=time)
        clock.monotonic.return_value = 0.0
        read = os.read

        def late_read(fd, size):
            data = read(fd, size)
            clock.monotonic.return_value = 1.0
            return data

        with patch("lookout.playback.time", clock), patch("lookout.playback.os.read", side_effect=late_read):
            self.assertEqual(stream._read_frames(process, deadline=1.0), "Timed out")
        stream.on_frame.assert_not_called()
        stream.on_status.assert_not_called()

    def test_retry_success_near_deadline_can_play_past_initial_budget(self):
        frames, statuses = [], []
        playing = threading.Event()

        def on_frame(frame):
            frames.append(frame)
            if len(frames) == 5:
                playing.set()

        child = self.decoder(
            "import os, time\n"
            "while True:\n"
            "    os.write(1, b'abcdefghijkl')\n"
            "    time.sleep(0.08)\n"
        )
        started = time.monotonic()

        def spawn(command, **kwargs):
            if mocked_spawn.call_count == 1:
                raise OSError("Transient failure")
            time.sleep(max(0, started + 0.4 - time.monotonic()))
            return child(command, **kwargs)

        with patch("lookout.playback.subprocess.Popen", side_effect=spawn) as mocked_spawn:
            stream = self.stream(on_frame, statuses.append, connect_timeout=0.6)
            stream.WIDTH = stream.HEIGHT = 2
            stream._READ_TIMEOUT = 0.2
            stream.start()
            self.assertTrue(playing.wait(1.5), statuses)
            self.assertGreater(time.monotonic() - started, 0.6)
            self.assertEqual(statuses, ["Connecting", "Live"])
            self.assertTrue(all(frame == b"abcdefghijkl" for frame in frames))
            self.assertEqual(mocked_spawn.call_count, 2)
            stream.stop()
            stream._thread.join(1)
            self.assertFalse(stream._thread.is_alive())

    def test_live_stall_times_out_even_with_partial_data(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                frames, statuses = [], []
                script = (
                    "import os, time\n"
                    "os.write(1, b'abcdefghijkl')\n"
                    "while True:\n"
                    "    time.sleep(0.04)\n"
                    + ("    os.write(1, b'x')\n" if partial else "")
                )
                with patch("lookout.playback.subprocess.Popen", side_effect=self.decoder(script)) as spawn:
                    stream = self.stream(frames.append, statuses.append, connect_timeout=0.2)
                    stream.WIDTH = stream.HEIGHT = 2
                    stream._READ_TIMEOUT = 0.3
                    started = time.monotonic()
                    stream.start()
                    stream._thread.join(1)
                    self.assertFalse(stream._thread.is_alive())
                    self.assertGreaterEqual(time.monotonic() - started, 0.3)
                    self.assertEqual(statuses, ["Connecting", "Live", "Timed out"])
                    self.assertEqual(frames, [b"abcdefghijkl"])
                    self.assertEqual(spawn.call_count, 1)
                    self.assertEqual(self.processes[-1].returncode, -signal.SIGKILL)
                    self.assertTrue(self.processes[-1].stdout.closed)

    def test_stop_interrupts_retry_backoff(self):
        retry = threading.Event()
        statuses = []
        stream = self.stream(on_status=statuses.append, connect_timeout=0.5)
        wait = stream._stopped.wait

        def waiting(delay):
            retry.set()
            return wait(delay)

        with patch("lookout.playback.subprocess.Popen", side_effect=OSError("secret")) as spawn, \
                patch.object(stream._stopped, "wait", side_effect=waiting):
            stream.start()
            self.assertTrue(retry.wait(1))
            started = time.monotonic()
            stream.stop()
            self.assertLess(time.monotonic() - started, 0.2)
            stream._thread.join(0.5)
            self.assertFalse(stream._thread.is_alive())
            self.assertEqual(statuses, ["Connecting"])
            self.assertEqual(spawn.call_count, 1)
            stream.on_frame.assert_not_called()

    def test_stop_interrupts_stalled_reads_without_waiting_for_watchdog(self):
        reading = threading.Event()
        statuses = []
        stream = self.stream(on_status=statuses.append)
        read_frames = stream._read_frames

        def read(process, deadline):
            reading.set()
            return read_frames(process, deadline)

        with patch("lookout.playback.subprocess.Popen", side_effect=self.decoder("import time; time.sleep(30)")), \
                patch.object(stream, "_read_frames", side_effect=read):
            stream.start()
            self.assertTrue(reading.wait(1))
            started = time.monotonic()
            stream.stop()
            self.assertLess(time.monotonic() - started, 0.2)
            stream._thread.join(0.5)
            self.assertFalse(stream._thread.is_alive())
            self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
            self.assertEqual(statuses, ["Connecting"])
            stream.on_frame.assert_not_called()

    def test_missing_ffmpeg_is_safe_and_does_not_retry(self):
        statuses = []
        with patch.dict(os.environ, {"FFREPORT": "file=should-not-exist.log"}), \
                patch("lookout.playback.subprocess.Popen", side_effect=FileNotFoundError("secret")) as spawn:
            stream = self.stream(on_status=statuses.append)
            stream.start()
            stream._thread.join(1)
            self.assertFalse(stream._thread.is_alive())
            self.assertEqual(statuses, ["Connecting", "Stream unavailable"])
            self.assertEqual(spawn.call_count, 1)
            kwargs = spawn.call_args.kwargs
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertTrue(kwargs["start_new_session"])
            self.assertFalse(kwargs.get("shell", False))
            self.assertNotIn("FFREPORT", kwargs["env"])

    def test_stop_during_process_creation(self):
        spawning, release = threading.Event(), threading.Event()
        statuses = []
        spawn_child = self.decoder("import time; time.sleep(30)")

        def spawn(command, **kwargs):
            spawning.set()
            release.wait(2)
            return spawn_child(command, **kwargs)

        with patch("lookout.playback.subprocess.Popen", side_effect=spawn):
            stream = self.stream(on_status=statuses.append)
            stream.start()
            self.assertTrue(spawning.wait(1))
            before_stop = time.monotonic()
            try:
                stream.stop()
            finally:
                release.set()
            self.assertLess(time.monotonic() - before_stop, 0.2)
            stream._thread.join(1)
            self.assertFalse(stream._thread.is_alive())
            self.assertEqual(statuses, ["Connecting"])
            self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)

    def test_process_creation_and_reads_share_connection_budget(self):
        statuses = []
        child = self.decoder("import time; time.sleep(30)")

        def spawn(command, **kwargs):
            time.sleep(0.25)
            return child(command, **kwargs)

        with patch("lookout.playback.subprocess.Popen", side_effect=spawn) as mocked_spawn:
            stream = self.stream(on_status=statuses.append, connect_timeout=0.4)
            stream._READ_TIMEOUT = 0.4
            started = time.monotonic()
            stream.start()
            stream._thread.join(1)
            elapsed = time.monotonic() - started
            self.assertFalse(stream._thread.is_alive())
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLess(elapsed, 0.6, "Process creation must not get a separate budget")
            self.assertEqual(statuses, ["Connecting", "Timed out"])
            self.assertEqual(mocked_spawn.call_count, 1)
            self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
            stream.on_frame.assert_not_called()

    def test_timeout_during_process_creation_ignores_late_results(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                spawning, release, timed_out = threading.Event(), threading.Event(), threading.Event()
                statuses, callback_threads = [], []
                child = self.decoder("import os, time; os.write(1, b'abcdefghijkl'); time.sleep(30)")

                def status(value):
                    statuses.append(value)
                    callback_threads.append(threading.get_ident())
                    if value == "Timed out":
                        timed_out.set()

                def spawn(command, **kwargs):
                    spawning.set()
                    release.wait(2)
                    if fail:
                        raise FileNotFoundError("secret")
                    return child(command, **kwargs)

                with patch("lookout.playback.subprocess.Popen", side_effect=spawn) as mocked_spawn:
                    stream = self.stream(on_status=status, connect_timeout=0.15)
                    stream.WIDTH = stream.HEIGHT = 2
                    stream.start()
                    try:
                        self.assertTrue(spawning.wait(1))
                        self.assertTrue(timed_out.wait(0.5), "Timeout waited for process creation")
                        stream.start()
                        self.assertEqual(mocked_spawn.call_count, 1)
                        self.assertEqual(statuses, ["Connecting", "Timed out"])
                    finally:
                        release.set()
                        stream._thread.join(1)
                    self.assertFalse(stream._thread.is_alive())
                    self.assertEqual(statuses, ["Connecting", "Timed out"])
                    self.assertEqual(set(callback_threads), {stream._thread.ident})
                    self.assertIsNone(stream._process)
                    stream.on_frame.assert_not_called()
                    if not fail:
                        self.assertEqual(self.processes[-1].returncode, -signal.SIGKILL)
                        self.assertTrue(self.processes[-1].stdout.closed)

    def test_command_preparation_is_in_connection_budget(self):
        preparing, release, timed_out = threading.Event(), threading.Event(), threading.Event()
        statuses = []

        def status(value):
            statuses.append(value)
            if value == "Timed out":
                timed_out.set()

        stream = self.stream(on_status=status, connect_timeout=0.1)
        command = stream._command

        def prepare():
            preparing.set()
            release.wait(2)
            return command()

        with patch.object(stream, "_command", side_effect=prepare), \
                patch("lookout.playback.subprocess.Popen") as spawn:
            stream.start()
            try:
                self.assertTrue(preparing.wait(1))
                self.assertTrue(timed_out.wait(0.5))
            finally:
                release.set()
                stream._thread.join(1)
            self.assertFalse(stream._thread.is_alive())
            self.assertEqual(statuses, ["Connecting", "Timed out"])
            spawn.assert_not_called()

    def test_exhausted_budget_never_launches(self):
        for timeout in (0.0, -0.1):
            with self.subTest(timeout=timeout), patch("lookout.playback.subprocess.Popen") as spawn:
                statuses = []
                stream = self.stream(on_status=statuses.append, connect_timeout=timeout)
                stream.start()
                stream._thread.join(0.5)
                self.assertFalse(stream._thread.is_alive())
                self.assertEqual(statuses, ["Connecting", "Timed out"])
                spawn.assert_not_called()
                stream.on_frame.assert_not_called()

    def test_stop_before_start(self):
        with patch("lookout.playback.subprocess.Popen") as spawn:
            stream = self.stream()
            stream.stop()
            stream.start()
            self.assertIsNone(stream._thread)
            spawn.assert_not_called()
            stream.on_frame.assert_not_called()
            stream.on_status.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_interpreter_exit_reaps_decoder(self):
        script = (
            "import threading\n"
            "from lookout.onvif import Camera\n"
            "from lookout.playback import Stream\n"
            "ready = threading.Event()\n"
            "stream = Stream(Camera('exit', 'Exit', '', '', ''), lambda frame: ready.set(), lambda status: None, demo=True)\n"
            "stream.start()\n"
            "assert ready.wait(3), 'Demo did not start'\n"
            "print(stream._process.pid, flush=True)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[1], timeout=4,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = int(result.stdout.strip())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.killpg(pid, signal.SIGKILL)
            self.fail("Interpreter exit left the decoder running")


if __name__ == "__main__":
    unittest.main()
