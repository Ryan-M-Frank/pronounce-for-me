"""macOS command-line preview; no global keyboard hooks or permissions required."""
from __future__ import annotations
import os
import subprocess
import threading
import time
from pathlib import Path
from collections.abc import Callable


def _terminate(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def play_mp3(path: Path, alias: str, cancelled: Callable[[], bool]) -> None:
    """Use the system audio player, stopping its process on cancellation."""
    if cancelled():
        return
    proc = subprocess.Popen(["/usr/bin/afplay", str(path.resolve())],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        while proc.poll() is None:
            if cancelled():
                return
            time.sleep(0.05)
        if proc.returncode:
            raise RuntimeError(f"afplay exited with status {proc.returncode}")
    finally:
        _terminate(proc)


class NativeSpeaker:
    """Local speech via macOS say. Percent rates approximate a 175 WPM baseline."""
    def __init__(self, voice: str = "", rate: int = 0):
        self.voice = voice
        self.rate = rate
        self._proc = None
        self._lock = threading.RLock()

    def describe(self):
        return f"macOS voice ({self.voice or 'system default'})"

    def start(self):
        pass

    def say(self, text, rate=None, blocking=False, respelled=None):
        # Existing respellings were tuned for Windows SAPI, not Apple voices.
        # Explicit use_overrides="always" is already applied to text by the caller.
        if not text.strip():
            return
        percent = self.rate if rate is None else int(rate)
        wpm = max(80, min(500, round(175 * (1 + percent / 100))))
        command = ["/usr/bin/say", "-r", str(wpm)]
        if self.voice:
            command += ["-v", self.voice]
        with self._lock:
            self.stop()
            proc = subprocess.Popen(command, stdin=subprocess.PIPE)
            self._proc = proc
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except BaseException:
                _terminate(proc)
                raise
        if blocking:
            try:
                status = proc.wait()
                if status:
                    raise RuntimeError(f"say exited with status {status}")
            finally:
                _terminate(proc)

    def stop(self):
        with self._lock:
            if self._proc is not None:
                _terminate(self._proc)
                self._proc = None

    def stop_process(self, timeout=2.0):
        self.stop()


def list_native_voices():
    print('Installed macOS voices (backend "native"):', flush=True)
    return subprocess.run(["/usr/bin/say", "-v", "?"]).returncode


def grab_selection(restore=False):
    """Read explicitly copied text; do not simulate keys in this milestone."""
    # Pin the child's encoding even when the calling shell sets LC_ALL=C.
    env = dict(os.environ, LC_ALL="en_US.UTF-8")
    return subprocess.run(["/usr/bin/pbpaste"], check=True, env=env,
                          stdout=subprocess.PIPE).stdout.decode("utf-8", "replace")


def beep():
    print("\a", end="", flush=True)


def claim_single_instance():
    raise RuntimeError("macOS background listener is not implemented yet")


class Listener:
    def run(self):
        print("macOS preview supports --say, --clipboard, --audition and --list-voices. "
              "Global hotkeys and background listening are not available yet.")
        return 1
