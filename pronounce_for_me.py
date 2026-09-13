#!/usr/bin/env python3
"""
pronounce-for-me
================

Press a hotkey anywhere in Windows and the currently highlighted text is read
aloud using the built-in Windows speech engine.

Built for drilling medical terminology: highlight a term in a PDF, Anki card,
lecture slide or web page, hit Ctrl+Alt+P, and hear it.

Dependencies: none for the built-in Windows voice. The optional "edge" backend
(Microsoft Edge's online neural voices - far better with medical Latin) needs
one package:  pip install edge-tts
  * Global hotkeys ...... user32.RegisterHotKey (ctypes)
  * Copy the selection .. user32.SendInput (ctypes)
  * Clipboard ........... user32 clipboard API (ctypes)
  * Speech, "sapi" ...... System.Speech.Synthesis via a long-lived
                          PowerShell worker process
  * Speech, "edge" ...... edge-tts for synthesis, winmm MCI (ctypes) for
                          playback, MP3s cached on disk

Usage:
    python pronounce_for_me.py            # start listening for hotkeys
    python pronounce_for_me.py --list-voices
    python pronounce_for_me.py --say "cholecystectomy"
    python pronounce_for_me.py --test
    python pronounce_for_me.py --audition "sphygmomanometer"   # hear several Edge voices
    python pronounce_for_me.py --set-voice en-US-JennyNeural   # save one to config.json
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
OVERRIDES_PATH = APP_DIR / "overrides.json"
HISTORY_PATH = APP_DIR / "history.tsv"

DEFAULT_CONFIG = {
    "hotkey_speak": "ctrl+alt+p",
    "hotkey_speak_slow": "ctrl+alt+o",
    "hotkey_stop": "ctrl+alt+s",
    "hotkey_quit": "ctrl+alt+q",
    "backend": "edge",
    "edge_voice": "en-US-MichelleNeural",
    "cache_dir": "",
    "voice": "",
    "rate": "-5%",
    "slow_rate": "-30%",
    "long_term_rate": "-10%",
    "long_term_letters": 12,
    "max_chars": 400,
    "restore_clipboard": True,
    "log_history": True,
    "use_overrides": True,
}


def log(msg: str) -> None:
    """Print if we have a console (we may be running under pythonw.exe)."""
    try:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)
    except Exception:
        pass


def parse_rate(value: object) -> int:
    """Speaking-rate setting -> percent change from normal (0 = normal).

    "-5%" style strings are used as-is. Bare numbers are the classic SAPI
    -10..10 scale, one unit being roughly 10%.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(round(value * 10))
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("%"):
        return int(round(float(text[:-1])))
    return int(round(float(text) * 10))


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = wintypes.WPARAM

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_C = 0x43

KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

ERROR_ALREADY_EXISTS = 183


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.MessageBeep.argtypes = [wintypes.UINT]

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
}

_VK_NAMES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 25):
    _VK_NAMES[f"f{_i}"] = 0x70 + _i - 1


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+p' -> (modifier flags, virtual key code)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey: {spec!r}")

    mods = 0
    for part in parts[:-1]:
        if part not in _MODIFIER_NAMES:
            raise ValueError(f"unknown modifier {part!r} in {spec!r}")
        mods |= _MODIFIER_NAMES[part]

    key = parts[-1]
    if key in _VK_NAMES:
        vk = _VK_NAMES[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(f"unknown key {key!r} in {spec!r}")

    if not mods:
        raise ValueError(f"{spec!r} needs at least one modifier (ctrl/alt/shift/win)")
    return mods | MOD_NOREPEAT, vk


def _key_event(vk: int, key_up: bool) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = user32.MapVirtualKeyW(vk, 0)
    inp.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
    inp.ki.time = 0
    inp.ki.dwExtraInfo = 0
    return inp


def _send(*events: INPUT) -> None:
    array = (INPUT * len(events))(*events)
    user32.SendInput(len(events), array, ctypes.sizeof(INPUT))


def _release_modifiers(timeout: float = 0.8) -> None:
    """Wait for the user to let go of the hotkey, then force anything stuck up.

    We are about to synthesise Ctrl+C. If the physical Alt or Shift keys are
    still held down from the hotkey itself, the target app sees Ctrl+Alt+C
    instead and the copy silently fails.
    """
    watched = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in watched):
            return
        time.sleep(0.01)

    stuck = [vk for vk in watched if user32.GetAsyncKeyState(vk) & 0x8000]
    if stuck:
        _send(*[_key_event(vk, True) for vk in stuck])
        time.sleep(0.02)


def send_copy() -> None:
    _release_modifiers()
    _send(_key_event(VK_CONTROL, False), _key_event(VK_C, False))
    time.sleep(0.02)
    _send(_key_event(VK_C, True), _key_event(VK_CONTROL, True))


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def _open_clipboard(attempts: int = 12) -> bool:
    """The clipboard is a global lock; another app may be holding it."""
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, ctypes.byref(data), size)
        finally:
            kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def grab_selection(restore: bool) -> str | None:
    """Copy whatever is highlighted and return it, leaving the clipboard as found."""
    previous = get_clipboard_text() if restore else None
    before = user32.GetClipboardSequenceNumber()

    send_copy()

    deadline = time.monotonic() + 0.7
    text = None
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if user32.GetClipboardSequenceNumber() != before:
            text = get_clipboard_text()
            if text:
                break

    if restore and previous is not None and text is not None:
        # Give the source app a beat to finish its own clipboard bookkeeping.
        threading.Timer(0.35, lambda: set_clipboard_text(previous)).start()

    return text


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

_DEHYPHENATE = re.compile(r"(\w)[-‐‑]\s*\n\s*(\w)")
_CITATIONS = re.compile(r"\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]")
_WHITESPACE = re.compile(r"\s+")
_EDGE_JUNK = " \t\"'“”‘’()[]{}<>.,;:!?*•–—"
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


def clean_text(raw: str, max_chars: int) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _DEHYPHENATE.sub(r"\1\2", text)   # PDF line-wrap: "hyper-\ntension"
    text = _CITATIONS.sub("", text)          # wiki/textbook footnote markers
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.strip(_EDGE_JUNK).strip()

    if len(text) > max_chars:
        cut = text[:max_chars]
        space = cut.rfind(" ")
        text = (cut[:space] if space > max_chars // 2 else cut).rstrip() + "..."
    return text


def split_sentences(text: str, min_chars: int = 25) -> list[str]:
    """Break a selection at sentence ends so a paragraph can start playing after its first sentence.

    Very short pieces ("e.g.", "Fig. 3", a heading ending in a colon) are glued
    to a neighbour rather than sent to the service as two-word clips. A single
    word or sentence comes back unchanged, as one chunk.
    """
    chunks: list[str] = []
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        if not part:
            continue
        if chunks and (len(part) < min_chars or len(chunks[-1]) < min_chars):
            chunks[-1] = f"{chunks[-1]} {part}"
        else:
            chunks.append(part)
    return chunks or [text]


def load_overrides() -> dict[str, str]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"could not read overrides.json ({exc}); continuing without it")
        return {}
    # Keys starting with "_" are comments, not terms.
    return {
        str(k).strip().lower(): str(v)
        for k, v in raw.items()
        if str(k).strip() and not str(k).startswith("_")
    }


def apply_overrides(text: str, overrides: dict[str, str]) -> str:
    """Swap terms the speech engine mangles for a phonetic respelling."""
    if not overrides:
        return text

    whole = re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()
    if whole in overrides:
        return overrides[whole]

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        key = word.lower()
        if key in overrides:
            return overrides[key]
        # Eponyms are usually possessive: "Raynaud's" should still hit "raynaud".
        for suffix in ("'s", "’s"):
            if key.endswith(suffix) and key[: -len(suffix)] in overrides:
                return overrides[key[: -len(suffix)]] + "'s"
        return word

    return _WORD.sub(replace, text)


def texts_for(term: str, overrides: dict[str, str], mode: object) -> tuple[str, str]:
    """Return (text for a neural voice, text for the Windows voice).

    use_overrides: true / "sapi" -> respellings go only to the Windows voice,
    which is the one that needs them; "always" -> to both; false -> to neither.
    """
    if not overrides or mode is False or str(mode).strip().lower() in ("false", "never", "0", ""):
        return term, term
    respelled = apply_overrides(term, overrides)
    if str(mode).strip().lower() == "always":
        return respelled, respelled
    return term, respelled


def record_history(term: str) -> None:
    try:
        new_file = not HISTORY_PATH.exists()
        with HISTORY_PATH.open("a", encoding="utf-8", newline="") as handle:
            if new_file:
                handle.write("timestamp\tterm\n")
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{term}\n")
    except OSError as exc:
        log(f"could not write history ({exc})")


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000

# Long-lived worker. Starting powershell.exe costs ~700ms, which is far too
# slow to do per word, so we start it once and feed it commands on stdin.
# Text arrives base64-encoded so the pipe stays pure ASCII and we never have
# to reason about console code pages.
_PS_WORKER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SetOutputToDefaultAudioDevice()
} catch {
    Write-Output "ERR could not start the speech engine: $($_.Exception.Message)"
    exit 1
}
Write-Output 'READY'

function Decode($s) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s)) }

$running = $true
while ($running) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }

    $space = $line.IndexOf(' ')
    if ($space -lt 0) { $cmd = $line; $arg = '' }
    else { $cmd = $line.Substring(0, $space); $arg = $line.Substring($space + 1) }

    try {
        switch ($cmd) {
            'SAY' {
                $parts = $arg.Split(' ', 2)
                $synth.SpeakAsyncCancelAll()
                $synth.Rate = [int]$parts[0]
                [void]$synth.SpeakAsync((Decode $parts[1]))
            }
            'SAYWAIT' {
                # Synchronous variant: nothing else is read until speech finishes,
                # so a queued EXIT waits politely for the sentence to end.
                $parts = $arg.Split(' ', 2)
                $synth.SpeakAsyncCancelAll()
                $synth.Rate = [int]$parts[0]
                $synth.Speak((Decode $parts[1]))
                Write-Output 'DONE'
            }
            'VOICE' {
                $want = Decode $arg
                $match = $synth.GetInstalledVoices() |
                    Where-Object { $_.Enabled -and $_.VoiceInfo.Name -like "*$want*" } |
                    Select-Object -First 1
                if ($match) {
                    $synth.SelectVoice($match.VoiceInfo.Name)
                    Write-Output "OK using voice: $($match.VoiceInfo.Name)"
                } else {
                    Write-Output "ERR no installed voice matches '$want'"
                }
            }
            'STOP' { $synth.SpeakAsyncCancelAll() }
            'EXIT' { $synth.SpeakAsyncCancelAll(); $running = $false }
            default { Write-Output "ERR unknown command: $cmd" }
        }
    } catch {
        Write-Output "ERR $($_.Exception.Message)"
    }
}
"""

_PS_LIST_VOICES = r"""
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.GetInstalledVoices() | ForEach-Object {
    $i = $_.VoiceInfo
    "{0}  ({1}, {2}){3}" -f $i.Name, $i.Gender, $i.Culture, $(if ($_.Enabled) { '' } else { '  [disabled]' })
}
"""


def _encoded_command(script: str) -> list[str]:
    payload = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", payload]


class SapiSpeaker:
    """Speaks text through the built-in Windows (SAPI) speech engine."""

    def __init__(self, voice: str = "", rate: int = 0) -> None:
        self.voice = voice
        self.rate = rate  # percent change from normal speed, see parse_rate()
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def describe(self) -> str:
        return f"Windows voice ({self.voice or 'default'})"

    # -- process lifecycle -------------------------------------------------

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            _encoded_command(_PS_WORKER),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # All diagnostics come back on stdout as "ERR ..." lines; a redirected
            # stderr only carries PowerShell's CLIXML-wrapped progress noise.
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._drain, args=(self._proc,), daemon=True).start()
        if self.voice:
            self._write(f"VOICE {self._b64(self.voice)}")

    def _drain(self, proc: subprocess.Popen[bytes]) -> None:
        """Read the worker's chatter so its stdout pipe can never fill up."""
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line and line != "READY":
                log(f"speech: {line}")

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if not self._alive():
                self._spawn()

    def stop_process(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._alive():
                assert self._proc is not None
                try:
                    self._write_locked("EXIT")
                    self._proc.wait(timeout=timeout)
                except Exception:
                    self._proc.kill()
            self._proc = None

    # -- protocol ----------------------------------------------------------

    @staticmethod
    def _b64(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    def _write_locked(self, line: str) -> None:
        if not self._alive():
            self._spawn()
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(line.encode("ascii") + b"\r\n")
        self._proc.stdin.flush()

    def _write(self, line: str) -> None:
        try:
            self._write_locked(line)
        except (OSError, ValueError) as exc:
            log(f"speech worker died ({exc}); restarting")
            self._proc = None
            try:
                self._write_locked(line)
            except Exception as exc2:
                log(f"could not restart speech worker: {exc2}")

    # -- public API --------------------------------------------------------

    def say(
        self,
        text: str,
        rate: int | None = None,
        blocking: bool = False,
        respelled: str | None = None,
    ) -> None:
        """Speak text, interrupting anything already being said.

        `respelled` is the overrides.json version of the text, preferred here
        because this engine is the one that needs the help. With blocking=True
        the worker speaks synchronously, so a following stop_process() only
        returns once the utterance has finished.
        """
        spoken = respelled if respelled is not None else text
        if not spoken.strip():
            return
        percent = self.rate if rate is None else int(rate)
        sapi_rate = max(-10, min(10, int(round(percent / 10))))  # SAPI's -10..10 scale
        command = "SAYWAIT" if blocking else "SAY"
        with self._lock:
            self._write(f"{command} {sapi_rate} {self._b64(spoken)}")

    def stop(self) -> None:
        with self._lock:
            if self._alive():
                self._write("STOP")


def list_voices() -> int:
    result = subprocess.run(
        _encoded_command(_PS_LIST_VOICES),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    out = result.stdout.decode("utf-8", "replace").strip()
    if not out:
        print("Could not query the speech engine (System.Speech unavailable?).", file=sys.stderr)
        return result.returncode or 1
    print('Installed Windows voices (backend "sapi"):\n')
    print(out)
    print('\nPut any part of a name in config.json as "voice", e.g. "Zira".')
    print("More voices: Settings > Time & language > Speech > Manage voices.")
    list_edge_voices()
    return 0


def list_edge_voices() -> None:
    try:
        import edge_tts
    except ImportError:
        print('\nEdge neural voices (backend "edge"): edge-tts is not installed - pip install edge-tts')
        return
    import asyncio

    try:
        voices = asyncio.run(edge_tts.list_voices())
    except Exception as exc:
        print(f"\nEdge neural voices: could not reach the service ({exc.__class__.__name__}: {exc})")
        return
    english = sorted(
        (v for v in voices if str(v.get("Locale", "")).startswith("en-")),
        key=lambda v: (v["Locale"], v["ShortName"]),
    )
    print(f'\nEdge neural voices (backend "edge"; English ones of {len(voices)} total):\n')
    for voice in english:
        print(f"  {voice['ShortName']:<36} {voice.get('Gender', ''):<7} {voice['Locale']}")
    print('\nPut a ShortName in config.json as "edge_voice" (or: --set-voice NAME).')
    print('Hear a shortlist of them side by side: --audition "sphygmomanometer"')


# ---------------------------------------------------------------------------
# Speech: Microsoft Edge neural voices (optional, online)
# ---------------------------------------------------------------------------

winmm = ctypes.WinDLL("winmm", use_last_error=True)
winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]
winmm.mciSendStringW.restype = wintypes.DWORD
winmm.mciGetErrorStringW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT]
winmm.mciGetErrorStringW.restype = wintypes.BOOL


def _mci(command: str) -> str:
    """Send one command to winmm's built-in media player and return its reply."""
    reply = ctypes.create_unicode_buffer(256)
    err = winmm.mciSendStringW(command, reply, 255, None)
    if err:
        msg = ctypes.create_unicode_buffer(256)
        winmm.mciGetErrorStringW(err, msg, 255)
        raise RuntimeError(f"MCI error {err}: {msg.value}")
    return reply.value


def play_mp3(path: Path, alias: str, cancelled: Callable[[], bool]) -> None:
    """Play an MP3 and block until it finishes or cancelled() turns true.

    Every MCI call for a clip happens on the calling thread; cancelling is just
    a flag this loop notices, which keeps the device handling single-threaded.
    """
    _mci(f'open "{path}" type mpegvideo alias {alias}')
    try:
        try:
            length_ms = int(_mci(f"status {alias} length"))
        except (RuntimeError, ValueError):
            length_ms = 60_000
        _mci(f"play {alias}")
        started = time.monotonic()
        deadline = started + length_ms / 1000 + 1.5
        while time.monotonic() < deadline and not cancelled():
            # Give the decoder half a second to spin up before "not playing"
            # is taken to mean "finished".
            if _mci(f"status {alias} mode") != "playing" and time.monotonic() - started > 0.5:
                break
            time.sleep(0.05)
    finally:
        try:
            _mci(f"close {alias}")
        except RuntimeError:
            pass


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else APP_DIR) / "pronounce-for-me" / "cache"


def configured_cache_dir(config: dict) -> Path:
    cache_dir = str(config.get("cache_dir", "")).strip()
    return Path(cache_dir) if cache_dir else default_cache_dir()


# Voices worth comparing by ear, women and men; --audition plays a term in each.
AUDITION_VOICES = [
    "en-US-MichelleNeural",
    "en-US-JennyNeural",
    "en-US-AriaNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
]


class EdgeSpeaker:
    """Microsoft Edge's online neural voices (Ava, Andrew, Emma, ...) via edge-tts.

    Synthesis runs off the hotkey thread and the MP3 is cached on disk, so a
    term you've heard before plays instantly and offline. Anything going wrong
    (no network, package missing, the service changing) falls back to the
    built-in Windows voice for that utterance.
    """

    def __init__(self, voice: str, cache_dir: Path | None, fallback: SapiSpeaker) -> None:
        self.voice = voice
        self.rate = fallback.rate
        self.cache_dir = cache_dir
        self.fallback = fallback
        self._edge = None       # the edge_tts module, imported lazily in start()
        self._gen = 0           # bumped by every say()/stop(); stale jobs notice and quit
        self._gen_lock = threading.Lock()

    def describe(self) -> str:
        if self._edge is None:
            return f"{self.fallback.describe()} - edge-tts unavailable"
        where = f", cached in {self.cache_dir}" if self.cache_dir else ""
        return f"{self.voice} via Edge neural TTS (online{where}); fallback: {self.fallback.describe()}"

    def start(self) -> None:
        self.fallback.start()
        if not self.prepare():
            log("edge-tts is not installed (pip install edge-tts); using the Windows voice")

    def prepare(self) -> bool:
        """Import edge-tts and create the cache folder; False if the package is missing.

        start() does this plus the Windows-voice worker; --audition uses it on
        its own, because it only ever needs synthesize().
        """
        try:
            import edge_tts

            self._edge = edge_tts
        except ImportError:
            return False
        if self.cache_dir is not None:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log(f"cannot create cache folder {self.cache_dir} ({exc}); caching off")
                self.cache_dir = None
        return True

    def _bump(self) -> int:
        with self._gen_lock:
            self._gen += 1
            return self._gen

    def say(
        self,
        text: str,
        rate: int | None = None,
        blocking: bool = False,
        respelled: str | None = None,
    ) -> None:
        """Speak `text` with the neural voice; `respelled` is what the Windows
        voice gets if we have to fall back to it."""
        if not text.strip():
            return
        gen = self._bump()
        self.fallback.stop()
        if self._edge is None:
            self.fallback.say(text, rate, blocking=blocking, respelled=respelled)
            return
        if blocking:
            self._job(gen, text, rate, True, respelled)
        else:
            threading.Thread(
                target=self._job, args=(gen, text, rate, False, respelled), daemon=True
            ).start()

    def stop(self) -> None:
        self._bump()
        self.fallback.stop()

    def stop_process(self, timeout: float = 2.0) -> None:
        self._bump()
        self.fallback.stop_process(timeout)

    # -- internals ---------------------------------------------------------

    def _percent(self, rate: int | None) -> int:
        """Clamp a percent rate to what the Edge service accepts."""
        value = self.rate if rate is None else int(rate)
        return max(-50, min(100, value))

    def _job(
        self, gen: int, text: str, rate: int | None, blocking: bool, respelled: str | None
    ) -> None:
        """Fetch the text sentence by sentence and play each clip as soon as it exists.

        A single word is one clip, exactly as before. A paragraph starts playing
        after its first sentence while a helper thread fetches the rest, so the
        wait no longer grows with the length of the selection.
        """
        pct = self._percent(rate)
        chunks = split_sentences(text)
        ready: queue.Queue[tuple[Path | None, Exception | None]] = queue.Queue()

        def fetch() -> None:
            for chunk in chunks:
                if gen != self._gen:
                    ready.put((None, None))  # cancelled; the player is told so it can stop waiting
                    return
                try:
                    ready.put((self._synthesize(chunk, pct), None))
                except Exception as exc:
                    ready.put((None, exc))
                    return

        if len(chunks) > 1:
            log(f"  {len(chunks)} sentences; playing each as it arrives")
            threading.Thread(target=fetch, daemon=True).start()
        else:
            fetch()

        for index in range(len(chunks)):
            path, error = ready.get()
            if gen != self._gen:
                self._discard(path)
                return
            if path is None:
                if error is not None:
                    log(f"Edge voice failed ({error.__class__.__name__}: {error}); using the Windows voice")
                    self._fallback_from(index, chunks, rate, blocking, respelled)
                return
            try:
                play_mp3(path, alias=f"pfm{gen}_{index}", cancelled=lambda: gen != self._gen)
            except RuntimeError as exc:
                log(f"playback failed ({exc}); using the Windows voice")
                if gen == self._gen:
                    self._fallback_from(index, chunks, rate, blocking, respelled)
                return
            finally:
                self._discard(path)

    def _fallback_from(
        self, index: int, chunks: list[str], rate: int | None, blocking: bool, respelled: str | None
    ) -> None:
        """Hand what hasn't been spoken yet to the Windows voice.

        The overrides respelling covers the whole text, so it is only usable
        when nothing has been spoken yet."""
        remaining = " ".join(chunks[index:])
        self.fallback.say(remaining, rate, blocking=blocking, respelled=respelled if index == 0 else None)

    def _discard(self, path: Path | None) -> None:
        """Remove a clip that was synthesized without a cache folder."""
        if path is not None and self.cache_dir is None:
            try:
                path.unlink()
            except OSError:
                pass

    def _cache_path(self, text: str, pct: int) -> Path | None:
        """Where the MP3 for this exact utterance lives on disk (None: caching is off)."""
        if self.cache_dir is None:
            return None
        key = hashlib.sha1(f"{self.voice}|{pct}|{text}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.mp3"

    def is_cached(self, text: str, rate: int | None = None) -> bool:
        target = self._cache_path(text, self._percent(rate))
        return target is not None and target.exists() and target.stat().st_size > 0

    def synthesize(self, text: str, rate: int | None = None) -> Path:
        """Fetch the MP3 for `text` (or find it in the cache) without playing it.

        The synthesis half of say(); --audition uses it to prepare every clip
        before playing any, so the comparison isn't broken up by network waits.
        """
        return self._synthesize(text, self._percent(rate))

    def _synthesize(self, text: str, pct: int) -> Path:
        target = self._cache_path(text, pct)
        if target is None:
            # Unique per clip: with caching off, a paragraph's later sentences are
            # fetched while an earlier one is still playing from its own file.
            handle, name = tempfile.mkstemp(prefix="pronounce-for-me-", suffix=".mp3")
            os.close(handle)
            target = Path(name)
        elif target.exists() and target.stat().st_size > 0:
            return target

        assert self._edge is not None
        partial = target.with_suffix(".part")
        communicate = self._edge.Communicate(
            text, self.voice, rate=f"{pct:+d}%", connect_timeout=6, receive_timeout=25
        )
        communicate.save_sync(str(partial))
        if partial.stat().st_size == 0:
            partial.unlink()
            raise RuntimeError("no audio received")
        os.replace(partial, target)
        return target


def make_speaker(config: dict) -> SapiSpeaker | EdgeSpeaker:
    sapi = SapiSpeaker(voice=str(config.get("voice", "")), rate=parse_rate(config.get("rate", 0)))
    if str(config.get("backend", "sapi")).strip().lower() != "edge":
        return sapi
    return EdgeSpeaker(
        voice=str(config.get("edge_voice") or DEFAULT_CONFIG["edge_voice"]),
        cache_dir=configured_cache_dir(config),
        fallback=sapi,
    )


# ---------------------------------------------------------------------------
# Choosing an Edge voice from the command line
# ---------------------------------------------------------------------------


def voice_first_name(short_name: str) -> str:
    """'en-US-EmmaMultilingualNeural' -> 'Emma': how a voice introduces itself."""
    name = short_name.rsplit("-", 1)[-1].split(":")[0]
    for suffix in ("Neural", "Multilingual"):
        name = name.removesuffix(suffix)
    return name or short_name


def audition(config: dict, term: str, voices: list[str]) -> int:
    """Play `term` in each of `voices`, every clip opening with the voice's own name.

    All clips are synthesized (and cached) first, then played back to back at
    the configured rate, so what you hear is what the hotkey would give you.
    """
    try:
        import edge_tts  # noqa: F401 - only checking; EdgeSpeaker.prepare() imports it for real
    except ImportError:
        print("The audition needs the Edge voices:  pip install edge-tts", file=sys.stderr)
        return 1

    term = clean_text(term, int(config.get("max_chars", 400)))
    if not term:
        print("nothing to say", file=sys.stderr)
        return 1
    overrides = load_overrides() if config.get("use_overrides", True) else {}
    text, _ = texts_for(term, overrides, config.get("use_overrides", True))

    rate = parse_rate(config.get("rate", 0))
    carrier = SapiSpeaker(rate=rate)  # never started; EdgeSpeaker takes its rate from here
    cache_dir = configured_cache_dir(config)
    current = str(config.get("edge_voice") or DEFAULT_CONFIG["edge_voice"])

    print(f'Auditioning "{text}" at rate {rate:+d}% in {len(voices)} voices.\n')
    print("Synthesizing (the first time a voice says this needs the network):")
    clips: list[tuple[str, Path]] = []
    try:
        for name in voices:
            speaker = EdgeSpeaker(voice=name, cache_dir=cache_dir, fallback=carrier)
            speaker.prepare()
            if speaker.cache_dir is None:
                print(f"the audition needs a writable cache folder ({cache_dir}); set cache_dir in config.json",
                      file=sys.stderr)
                return 1
            clip_text = f"{voice_first_name(name)}. {text}."
            cached = speaker.is_cached(clip_text)
            started = time.perf_counter()
            try:
                path = speaker.synthesize(clip_text)
            except Exception as exc:
                print(f"  {name:<32} FAILED ({exc.__class__.__name__}: {exc})")
                continue
            took = time.perf_counter() - started
            print(f"  {name:<32} {'cached' if cached else f'{took:4.1f} s'}", flush=True)
            clips.append((name, path))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1

    if not clips:
        print("\nNo voice could be synthesized. Are you online? Check the names with --list-voices.",
              file=sys.stderr)
        return 1
    if len(clips) < len(voices):
        print("  (a voice that FAILED usually means a misspelt name or no network; see --list-voices)")

    print("\nPlaying (Ctrl+C skips to the summary):")
    try:
        for index, (name, path) in enumerate(clips):
            note = "   <- current default" if name == current else ""
            print(f"  {name}{note}", flush=True)
            try:
                play_mp3(path, alias=f"audition{index}", cancelled=lambda: False)
            except RuntimeError as exc:
                print(f"    playback failed: {exc}")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("  stopped")

    print("\nLiked one? Set its line in config.json and restart the listener:")
    for name, _ in clips:
        print(f'  "edge_voice": "{name}",')
    print("\n  or let the script write it:    python pronounce_for_me.py --set-voice NAME")
    print(f'  Try any voice once, unsaved:   python pronounce_for_me.py --voice NAME --say "{term}"')
    if str(config.get("backend", "sapi")).strip().lower() != "edge":
        print('\nnote: config.json has "backend" set to something other than "edge"; '
              'Edge voices are only used with "backend": "edge".')
    return 0


def set_voice(name: str) -> int:
    """Write `name` into config.json as edge_voice, after checking it really exists."""
    try:
        import edge_tts
    except ImportError:
        print("Checking the voice name needs edge-tts:  pip install edge-tts", file=sys.stderr)
        return 1
    import asyncio

    try:
        voices = asyncio.run(edge_tts.list_voices())
    except Exception as exc:
        print(f"could not fetch the voice list to check the name ({exc.__class__.__name__}: {exc}).\n"
              'Try again when online, or edit "edge_voice" in config.json by hand.', file=sys.stderr)
        return 1

    wanted = name.strip().lower()
    short_names = [str(v.get("ShortName", "")) for v in voices]
    matches = [s for s in short_names if s.lower() == wanted]
    if not matches:  # a bare "Jenny" is fine as long as it's unambiguous
        matches = [s for s in short_names if wanted and wanted in s.lower()]
    if not matches:
        print(f'no Edge voice is called "{name}"; --list-voices shows them all', file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f'"{name}" matches more than one voice - use the full ShortName:', file=sys.stderr)
        for short in sorted(matches):
            print(f"  {short}", file=sys.stderr)
        return 1
    chosen = matches[0]

    settings: dict = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"could not read config.json ({exc}); fix that first", file=sys.stderr)
            return 1
        if not isinstance(settings, dict):
            print("config.json should contain a single {...} object; fix that first", file=sys.stderr)
            return 1
    settings["edge_voice"] = chosen
    try:
        CONFIG_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write config.json ({exc})", file=sys.stderr)
        return 1

    print(f'config.json now says  "edge_voice": "{chosen}"')
    if str(settings.get("backend", "sapi")).strip().lower() != "edge":
        print('note: "backend" is not "edge" in config.json, so this voice is not used until that changes too.')
    print("Restart pronounce-for-me for it to take effect.")
    return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"could not read config.json ({exc}); using defaults")
    else:
        try:
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
            log(f"wrote default config to {CONFIG_PATH.name}")
        except OSError:
            pass
    return config


def claim_single_instance() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\PronounceForMe_SingleInstance")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

HOTKEY_SPEAK = 1
HOTKEY_SLOW = 2
HOTKEY_STOP = 3
HOTKEY_QUIT = 4


class App:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.overrides = load_overrides() if config.get("use_overrides", True) else {}
        self.speaker = make_speaker(config)
        self.registered: list[int] = []

    # -- hotkeys -----------------------------------------------------------

    def register_hotkeys(self) -> bool:
        wanted = [
            (HOTKEY_SPEAK, self.config["hotkey_speak"], "speak selection"),
            (HOTKEY_SLOW, self.config["hotkey_speak_slow"], "speak selection slowly"),
            (HOTKEY_STOP, self.config["hotkey_stop"], "stop speaking"),
            (HOTKEY_QUIT, self.config["hotkey_quit"], "quit"),
        ]
        ok = True
        for hotkey_id, spec, description in wanted:
            spec = str(spec).strip()
            if not spec:
                continue
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as exc:
                log(f"bad hotkey in config.json: {exc}")
                ok = False
                continue
            if user32.RegisterHotKey(None, hotkey_id, mods, vk):
                self.registered.append(hotkey_id)
                log(f"  {spec:<18} {description}")
            else:
                log(f"  {spec:<18} FAILED - another app already owns this combo")
                ok = False
        return ok

    def unregister_hotkeys(self) -> None:
        for hotkey_id in self.registered:
            user32.UnregisterHotKey(None, hotkey_id)
        self.registered.clear()

    # -- actions -----------------------------------------------------------

    def _rate_for(self, term: str) -> int:
        """Normal rate, or the gentler long_term_rate when a word is long enough to trip over."""
        normal = parse_rate(self.config.get("rate", 0))
        raw_long = self.config.get("long_term_rate", 0)
        letters = int(self.config.get("long_term_letters") or 0)
        if raw_long in (None, "", 0, False) or letters <= 0:  # feature switched off
            return normal
        if any(len(word) >= letters for word in re.findall(r"[A-Za-z]+", term)):
            return parse_rate(raw_long)
        return normal

    def speak_selection(self, slow: bool) -> None:
        raw = grab_selection(bool(self.config.get("restore_clipboard", True)))
        if not raw:
            log("nothing highlighted (or the app would not copy)")
            user32.MessageBeep(0xFFFFFFFF)
            return

        term = clean_text(raw, int(self.config.get("max_chars", 400)))
        if not term:
            log("selection had no readable text")
            user32.MessageBeep(0xFFFFFFFF)
            return

        rate = parse_rate(self.config.get("slow_rate", "-30%")) if slow else self._rate_for(term)
        note = ""
        if slow:
            note = " [slow]"
        elif rate != parse_rate(self.config.get("rate", 0)):
            note = " [long term, a touch slower]"

        text, windows_text = texts_for(term, self.overrides, self.config.get("use_overrides", True))
        if text != term:
            log(f'saying: "{term}"  ->  "{text}"{note}')
        elif windows_text != term:
            log(f'saying: "{term}"{note}  (Windows voice would get: "{windows_text}")')
        else:
            log(f'saying: "{term}"{note}')

        self.speaker.say(text, rate=rate, respelled=windows_text)

        if self.config.get("log_history", True):
            record_history(term)

    # -- loop --------------------------------------------------------------

    def run(self) -> int:
        log("pronounce-for-me is listening. Hotkeys:")
        if not self.register_hotkeys():
            log("one or more hotkeys are unavailable - edit config.json and restart")
            if not self.registered:
                return 1

        self.speaker.start()
        log(f"speaking with: {self.speaker.describe()}")
        message = wintypes.MSG()
        try:
            while True:
                # A plain GetMessage would block inside Windows forever and Python
                # would never get to raise KeyboardInterrupt; waking twice a second
                # keeps Ctrl+C in the console working.
                user32.MsgWaitForMultipleObjects(0, None, False, 500, QS_ALLINPUT)
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    if message.message == WM_QUIT:
                        return 0
                    if message.message != WM_HOTKEY:
                        continue
                    try:
                        hotkey_id = message.wParam
                        if hotkey_id == HOTKEY_SPEAK:
                            self.speak_selection(slow=False)
                        elif hotkey_id == HOTKEY_SLOW:
                            self.speak_selection(slow=True)
                        elif hotkey_id == HOTKEY_STOP:
                            self.speaker.stop()
                        elif hotkey_id == HOTKEY_QUIT:
                            log("quitting")
                            return 0
                    except Exception as exc:  # never let one bad word kill the loop
                        log(f"error handling hotkey: {exc}")
        except KeyboardInterrupt:
            log("interrupted")
            return 0
        finally:
            self.unregister_hotkeys()
            self.speaker.stop_process()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pronounce-for-me",
        description="Read the highlighted word aloud with Windows text-to-speech.",
    )
    parser.add_argument("--list-voices", action="store_true", help="show installed speech voices and exit")
    parser.add_argument("--say", metavar="TEXT", help="speak TEXT and exit (no hotkeys)")
    parser.add_argument("--test", action="store_true", help="speak a sample medical term and exit")
    parser.add_argument("--no-overrides", action="store_true", help="ignore overrides.json for this run")
    parser.add_argument(
        "--voice",
        metavar="NAME",
        help='voice for this run: an Edge ShortName such as en-US-JennyNeural (backend "edge") '
        'or part of a Windows voice name such as Zira (backend "sapi"); see --list-voices',
    )
    parser.add_argument(
        "--audition",
        metavar="TERM",
        help="play TERM in a shortlist of Edge voices, each saying its own name first, and exit",
    )
    parser.add_argument(
        "--voices",
        metavar="A,B,C",
        help="comma-separated Edge ShortNames for --audition (default: " + ",".join(AUDITION_VOICES) + ")",
    )
    parser.add_argument(
        "--set-voice",
        metavar="NAME",
        help='check NAME against the Edge voice list, save it to config.json as "edge_voice", and exit',
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("pronounce-for-me is Windows-only.", file=sys.stderr)
        return 1

    if args.list_voices:
        return list_voices()
    if args.set_voice:
        return set_voice(args.set_voice)
    if args.voices and not args.audition:
        parser.error("--voices only makes sense together with --audition")

    config = load_config()
    if args.no_overrides:
        config["use_overrides"] = False

    if args.audition:
        voices = [v.strip() for v in (args.voices or "").split(",") if v.strip()] or list(AUDITION_VOICES)
        return audition(config, args.audition, voices)

    if args.voice:
        is_edge = str(config.get("backend", "sapi")).strip().lower() == "edge"
        config["edge_voice" if is_edge else "voice"] = args.voice

    if args.say or args.test:
        text = args.say or "Syncope secondary to orthostatic hypotension."
        overrides = load_overrides() if config.get("use_overrides", True) else {}
        speaker = make_speaker(config)
        speaker.start()
        print(f"speaking with: {speaker.describe()}")
        term = clean_text(text, int(config.get("max_chars", 400)))
        neural_text, windows_text = texts_for(term, overrides, config.get("use_overrides", True))
        extra = f'  (Windows voice would get: "{windows_text}")' if windows_text != neural_text else ""
        print(f'speaking: "{neural_text}"{extra}')
        speaker.say(neural_text, blocking=True, respelled=windows_text)
        speaker.stop_process(timeout=300)  # EXIT is only read once speech ends
        return 0

    if not claim_single_instance():
        log("pronounce-for-me is already running; this copy is exiting so the hotkeys keep working")
        return 0

    return App(config).run()


if __name__ == "__main__":
    sys.exit(main())
