from pathlib import Path
import tempfile
import threading
import time
import unittest

from taildrop import LocalAPI, TaildropError, TaildropManager
from taildrop_fixture import TaildropFixture


class TaildropTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="taildrop-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = TaildropFixture(self.root)
        self.addCleanup(self.fixture.close)
        self.client = LocalAPI(self.fixture.socket_path)
        self.addCleanup(self.client.close)
        self.destination = self.root / "received"
        self.notices = []

    def manager(self, auto_receive=True):
        manager = TaildropManager(str(self.fixture.binary), self.destination, auto_receive,
                                  lambda items: self.notices.extend(items), client=self.client)
        self.addCleanup(manager.close)
        return manager

    def wait_for(self, condition):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("transfer did not reach expected state")

    def source(self, name="说明.txt", data=b"test bytes\x00\xff"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_receive_unicode_bytes_and_acknowledge(self):
        name = "中文 #文件\n说明.txt"
        self.fixture.files[name] = b"hello\x00\xff" * 100000
        manager = self.manager()
        manager.poll()
        self.assertEqual((self.destination / name).read_bytes(), b"hello\x00\xff" * 100000)
        self.assertEqual(self.fixture.files, {})
        self.assertEqual(self.fixture.deletes, [name])
        self.assertEqual(self.notices[0]["name"], name)
        self.assertFalse(manager.snapshot()["error"])

    def test_same_name_and_symlink_are_never_overwritten(self):
        self.destination.mkdir()
        original = self.source("original.txt", b"keep me")
        (self.destination / "data.txt").symlink_to(original)
        (self.destination / "data (1).txt").write_bytes(b"also keep")
        self.fixture.files["data.txt"] = b"new content"
        self.manager().poll()
        self.assertEqual(original.read_bytes(), b"keep me")
        self.assertEqual((self.destination / "data (1).txt").read_bytes(), b"also keep")
        self.assertEqual((self.destination / "data (2).txt").read_bytes(), b"new content")

    def test_interrupted_receive_keeps_inbox_and_cleans_partial_file(self):
        self.fixture.files["broken.txt"] = b"partial"
        self.fixture.truncated.add("broken.txt")
        manager = self.manager()
        manager.poll()
        self.assertIn("broken.txt", self.fixture.files)
        self.assertFalse((self.destination / "broken.txt").exists())
        self.assertFalse(self.fixture.deletes)
        self.assertEqual(manager.snapshot()["transfers"][0]["status"], "failed")
        self.fixture.truncated.clear()
        manager.poll()
        self.assertEqual((self.destination / "broken.txt").read_bytes(), b"partial")
        self.assertEqual(len(manager.snapshot()["transfers"]), 1)
        self.assertEqual(manager.snapshot()["transfers"][0]["status"], "done")

    def test_failed_acknowledgement_retries_without_saving_duplicate(self):
        self.fixture.files["note.txt"] = b"note"
        self.fixture.fail_delete.add("note.txt")
        manager = self.manager()
        manager.poll()
        self.assertEqual((self.destination / "note.txt").read_bytes(), b"note")
        self.assertTrue(manager.snapshot()["error"])
        manager.poll()
        self.assertEqual(self.fixture.downloads, ["note.txt"])
        self.assertEqual(len(list(self.destination.iterdir())), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertFalse(manager.snapshot()["error"])

    def test_manual_receive_when_automatic_is_disabled(self):
        self.fixture.files["manual.txt"] = b"manual"
        manager = self.manager(auto_receive=False)
        manager.poll()
        self.assertEqual(len(manager.snapshot()["pending"]), 1)
        self.assertFalse(self.destination.exists())
        manager.poll(receive=True)
        self.assertEqual((self.destination / "manual.txt").read_bytes(), b"manual")

    def test_unsafe_names_do_not_escape_receive_directory(self):
        for name in ("../outside", "a/b", "a\\b", ".", "..", "", "a\0b"):
            with self.subTest(name=name), self.assertRaises(TaildropError):
                self.client.receive(name, self.destination, threading.Event())
        self.assertFalse(self.destination.exists())

    def test_stopped_receive_does_not_acknowledge_or_keep_partial(self):
        self.fixture.files["stop.txt"] = b"data"
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(TaildropError):
            self.client.receive("stop.txt", self.destination, stopped)
        self.assertFalse(list(self.destination.iterdir()))
        self.assertIn("stop.txt", self.fixture.files)

    def test_permission_error_is_visible(self):
        self.fixture.denied = True
        manager = self.manager()
        manager.poll()
        self.assertIn("权限", manager.snapshot()["error"])
        self.assertFalse(manager.snapshot()["targets"])

    def test_send_uses_official_cli_and_preserves_bytes(self):
        manager = self.manager(auto_receive=False)
        manager.poll()
        source = self.source("--中文 ' file.txt")
        manager.send([source], "100.64.0.2")
        self.wait_for(lambda: manager.snapshot()["transfers"][0]["status"] == "done")
        self.assertEqual((self.fixture.delivered / source.name).read_bytes(), source.read_bytes())

    def test_only_available_personal_targets_are_allowed(self):
        manager = self.manager(auto_receive=False)
        manager.poll()
        source = self.source()
        for ip in ("100.64.0.3", "--help", "example.com"):
            with self.assertRaises(TaildropError):
                manager.send([source], ip)
        self.fixture.targets[0]["Node"]["Online"] = False
        manager.poll()
        with self.assertRaises(TaildropError):
            manager.send([source], "100.64.0.2")
        self.assertFalse(self.fixture.log.exists())

    def test_invalid_file_batch_does_not_partially_start(self):
        manager = self.manager(auto_receive=False)
        manager.poll()
        with self.assertRaises(FileNotFoundError):
            manager.send([self.source(), self.root / "missing"], "100.64.0.2")
        self.assertFalse(manager.snapshot()["transfers"])
        with self.assertRaises(TaildropError):
            manager.send([self.root], "100.64.0.2")

    def test_cli_failure_is_reported(self):
        manager = self.manager(auto_receive=False)
        manager.poll()
        manager.send([self.source("fail.txt")], "100.64.0.2")
        self.wait_for(lambda: manager.snapshot()["transfers"][0]["status"] == "failed")
        self.assertIn("simulated transfer failure", manager.snapshot()["transfers"][0]["error"])

    def test_running_and_queued_transfers_can_be_cancelled(self):
        manager = self.manager(auto_receive=False)
        manager.poll()
        manager.send([self.source("slow.txt"), self.source("queued.txt")], "100.64.0.2")
        self.wait_for(self.fixture.log.exists)
        records = manager.snapshot()["transfers"]
        for record in records:
            manager.cancel(record["id"])
        self.wait_for(lambda: all(item["status"] == "cancelled" for item in manager.snapshot()["transfers"]))
        self.assertFalse(list(self.fixture.delivered.iterdir()))
        self.assertEqual(len(self.fixture.log.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
