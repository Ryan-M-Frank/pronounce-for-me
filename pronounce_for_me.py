#!/usr/bin/env python3
"""
pronounce-for-me
================

Read selected text with Windows hotkeys, or use the macOS command-line preview.
Shared Edge speech uses native playback and a platform-specific offline fallback.

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
from datetime import datetime
from pathlib import Path

from speech_common import log, parse_rate

if sys.platform == "win32":
    from platform_windows import (
        NativeSpeaker, Listener, beep, grab_selection, play_mp3,
        claim_single_instance, list_native_voices,
    )
else:
    # No native imports here, so unsupported systems can still display --help.
    # main() rejects other actions before reaching platform-specific commands.
    from platform_macos import (
        NativeSpeaker, Listener, beep, grab_selection, play_mp3,
        claim_single_instance, list_native_voices,
    )

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = (Path.home() / "Library" / "Application Support" / "pronounce-for-me"
            if sys.platform == "darwin" else APP_DIR)
CONFIG_PATH = DATA_DIR / "config.json"
OVERRIDES_PATH = APP_DIR / "overrides.json"
HISTORY_PATH = DATA_DIR / "history.tsv"

DEFAULT_CONFIG = {
    "hotkey_speak": "ctrl+alt+p",
    "hotkey_speak_slow": "ctrl+alt+o",
    "hotkey_stop": "ctrl+alt+s",
    "hotkey_quit": "ctrl+alt+q",
    "backend": "edge",
    "edge_voice": "en-US-JennyNeural",
    "cache_dir": "",
    "voice": "",
    "rate": "-5%",
    "slow_rate": "-30%",
    "long_term_rate": "-10%",
    "long_term_letters": 12,
    "max_chars": 400,
    "restore_clipboard": True,
    "log_history": False,
    "use_overrides": True,
}


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
    if not isinstance(raw, dict):
        log("overrides.json must contain a JSON object; continuing without it")
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

def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pronounce-for-me"
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
    platform's built-in voice for that utterance.
    """

    def __init__(self, voice: str, cache_dir: Path | None, fallback: NativeSpeaker) -> None:
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
            log("edge-tts is not installed (pip install edge-tts); using the built-in voice")

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
                    log(f"Edge voice failed ({error.__class__.__name__}: {error}); using the built-in voice")
                    self._fallback_from(index, chunks, rate, blocking, respelled)
                return
            try:
                play_mp3(path, alias=f"pfm{gen}_{index}", cancelled=lambda: gen != self._gen)
            except (OSError, RuntimeError) as exc:
                log(f"playback failed ({exc}); using the built-in voice")
                if gen == self._gen:
                    self._fallback_from(index, chunks, rate, blocking, respelled)
                return
            finally:
                self._discard(path)

    def _fallback_from(
        self, index: int, chunks: list[str], rate: int | None, blocking: bool, respelled: str | None
    ) -> None:
        """Hand what hasn't been spoken yet to the built-in voice.

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


def make_speaker(config: dict) -> NativeSpeaker | EdgeSpeaker:
    backend = str(config.get("backend", "edge")).strip().lower()
    allowed = {"edge", "native", "sapi" if sys.platform == "win32" else "macos"}
    if backend not in allowed:
        raise ValueError(f"Unsupported backend {backend!r} on {sys.platform}; use edge or native")
    native = NativeSpeaker(voice=str(config.get("voice", "")), rate=parse_rate(config.get("rate", 0)))
    if backend != "edge":
        return native
    return EdgeSpeaker(
        voice=str(config.get("edge_voice") or DEFAULT_CONFIG["edge_voice"]),
        cache_dir=configured_cache_dir(config),
        fallback=native,
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
    carrier = NativeSpeaker(rate=rate)  # never started; EdgeSpeaker takes its rate from here
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
            except (OSError, RuntimeError) as exc:
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
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write config.json ({exc})", file=sys.stderr)
        return 1

    print(f'config.json now says  "edge_voice": "{chosen}"')
    if str(settings.get("backend", "sapi")).strip().lower() != "edge":
        print('note: "backend" is not "edge" in config.json, so this voice is not used until that changes too.')
    print(f"Saved to {CONFIG_PATH}")
    print("Future commands use this voice. Restart any running listener to apply it.")
    return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("expected a JSON object")
            config.update(saved)
        except (OSError, ValueError) as exc:
            log(f"could not read config.json ({exc}); using defaults")
    else:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
            log(f"wrote default config to {CONFIG_PATH.name}")
        except OSError:
            pass
    return config


class App(Listener):
    def __init__(self, config: dict) -> None:
        self.config = config
        self.overrides = load_overrides() if config.get("use_overrides", True) else {}
        self.speaker = make_speaker(config)
        self.registered: list[int] = []

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
            beep()
            return

        term = clean_text(raw, int(self.config.get("max_chars", 400)))
        if not term:
            log("selection had no readable text")
            beep()
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
            log(f'saying: "{term}"{note}  (built-in voice would get: "{windows_text}")')
        else:
            log(f'saying: "{term}"{note}')

        self.speaker.say(text, rate=rate, respelled=windows_text)

        if self.config.get("log_history", False):
            record_history(term)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pronounce-for-me",
        description="Read text aloud on Windows or macOS (command-line preview).",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list-voices", action="store_true", help="show installed speech voices and exit")
    actions.add_argument("--say", metavar="TEXT", help="speak TEXT and exit (no hotkeys)")
    parser.add_argument("--backend", choices=("edge", "native", "sapi", "macos"), help="speech engine for this run")
    actions.add_argument("--clipboard", action="store_true", help="read copied text once (macOS preview)")
    actions.add_argument("--test", action="store_true", help="speak a sample medical term and exit")
    parser.add_argument("--no-overrides", action="store_true", help="ignore overrides.json for this run")
    parser.add_argument(
        "--voice",
        metavar="NAME",
        help='voice for this run: an Edge ShortName such as en-US-JennyNeural (backend "edge") '
        'or a built-in voice name (backend "native"); see --list-voices',
    )
    actions.add_argument(
        "--audition",
        metavar="TERM",
        help="play TERM in a shortlist of Edge voices, each saying its own name first, and exit",
    )
    parser.add_argument(
        "--voices",
        metavar="A,B,C",
        help="comma-separated Edge ShortNames for --audition (default: " + ",".join(AUDITION_VOICES) + ")",
    )
    actions.add_argument(
        "--set-voice",
        metavar="NAME",
        help='check NAME against the Edge voice list, save it to config.json as "edge_voice", and exit',
    )
    args = parser.parse_args(argv)

    if sys.platform not in ("win32", "darwin"):
        print("Supported platforms: Windows and macOS (command-line preview).", file=sys.stderr)
        return 1

    if args.voices is not None and args.audition is None:
        parser.error("--voices only makes sense together with --audition")
    if args.list_voices:
        try:
            result = list_native_voices()
        except OSError as exc:
            print(f"Could not list built-in voices: {exc}", file=sys.stderr)
            result = 1
        list_edge_voices()
        return result
    if args.set_voice is not None:
        return set_voice(args.set_voice)

    config = load_config()
    if args.backend:
        config["backend"] = args.backend
    backend = str(config.get("backend", "edge")).strip().lower()
    if backend not in {"edge", "native", "sapi" if sys.platform == "win32" else "macos"}:
        parser.error(f"backend {backend!r} is unavailable on this platform; use edge or native")
    try:
        parse_rate(config.get("rate", 0))
        if int(config.get("max_chars", 400)) < 1:
            raise ValueError("max_chars must be positive")
    except (ValueError, TypeError, OverflowError) as exc:
        parser.error(f"invalid rate or max_chars in {CONFIG_PATH}: {exc}")
    if args.clipboard:
        if sys.platform != "darwin":
            parser.error("--clipboard is currently available in the macOS preview")
        try:
            args.say = grab_selection(False)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Could not read the clipboard: {exc}", file=sys.stderr)
            return 1
        if not args.say or not args.say.strip():
            print("The clipboard contains no text.", file=sys.stderr)
            return 1
    if args.no_overrides:
        config["use_overrides"] = False

    if args.audition is not None:
        voices = (list(AUDITION_VOICES) if args.voices is None else
                  [v.strip() for v in args.voices.split(",") if v.strip()])
        if not voices:
            parser.error("--voices must contain at least one voice name")
        return audition(config, args.audition, voices)

    if args.voice:
        is_edge = str(config.get("backend", "sapi")).strip().lower() == "edge"
        config["edge_voice" if is_edge else "voice"] = args.voice

    if args.say is not None or args.test:
        text = args.say if args.say is not None else "Syncope secondary to orthostatic hypotension."
        term = clean_text(text, int(config.get("max_chars", 400)))
        if not term:
            print("Nothing to say.", file=sys.stderr)
            return 1
        overrides = load_overrides() if config.get("use_overrides", True) else {}
        speaker = make_speaker(config)
        neural_text, windows_text = texts_for(term, overrides, config.get("use_overrides", True))
        extra = (f'  (Windows fallback would get: "{windows_text}")'
                 if sys.platform == "win32" and windows_text != neural_text else "")
        try:
            speaker.start()
            print(f"speaking with: {speaker.describe()}")
            print(f'speaking: "{neural_text}"{extra}')
            speaker.say(neural_text, blocking=True, respelled=windows_text)
            speaker.stop_process(timeout=300)  # Let the Windows worker finish.
        except KeyboardInterrupt:
            speaker.stop()
            speaker.stop_process()
            return 130
        except (OSError, RuntimeError) as exc:
            speaker.stop_process()
            print(f"Speech failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if sys.platform == "darwin":
        return App(config).run()

    if not claim_single_instance():
        log("pronounce-for-me is already running; this copy is exiting so the hotkeys keep working")
        return 0

    return App(config).run()


if __name__ == "__main__":
    sys.exit(main())
