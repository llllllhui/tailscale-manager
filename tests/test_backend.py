import json
import unittest
from unittest.mock import patch

import server


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tailscale = patch.object(server, "TAILSCALE", "/usr/bin/tailscale")
        self.tailscale.start()
        self.addCleanup(self.tailscale.stop)

    def test_state_handles_exit_node_selected_by_id_and_missing_fields(self):
        raw = {"BackendState": "Running", "TailscaleIPs": ["fd7a::1", "100.64.0.1"],
               "Peer": {"one": {"HostName": "exit", "TailscaleIPs": ["100.64.0.2"],
                                "Online": True, "ExitNode": True, "ExitNodeOption": True},
                        "two": {"HostName": "offline"}}}
        with patch.object(server, "run", side_effect=[(0, json.dumps(raw), ""),
                                                      (1, "", "access denied")]):
            state = server.build_state()
        self.assertTrue(state["ok"])
        self.assertEqual(state["exitNodeIP"], "100.64.0.2")
        self.assertEqual(state["self"]["ipv4"], "100.64.0.1")
        self.assertEqual(state["peers"][1]["name"], "offline")

    def test_permission_error_has_actionable_hint(self):
        with patch.object(server, "run", return_value=(1, "", "Access denied")):
            result = server.action("/api/down")
        self.assertTrue(result["needOperator"])
        self.assertIn("set --operator=$USER", result["hint"])

    def test_login_timeout_returns_browser_url(self):
        with patch.object(server, "run", return_value=(1, "", "timeout")), patch.object(
                server, "build_state", return_value={"authURL": "https://login.tailscale.com/a/test"}):
            result = server.action("/api/login")
        self.assertTrue(result["ok"])
        self.assertTrue(result["url"].startswith("https://"))

    def test_invalid_targets_never_reach_cli(self):
        with patch.object(server, "run") as run:
            for path in ("/api/ping", "/api/exit-node"):
                for target in ("--help", "example.com", 42, [], "100.999.0.1"):
                    self.assertFalse(server.action(path, {"ip": target})["ok"])
            self.assertFalse(server.action("/api/ping", {"ip": ""})["ok"])
            self.assertFalse(server.action("/api/down", ["invalid"])["ok"])
        run.assert_not_called()

    def test_exit_cancel_preserves_other_preferences(self):
        with patch.object(server, "run", return_value=(0, "", "")) as run:
            self.assertTrue(server.action("/api/exit-node", {"ip": ""})["ok"])
        self.assertEqual(run.call_args.args[0], ["set", "--exit-node="])

    def test_ping_result_and_unknown_action(self):
        with patch.object(server, "run", return_value=(0, "pong from mac in 12ms", "")):
            self.assertEqual(server.action("/api/ping", {"ip": "100.64.0.2"})["ms"], "12ms")
        self.assertFalse(server.action("/api/unknown")["ok"])

    def test_daemon_unavailable_and_corrupt_status(self):
        for result in ((1, "", "daemon unavailable"), (0, "not json", "")):
            with patch.object(server, "run", return_value=result):
                self.assertFalse(server.build_state()["ok"])

    def test_missing_cli(self):
        with patch.object(server, "TAILSCALE", None):
            self.assertFalse(server.action("/api/up")["ok"])


if __name__ == "__main__":
    unittest.main()
