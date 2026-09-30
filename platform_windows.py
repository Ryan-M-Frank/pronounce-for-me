"""Windows hotkeys, clipboard, SAPI and MCI playback."""
from __future__ import annotations
import base64
import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from collections.abc import Callable
from speech_common import log

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


def list_native_voices() -> int:
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
    return 0


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


def claim_single_instance() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\PronounceForMe_SingleInstance")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


HOTKEY_SPEAK = 1
HOTKEY_SLOW = 2
HOTKEY_STOP = 3
HOTKEY_QUIT = 4


class Listener:
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



def beep():
    user32.MessageBeep(0xFFFFFFFF)

NativeSpeaker = SapiSpeaker
