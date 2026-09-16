# Contributing

Small fixes, pronunciation reports and Mac testing are welcome. Open an issue
before proposing a new speech engine or a large interface change.

Use Python 3.10+ in a virtual environment. Install `edge-tts` for online speech.
Run `python -m unittest discover -s tests -v` before submitting a pull request.
Keep Windows behavior compatible, and keep native imports in platform modules.
The main script is the shared CLI, text processing, cache and Edge speech layer;
`speech_common.py` contains shared utilities. Platform modules own playback,
local speech and (on Windows) hotkeys and clipboard capture.

Please report your OS/version, Python version, voice, command or shortcut,
expected behavior and actual result. Remove private selected text from logs.
Use the Mac checklist in `docs/MACOS.md` for real-device testing. Passing mocked
tests or CI does not certify audio output or permissions on a user's desktop.

This project is MIT licensed. Contributions must be compatible with that license.
Speech providers and dependencies retain their own licenses and service terms.
