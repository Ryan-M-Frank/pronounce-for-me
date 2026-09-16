# pronounce-for-me

MIT-licensed pronunciation helper. **Windows:** global hotkeys. **macOS:**
experimental command-line speech and voice auditions; no global hotkeys yet.
See [Mac setup and tester checklist](docs/MACOS.md) and [contributing](CONTRIBUTING.md).
Download the entire repository: the Python entry point now uses shared and
platform-specific modules.


Highlight a word anywhere in Windows, press **Ctrl+Alt+P**, and hear it read
aloud. Made for drilling medical terminology while reading PDFs, slides, Anki
cards or web pages.

Choose a speech engine in `config.json` or for one run with `--backend`:

| `backend` | Voice | Needs |
|---|---|---|
| `"edge"` (default) | Microsoft Edge neural voices; Jenny by default | internet for new clips + `pip install edge-tts` |
| `"native"` | Windows SAPI or the Mac's installed voices | no Python packages |
| `"sapi"` / `"macos"` | Platform-specific aliases for `native` | Windows / macOS respectively |

If Edge cannot synthesize speech, the app tries the platform's built-in voice.
Pronunciation varies by voice and term; audition difficult words before relying
on a voice for study. Mac support is an unverified command-line preview.

## Windows setup

1. **Python 3.10+** — [python.org](https://www.python.org/downloads/windows/),
   the Python install manager, or `winget install Python.Python.3.13`.
   (The portable *embeddable* zip works too, unzipped into a `python\`
   folder next to the script — but it has no `pip`, so it can only use the
   built-in voice.)
2. **For the neural voices:** `pip install edge-tts`
3. Run it:

| How | What you get |
|---|---|
| `start.bat` | console window showing every word it hears |
| `start_hidden.vbs` | no window at all |
| `python pronounce_for_me.py` | same as `start.bat` |

Put a shortcut to `start_hidden.vbs` in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` to have it running
at every login.

### Handing it to a classmate

Copy the folder, do steps 1–2, run `start.bat`. To pick a voice by ear
rather than by name:

```
python pronounce_for_me.py --audition "sphygmomanometer"
```

plays the word in ten Edge voices, women and men, each introducing itself
first ("Jenny. Sphygmomanometer."), shows how long each took to produce, and
ends by printing the `config.json` line for every one of them. Save the favourite
without opening an editor:

```
python pronounce_for_me.py --set-voice en-US-JennyNeural
```

(the name is checked against the live voice list first; restart the listener
afterwards). `--voices en-US-AriaNeural,en-GB-RyanNeural` auditions a
shortlist of your own, `--list-voices` prints every option, and `--voice`
tries one without saving anything:

```
python pronounce_for_me.py --voice en-US-JennyNeural --say "sphygmomanometer"
```

## Windows hotkeys

| Keys | Action |
|---|---|
| **Ctrl+Alt+P** | speak the highlighted text |
| **Ctrl+Alt+O** | speak it slowly |
| **Ctrl+Alt+S** | stop talking |
| **Ctrl+Alt+Q** | quit |

All four can be changed in `config.json`. Words of 12+ letters are
automatically read a touch slower than short ones (see `long_term_rate`).

## Configuration

`config.json` is created next to the script on Windows; Mac settings live in
`~/Library/Application Support/pronounce-for-me/`. The defaults are:

```json
{
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
  "restore_clipboard": true,
  "log_history": false,
  "use_overrides": true
}
```

* `backend` — `"edge"` or `"native"`; `"sapi"` and `"macos"` are platform-specific aliases.
* `edge_voice` — an Edge voice's *ShortName*. Good English ones, roughly by
  how fast the service produces them — women: `en-US-JennyNeural` and
  `en-US-AriaNeural` (~1 s per word), `en-US-MichelleNeural` (~1.5 s),
  `en-US-EmmaMultilingualNeural` / `en-US-AvaMultilingualNeural` (~3.5 s);
  men: `en-US-GuyNeural` (~1 s), `en-US-AndrewMultilingualNeural` /
  `en-US-BrianMultilingualNeural` (~2 s). British: `en-GB-SoniaNeural`,
  `en-GB-RyanNeural`. `--audition "term"` plays a term in all ten of these
  so you can choose by ear, and `--set-voice NAME` saves the choice.
* `voice` — for the `sapi` backend (and the fallback): any part of a Windows
  voice name, e.g. `"Zira"`. Empty = Windows default.
* `rate`, `slow_rate`, `long_term_rate` — percent change from normal speed,
  e.g. `"-5%"`, `"+20%"`. `long_term_rate` applies automatically whenever a
  word has at least `long_term_letters` letters; set it to `""` to turn that
  off. (Bare numbers are also accepted as the old SAPI −10…10 scale.)
* `cache_dir` — where Edge audio is kept; empty means
  `%LOCALAPPDATA%\pronounce-for-me\cache`.
* `max_chars` — longer selections are cut off so a mis-click can't read you a
  whole chapter.
* `restore_clipboard` — the selection is grabbed by simulating Ctrl+C; with
  this on, whatever text was on your clipboard beforehand is put back.
* `log_history` — append every term you look up to `history.tsv` (timestamp +
  term) when using the Windows listener. Off by default; opt in if you want a study log.
* `use_overrides` — see below.

Hotkey syntax: modifiers joined with `+`, then a key — `ctrl`, `alt`, `shift`,
`win`; letters, digits, `f1`–`f24`, `space`, `home`, punctuation, etc.

## Fixing mispronunciations

The Windows voice can mispronounce Latin and Greek terms. `overrides.json` maps a
term (or phrase) to a phonetic respelling it *does* read correctly:

```json
{
  "syncope": "sing-kuh-pee",
  "myasthenia gravis": "my-us-thee-nee-uh grav-is"
}
```

Matching is case-insensitive and possessives match too (`Raynaud's` hits
`raynaud`). If the whole selection matches an entry that respelling is used;
otherwise each matching word inside the selection is swapped.

By default (`"use_overrides": true`) respellings are fed **only to the Windows
voice** — neural voices may handle the original spelling better and can read
`sing-kuh-pee` literally. `"always"` sends them to both engines (useful for
an eponym the neural voice also gets wrong); `false` disables them. Tune by
ear with `--say`.

## Speed, cache and privacy

* The first time you hear a term the Edge service has to produce it: about
  0.5–2 s depending on the voice. The MP3 is then cached (6–15 KB per term), so
  repeats are instant and work offline. Longer selections are cached one
  sentence at a time, so a paragraph starts playing after its first sentence.
* With the `edge` backend the highlighted text is sent to Microsoft's servers.
  Use `"backend": "native"` for local speech instead.
* Edge's read-aloud service is undocumented; Microsoft has changed it before.
  If every term suddenly falls back to the Windows voice, run
  `pip install -U edge-tts`.

### What about the Windows 11 "natural" voices?

Windows 11 can install high-quality Narrator voices (e.g. *Microsoft Ava HD*),
but Windows exposes them **only to Narrator** — they're invisible to SAPI and
to the modern WinRT speech API alike, so no script can use them. The Edge
voices are the same voice family, just served online. (The third-party
[NaturalVoiceSAPIAdapter](https://github.com/gexgd0419/NaturalVoiceSAPIAdapter)
can unlock them offline, but it needs admin rights to install and, per its
README, no longer works with the newest voice packs.)

## How it works on Windows

1. `RegisterHotKey` registers the four combos system-wide; the script sits in
   a Windows message loop waiting for `WM_HOTKEY`.
2. On the speak hotkey it waits for you to release the keys, sends a
   synthetic **Ctrl+C** with `SendInput`, and watches the clipboard sequence
   number until the source app has written the selection.
3. The text is tidied — PDF line-wrap hyphens re-joined, `[12]` citation
   markers removed, whitespace collapsed.
4. **edge:** `edge-tts` fetches an MP3 (cached on disk), which is played
   through Windows' own `winmm` MCI player — no audio library needed. A
   selection of several sentences is fetched one sentence at a time and each
   clip plays as soon as it exists, so a paragraph starts after its first
   sentence rather than after the whole thing has downloaded.
   **sapi:** the text goes to a single long-lived PowerShell process hosting
   `System.Speech.Synthesis.SpeechSynthesizer`; starting PowerShell costs
   ~0.7 s, so it's started once and fed commands over stdin.
   Either way a new word interrupts the previous one and **Stop** is instant.

Everything except `edge-tts` is the Python standard library talking to
Windows through `ctypes`.

## Troubleshooting

* **"FAILED - another app already owns this combo"** — pick a different combo
  in `config.json`. Ctrl+Alt+letter is also AltGr+letter on some non-US
  keyboard layouts.
* **Beeps instead of speaking** — nothing was highlighted, or the app refused
  the copy. Some PDF readers block copying in protected documents; web pages
  occasionally do too.
* **"Edge voice failed … using the Windows voice"** — no internet, a proxy
  in the way, or the service changed (`pip install -U edge-tts`).
* **Nothing happens at all** — run `start.bat` instead of the `.vbs` and read
  the console. Running two copies is harmless; the second one exits.

## Roadmap

* Mac selection hotkeys and permission setup; see [the Mac milestones](docs/MACOS.md).
* Menu-bar controls, signed Mac releases, and real-device acceptance testing.
* A hotkey to cycle through a shortlist of voices.

## License and support

[MIT](LICENSE). New installations use Jenny and do not record study history unless
`log_history` is enabled. Existing configuration is preserved. The code is open
source; Microsoft's online voices are an external service, not bundled open-source
models. Edge sends selected text to that service. Pronunciation varies by voice
and term. Mac CLI support is a preview awaiting real-device testing.
