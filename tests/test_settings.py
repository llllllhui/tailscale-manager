import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import desktop_settings as settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary.name})
        env.start()
        self.addCleanup(env.stop)

    def test_corrupt_or_missing_config_is_recoverable(self):
        self.assertEqual(settings.load_settings(), {})
        settings.save_settings({"width": 900, "height": 700})
        self.assertEqual(settings.load_settings()["height"], 700)
        path = self.directory / "tailscale-manager/settings.json"
        for data in ("bad", "[]", "null"):
            path.write_text(data)
            self.assertEqual(settings.load_settings(), {})

    def test_autostart_migration_is_reversible_and_idempotent(self):
        folder = self.directory / "autostart"
        folder.mkdir()
        legacy = folder / "tailscale-systray.desktop"
        original = "[Desktop Entry]\nExec=/usr/bin/tailscale systray --theme light\n"
        legacy.write_text(original)
        self.assertFalse(settings.autostart_enabled())
        for _ in range(2):
            settings.set_autostart(True, '/home/example/My Apps/tailscale-manager')
            self.assertTrue(settings.autostart_enabled())
        self.assertIn("Hidden=true", legacy.read_text())
        self.assertEqual(legacy.with_suffix(".desktop.before-manager").read_text(), original)
        desktop = (folder / "tailscale-manager.desktop").read_text()
        self.assertIn('Exec="/home/example/My Apps/tailscale-manager" --background', desktop)
        settings.set_autostart(False, "unused")
        self.assertFalse(settings.autostart_enabled())

    def test_exec_escaping_preserves_literal_paths(self):
        quoted = settings.desktop_quote('/tmp/a$`"%\\b')
        self.assertEqual(quoted, '"/tmp/a\\$\\`\\"%%\\\\b"')


if __name__ == "__main__":
    unittest.main()
