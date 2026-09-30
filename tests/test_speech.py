"""Offline regression checks. These do not certify audible output on a Mac."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import pronounce_for_me as app
import platform_macos as mac


class SharedTests(unittest.TestCase):
    def test_mac_import_does_not_load_windows(self):
        result = subprocess.run([sys.executable, "-B", "-c",
            "import sys; sys.platform='darwin'; import pronounce_for_me as p; "
            "assert 'platform_windows' not in sys.modules; "
            "assert 'Library' in str(p.CONFIG_PATH); "
            "assert p.NativeSpeaker.__module__ == 'platform_macos'; "
            "assert p.main(['--help']) is None"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_config_preserved_and_new_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "config.json"
            with patch.object(app, "CONFIG_PATH", path):
                cfg = app.load_config()
                self.assertFalse(cfg["log_history"])
                self.assertEqual(cfg["edge_voice"], "en-US-JennyNeural")
                old = '{"edge_voice": "en-US-GuyNeural", "log_history": true}'
                path.write_text(old)
                cfg = app.load_config()
                self.assertTrue(cfg["log_history"])
                self.assertEqual(cfg["edge_voice"], "en-US-GuyNeural")
                self.assertEqual(path.read_text(), old)

    def test_cached_audio_skips_network_and_plays(self):
        with tempfile.TemporaryDirectory() as folder:
            fallback = Mock(rate=-5)
            speaker = app.EdgeSpeaker('en-US-JennyNeural', Path(folder), fallback)
            speaker._edge = Mock()
            target = speaker._cache_path('syncope', -5)
            target.write_bytes(b'cached audio')
            with patch.object(app, 'play_mp3') as play:
                speaker.say('syncope', blocking=True)
            speaker._edge.Communicate.assert_not_called()
            self.assertEqual(play.call_args.args[0], target)

    def test_audition_finishes_synthesis_before_playback(self):
        events = []
        speaker = Mock(cache_dir=Path('.'))
        speaker.is_cached.return_value = False
        speaker.synthesize.side_effect = lambda text: events.append('synth') or Path('clip.mp3')
        with patch.dict(sys.modules, {'edge_tts': Mock()}), \
             patch.object(app, 'EdgeSpeaker', return_value=speaker), \
             patch.object(app, 'load_overrides', return_value={}), \
             patch.object(app.time, 'sleep'), \
             patch.object(app, 'play_mp3', side_effect=lambda *a, **k: events.append('play')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.audition(app.DEFAULT_CONFIG, 'syncope', ['en-US-JennyNeural', 'en-US-GuyNeural']), 0)
        self.assertEqual(events, ['synth', 'synth', 'play', 'play'])

    def test_cli_interrupt_cleans_up(self):
        speaker = Mock()
        speaker.say.side_effect = KeyboardInterrupt
        with patch.object(app, 'load_config', return_value=dict(app.DEFAULT_CONFIG)), \
             patch.object(app, 'make_speaker', return_value=speaker), \
             patch.object(app, 'load_overrides', return_value={}), \
             patch.object(app, 'claim_single_instance', side_effect=AssertionError('CLI claimed mutex')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--say', 'syncope']), 130)
        speaker.stop.assert_called_once()
        speaker.stop_process.assert_called_once()

    def test_mac_no_listener_or_mutex(self):
        with patch.object(app.sys, 'platform', 'darwin'), \
             patch.object(app, 'load_config', return_value=dict(app.DEFAULT_CONFIG)), \
             patch.object(app, 'App') as listener, \
             patch.object(app, 'claim_single_instance', side_effect=AssertionError):
            listener.return_value.run.return_value = 1
            self.assertEqual(app.main([]), 1)


class MacTests(unittest.TestCase):
    def test_native_text_is_stdin_and_sapi_overrides_ignored(self):
        proc = Mock()
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        with patch.object(mac.subprocess, 'Popen', return_value=proc) as spawn:
            voice = mac.NativeSpeaker('Samantha', -5)
            text = 'syncope; $(not-a-command)'
            voice.say(text, blocking=True, respelled='SAPI only')
        self.assertEqual(spawn.call_args.args[0], ['/usr/bin/say', '-r', '166', '-v', 'Samantha'])
        proc.stdin.write.assert_called_once_with(text.encode())
        proc.stdin.close.assert_called_once()

    def test_cancel_player_terminates_and_reaps(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.return_value = -15
        with patch.object(mac.subprocess, 'Popen', return_value=proc):
            mac.play_mp3(Path('clip.mp3'), 'unused', Mock(side_effect=[False, True]))
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2)

    def test_playback_error_reported(self):
        proc = Mock(returncode=1)
        proc.poll.return_value = 1
        with patch.object(mac.subprocess, 'Popen', return_value=proc):
            with self.assertRaises(RuntimeError):
                mac.play_mp3(Path('clip.mp3'), '', lambda: False)

    def test_precancel_does_not_launch(self):
        with patch.object(mac.subprocess, 'Popen') as spawn:
            mac.play_mp3(Path('clip.mp3'), '', lambda: True)
        spawn.assert_not_called()

    def test_clipboard_read_only(self):
        with patch.object(mac.subprocess, 'run', return_value=Mock(stdout=b'syncope')) as run:
            self.assertEqual(mac.grab_selection(), 'syncope')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pbpaste'])

class CliValidationTests(unittest.TestCase):
    def test_empty_text_does_not_start_speaker(self):
        for text in ('', '  ', '...'):
            with self.subTest(text=text), \
                 patch.object(app, 'load_config', return_value=dict(app.DEFAULT_CONFIG)), \
                 patch.object(app, 'make_speaker') as make, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(['--say', text]), 1)
                make.assert_not_called()

    def test_conflicting_actions_rejected_before_config(self):
        with patch.object(app, 'load_config') as load, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as err:
            app.main(['--say', '', '--clipboard'])
        self.assertEqual(err.exception.code, 2)
        load.assert_not_called()

    def test_startup_failure_releases_speaker(self):
        speaker = Mock()
        speaker.start.side_effect = OSError('missing speech executable')
        with patch.object(app, 'load_config', return_value=dict(app.DEFAULT_CONFIG)), \
             patch.object(app, 'make_speaker', return_value=speaker), \
             patch.object(app, 'load_overrides', return_value={}), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(['--say', 'syncope']), 1)
        speaker.stop_process.assert_called_once()

    def test_malformed_config_kept_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            for content in ('[]', 'null', '{broken'):
                path.write_text(content)
                with patch.object(app, 'CONFIG_PATH', path):
                    self.assertEqual(app.load_config(), app.DEFAULT_CONFIG)
                self.assertEqual(path.read_text(), content)

    def test_mac_clipboard_command_passes_text_to_speaker(self):
        speaker = Mock()
        with patch.object(app.sys, 'platform', 'darwin'), \
             patch.object(app, 'load_config', return_value=dict(app.DEFAULT_CONFIG)), \
             patch.object(app, 'grab_selection', return_value='Copied text') as copy, \
             patch.object(app, 'make_speaker', return_value=speaker), \
             patch.object(app, 'load_overrides', return_value={}), \
             patch.object(app, 'claim_single_instance', side_effect=AssertionError), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--clipboard']), 0)
        copy.assert_called_once_with(False)
        speaker.say.assert_called_once_with('Copied text', blocking=True, respelled='Copied text')

    def test_empty_audition_does_not_start_listener(self):
        with patch.object(app, 'load_config', return_value=dict(app.DEFAULT_CONFIG)), \
             patch.dict(sys.modules, {'edge_tts': Mock()}), \
             patch.object(app, 'claim_single_instance', side_effect=AssertionError), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(['--audition', '']), 1)

    def test_set_voice_creates_mac_settings_directory(self):
        async def list_voices():
            return [{'ShortName': 'en-US-JennyNeural'}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'Application Support' / 'pronounce-for-me' / 'config.json'
            with patch.object(app, 'CONFIG_PATH', path), \
                 patch.dict(sys.modules, {'edge_tts': Mock(list_voices=list_voices)}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.set_voice('Jenny'), 0)
            self.assertEqual(json.loads(path.read_text())['edge_voice'], 'en-US-JennyNeural')

    def test_invalid_voice_does_not_change_settings(self):
        async def list_voices():
            return [{'ShortName': 'en-US-JennyNeural'}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('{"edge_voice":"existing","custom":42}')
            before = path.read_bytes()
            with patch.object(app, 'CONFIG_PATH', path), \
                 patch.dict(sys.modules, {'edge_tts': Mock(list_voices=list_voices)}), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.set_voice('no-such-voice'), 1)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_audio_player_falls_back(self):
        fallback = Mock(rate=0)
        speaker = app.EdgeSpeaker('en-US-JennyNeural', Path('.'), fallback)
        speaker._edge = Mock()
        with patch.object(speaker, '_synthesize', return_value=Path('clip.mp3')), \
             patch.object(app, 'play_mp3', side_effect=FileNotFoundError('afplay')), \
             contextlib.redirect_stdout(io.StringIO()):
            speaker.say('syncope', blocking=True)
        fallback.say.assert_called_once_with('syncope', None, blocking=True, respelled=None)


@unittest.skipUnless(sys.platform == 'darwin', 'requires actual macOS say executable')
class MacNativeIntegrationTests(unittest.TestCase):
    def test_native_speech_generates_audio_file(self):
        # Exercise the real say process, voice listing and UTF-8 stdin without
        # requiring an audio device in CI. Only redirect its output to a file.
        listing = subprocess.run(['/usr/bin/say', '-v', '?'], capture_output=True, check=True)
        self.assertTrue(listing.stdout.strip())
        spawn = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'speech.aiff'
            def to_file(command, **kwargs):
                return spawn(command + ['-o', str(output)], **kwargs)
            voice = mac.NativeSpeaker()
            try:
                with patch.object(mac.subprocess, 'Popen', side_effect=to_file):
                    voice.say('Hello. This is a pronunciation test.', blocking=True)
                data = output.read_bytes()
                self.assertEqual(data[:4], b'FORM')
                self.assertIn(data[8:12], (b'AIFF', b'AIFC'))
                self.assertGreater(len(data), 1000)
            finally:
                voice.stop()


class ReviewRegressionTests(unittest.TestCase):
    def test_clipboard_preserves_accents_under_non_utf8_parent_locale(self):
        text = 'M\u00e9ni\u00e8re and Barr\u00e9'
        for parent in ({}, {'LC_ALL': 'C', 'LC_CTYPE': 'C', 'LANG': 'C'}):
            with self.subTest(parent=parent), patch.dict(mac.os.environ, parent, clear=True), \
                 patch.object(mac.subprocess, 'run', return_value=Mock(stdout=text.encode('utf-8'))) as run:
                self.assertEqual(mac.grab_selection(), text)
                self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'en_US.UTF-8')
                self.assertEqual(dict(mac.os.environ), parent)

    def test_partial_configuration_uses_current_defaults_without_rewriting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            original = '{"backend":"edge","rate":"+15%"}'
            path.write_text(original)
            with patch.object(app, 'CONFIG_PATH', path):
                cfg = app.load_config()
            self.assertEqual(cfg['rate'], '+15%')
            self.assertEqual(cfg['edge_voice'], 'en-US-JennyNeural')
            self.assertFalse(cfg['log_history'])
            self.assertEqual(path.read_text(), original)

    def test_unknown_windows_backend_reports_error_instead_of_silent_fallback(self):
        with patch.object(app.sys, 'platform', 'win32'), \
             patch.object(app, 'load_config', return_value={**app.DEFAULT_CONFIG, 'backend': 'windows'}), \
             patch.object(app, 'make_speaker') as make, \
             contextlib.redirect_stderr(io.StringIO()) as errors, \
             self.assertRaises(SystemExit) as result:
            app.main(['--say', 'syncope'])
        self.assertEqual(result.exception.code, 2)
        self.assertIn('use edge or native', errors.getvalue())
        make.assert_not_called()

    @unittest.skipUnless(
        sys.platform == 'darwin' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted',
        'uses only the disposable hosted Mac runner clipboard',
    )
    def test_real_mac_clipboard_roundtrip_with_c_locale(self):
        text = 'M\u00e9ni\u00e8re and Barr\u00e9'
        utf8_env = dict(os.environ, LC_ALL='en_US.UTF-8')
        try:
            subprocess.run(['/usr/bin/pbcopy'], input=text.encode('utf-8'),
                           env=utf8_env, check=True)
            with patch.dict(mac.os.environ, {'LC_ALL': 'C', 'LC_CTYPE': 'C', 'LANG': 'C'}):
                self.assertEqual(mac.grab_selection(), text)
        finally:
            subprocess.run(['/usr/bin/pbcopy'], input=b'', env=utf8_env, check=True)


if __name__ == '__main__':
    unittest.main()
