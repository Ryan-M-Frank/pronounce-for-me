# macOS command-line preview

Status: experimental; not yet verified on a physical Mac. This milestone adds
speech and auditions, not a background listener or global selection hotkeys.
Windows hotkeys remain supported. Linux is not yet supported.

## Setup

Install Python 3.10+ and download or clone the entire repository, then in Terminal:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install edge-tts
python pronounce_for_me.py --say "sphygmomanometer"
python pronounce_for_me.py --audition "sphygmomanometer" --voices en-US-JennyNeural,en-US-AriaNeural,en-US-GuyNeural
python pronounce_for_me.py --set-voice en-US-JennyNeural
```

For copied text, copy it in any application and run:

```sh
python pronounce_for_me.py --clipboard
```

This reads the existing clipboard without changing it. It does not automatically
copy a selection. No Accessibility permission is needed by this CLI preview.
Stop playback with Ctrl+C. Voice names and text are passed without shell evaluation.

For offline speech:

```sh
python pronounce_for_me.py --backend native --say "Hello from my Mac"
python pronounce_for_me.py --list-voices
```

Use `--backend native --voice NAME --say "term"` with an installed Apple voice.
Apple's rate is approximated from 175 words per minute; it will not exactly match
Edge's percentage setting. Windows-only respellings are not automatically sent
to Apple voices. Setting `use_overrides` to `"always"` opts into them explicitly.

## Files and privacy

Settings: `~/Library/Application Support/pronounce-for-me/config.json`.
Audio cache: `~/Library/Caches/pronounce-for-me/`.
History, if enabled: `~/Library/Application Support/pronounce-for-me/history.tsv`.
Bundled pronunciation overrides still come from the repository's `overrides.json`.
New installations default to Jenny and history logging off. Existing Windows
settings are retained. Edge sends spoken text to Microsoft's online service;
the native backend runs locally. Cached Edge audio can be replayed offline.

## Automated checks and real-Mac acceptance checklist

CI runs on Windows and macOS with Python 3.10, 3.13 and 3.14. A macOS-only
test invokes the real system voice and verifies the generated audio file. It
does not verify audible playback, clipboard access in desktop apps, or sound
quality. Those still require the checks below.

- [ ] Record macOS version, CPU (Apple Silicon/Intel), and Python version.
- [ ] Help, voice listing and default settings creation succeed.
- [ ] Edge speaks a new word; cached repetition works offline.
- [ ] Audition labels and audio follow the requested voice order.
- [ ] Voice selection persists, preserving unrelated settings.
- [ ] Native speech works offline and Ctrl+C stops speech.
- [ ] Copy from Preview, Anki, Safari/Chrome and Word, then use --clipboard.
- [ ] Empty clipboard is handled; clipboard contents remain unchanged.
- [ ] Multi-sentence speech starts and completes, and cancellation stops playback.
- [ ] Test headphones and the system's selected output device.

## Next milestones

1. Mac global shortcuts and selection capture, permission onboarding, clipboard
   restoration that respects newer copies, and a single-instance listener.
2. Menu-bar settings and voice audition UI; opt-in launch at login.
3. Mac builds in CI, then packaged releases with Developer ID signing and
   notarization. Confirm architecture and macOS-version support through testing.
4. Compare against Apple's built-in Speak Selection with classmates before
   calling the Mac app ready for general use.

No Piper, pocket-tts, cache pre-warming, or additional speech engines are planned
for this milestone. Pronunciation quality must be assessed by listeners; no
voice is guaranteed to pronounce every medical term correctly.
