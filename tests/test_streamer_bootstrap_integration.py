import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from risk import mitm
from tests.test_streamer_mode import backend


class BootstrapIntegrationTest(unittest.TestCase):
    def test_response_preserves_login_headers_and_recalculates_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "ssjj-toolbox" / "streamer_mode.json"
            config.parent.mkdir()
            config.write_text('{"enabled":true,"lobby_overlay":true}', encoding="utf8")
            old = "Lobby_" + "a" * 32 + ".swf"
            data = b"variant"
            variant = "Lobby_%s.swf" % hashlib.md5(data).hexdigest()
            (root / variant).write_bytes(data)
            (root / "streamer_lobby_experiment.json").write_text(json.dumps(
                {"original": old, "variant": variant, "config_path": str(config)}), encoding="utf8")
            body = ('{"lobbyFileName":"%s","key":"other"}' % old).encode()
            headers = [("Content-Type", "text/html"), ("Set-Cookie", "session=test"),
                       ("ETag", '"stale"'), ("Content-Length", str(len(body)))]
            server = SimpleNamespace(
                CACHE=str(root), _slk=threading.Lock(),
                STATS={"miss": 0, "bytes": 0}, MAX_CACHE=100000,
                path_to_file=lambda _: None, dyn_get=lambda _: None,
                dyn_get_stale=lambda _: None, dyn_put=Mock(), log=Mock())
            tls = SimpleNamespace(sendall=Mock())
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}), \
                 patch.object(mitm, "DATA_ROOT", str(root)), \
                 patch.object(mitm, "_fetch_upstream_full", return_value=(200, "OK", headers, body)), \
                 patch.object(mitm, "record"):
                mitm._serve_request(server, tls, b"GET /s10/lobby_truly.php?v=x HTTP/1.1\r\n\r\n", "host")
                response = tls.sendall.call_args.args[0]
                head, result = response.split(b"\r\n\r\n", 1)
                self.assertEqual(result, body.replace(old.encode(), variant.encode()))
                self.assertIn(b"Set-Cookie: session=test", head)
                self.assertIn(("Content-Length: %d" % len(result)).encode(), head)
                self.assertNotIn(b"ETag:", head)
                server.log.assert_any_call("STREAMER-BOOTSTRAP rewritten")

                config.write_text('{"enabled":false}', encoding="utf8")
                mitm._serve_request(server, tls, b"GET /s10/lobby_truly.php?v=y HTTP/1.1\r\n\r\n", "host")
                head, result = tls.sendall.call_args.args[0].split(b"\r\n\r\n", 1)
                self.assertEqual(result, body)
                self.assertIn(b'ETag: "stale"', head)

    def test_startup_preparation_follows_saved_settings_and_handles_failure(self):
        with patch.object(backend, "load_app_settings", return_value={}), \
             patch.object(backend, "risk_config", return_value={}), \
             patch.object(backend, "skin_load_rules", return_value={}), \
             patch.object(backend, "streamer_load_config") as config, \
             patch.object(backend, "streamer_lobby_experiment") as prepare:
            config.return_value = {"enabled": True, "lobby_overlay": True}
            backend.restore_on_startup()
            prepare.assert_called_once_with(True)
            prepare.reset_mock()
            config.return_value = {"enabled": False, "lobby_overlay": True}
            backend.restore_on_startup()
            prepare.assert_not_called()
            config.return_value = {"enabled": True, "lobby_overlay": False}
            backend.restore_on_startup()
            prepare.assert_not_called()
            config.return_value = {"enabled": True, "lobby_overlay": True}
            prepare.side_effect = ValueError("test failure")
            self.assertTrue(any("准备失败" in line for line in backend.restore_on_startup()))


if __name__ == "__main__":
    unittest.main()
