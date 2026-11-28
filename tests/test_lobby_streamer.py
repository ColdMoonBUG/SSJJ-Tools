import bz2
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import swf_fps
from risk import lobby_streamer


def number(value):
    return (abs(value) | ((1 << 63) if value < 0 else 0)).to_bytes(8, "little")


def delta(control, diff, extra, size):
    control = bz2.compress(b"".join(number(value) for triple in control for value in triple))
    diff = bz2.compress(diff)
    return b"BSDIFF40" + number(len(control)) + number(len(diff)) + number(size) + control + diff + bz2.compress(extra)


class LobbyStreamerDeltaTest(unittest.TestCase):
    def test_delta_handles_extra_and_negative_source_offset(self):
        patch_bytes = delta([(2, 1, -4), (3, 0, 0)], b"\x00\x01\x01\x02\x03", b"!", 6)
        self.assertEqual(lobby_streamer._apply_delta(b"abc", patch_bytes), b"ac!\x01\x02d")

    def test_invalid_delta_is_rejected(self):
        with self.assertRaises(ValueError):
            lobby_streamer._apply_delta(b"x", delta([(2, 0, 0)], b"\0", b"", 2))
        with self.assertRaises(ValueError):
            lobby_streamer._apply_delta(b"x", b"not a patch")

    def test_unknown_lobby_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.swf"
            source.write_bytes(swf_fps.encode(b"unrecognized", b"FWS"))
            with self.assertRaisesRegex(ValueError, "大厅版本不匹配"):
                lobby_streamer.prepare_variant(source, root / "result", root / "config.json")
            self.assertFalse((root / "result").exists())

    def test_template_hash_and_config_path_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.swf"
            source.write_bytes(swf_fps.encode(b"known body", b"FWS"))
            with patch.object(lobby_streamer, "_BASE_SHA256",
                              __import__("hashlib").sha256(b"known body").hexdigest()), \
                 patch.object(lobby_streamer, "_apply_delta", return_value=b"wrong"):
                with self.assertRaisesRegex(ValueError, "补丁校验失败"):
                    lobby_streamer.prepare_variant(source, root / "out", root / "config.json")
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                lobby_streamer.prepare_variant(source, root / "out", "relative.json")

    def test_install_and_restore_preserve_other_settings_and_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paras, record = root / "paras.dat", root / "record.json"
            paras.write_bytes(b'\xef\xbb\xbf{"lobbyFileName":"Lobby_old.swf","serverId":7}')
            variant = root / "Lobby_new.swf"
            with patch.object(lobby_streamer, "prepare_variant", return_value=variant):
                result = lobby_streamer.install(paras, root, record, root / "config.json")
            self.assertEqual(result["variant"], variant.name)
            self.assertTrue(paras.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(json.loads(paras.read_text(encoding="utf-8-sig")),
                             {"lobbyFileName": "Lobby_old.swf", "serverId": 7})
            self.assertTrue(lobby_streamer.restore(paras, record))
            self.assertEqual(json.loads(paras.read_text(encoding="utf-8-sig"))["lobbyFileName"], "Lobby_old.swf")
            self.assertFalse(record.exists())

    def test_restore_never_overwrites_another_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paras, record = root / "paras.dat", root / "record.json"
            paras.write_text('{"lobbyFileName":"different.swf"}', encoding="utf8")
            record.write_text('{"original":"old.swf","variant":"experiment.swf"}', encoding="utf8")
            with self.assertRaisesRegex(ValueError, "其他操作修改"):
                lobby_streamer.restore(paras, record)
            self.assertEqual(json.loads(paras.read_text())["lobbyFileName"], "different.swf")
            self.assertTrue(record.exists())

    def test_launcher_reset_reuses_valid_prepared_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paras, record = root / "paras.dat", root / "record.json"
            paras.write_text('{"lobbyFileName":"old.swf","serverId":7}', encoding="utf8")
            data = b"existing-variant"
            variant = "Lobby_%s.swf" % __import__("hashlib").md5(data).hexdigest()
            (root / variant).write_bytes(data)
            config = root / "config.json"
            record.write_text(json.dumps({"original": "old.swf", "variant": variant,
                                          "config_path": str(config)}), encoding="utf8")
            result = lobby_streamer.install(paras, root, record, config)
            self.assertTrue(result["already_active"])
            self.assertTrue(lobby_streamer.restore(paras, record))
            self.assertEqual(json.loads(paras.read_text())["serverId"], 7)
            self.assertFalse(record.exists())

    def test_install_does_not_overwrite_change_during_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paras, record = root / "paras.dat", root / "record.json"
            paras.write_text('{"lobbyFileName":"old.swf"}', encoding="utf8")

            def concurrent_change(*_args):
                paras.write_text('{"lobbyFileName":"different.swf"}', encoding="utf8")
                return root / "experiment.swf"

            with patch.object(lobby_streamer, "prepare_variant", side_effect=concurrent_change):
                with self.assertRaisesRegex(ValueError, "配置发生变化"):
                    lobby_streamer.install(paras, root, record, root / "config.json")
            self.assertFalse(record.exists())
            self.assertEqual(json.loads(paras.read_text())["lobbyFileName"], "different.swf")

    def test_login_page_changes_only_one_matching_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = "Lobby_" + "a" * 32 + ".swf"
            blob = b"test variant"
            variant = "Lobby_%s.swf" % __import__("hashlib").md5(blob).hexdigest()
            (root / variant).write_bytes(blob)
            config, record = root / "config.json", root / "record.json"
            config.write_text('{"enabled":true,"lobby_overlay":true}', encoding="utf8")
            record.write_text(json.dumps({"original": original, "variant": variant,
                                          "config_path": str(config)}), encoding="utf8")
            body = ('{"lobbyFileName":"%s","other":"%s"}' % (original, original)).encode()
            rewrite = lambda p, b, h=(): lobby_streamer.rewrite_bootstrap(p, b, h, record, root, config)
            result, state = rewrite("/s10/lobby_truly.php?v=test", body)
            self.assertEqual(state, "rewritten")
            self.assertEqual(result.count(variant.encode()), 1)
            self.assertEqual(result.count(original.encode()), 1)
            self.assertEqual(rewrite("/s10/other.php", body), (body, "not-target"))
            self.assertEqual(rewrite("/s10/lobby_truly.php", body, [("Content-Encoding", "gzip")]),
                             (body, "encoded-response"))
            doubled = body + body
            self.assertEqual(rewrite("/s10/lobby_truly.php", doubled), (doubled, "field-count-2"))
            another = b'{"lobbyFileName":"Lobby_' + b'b' * 32 + b'.swf"}'
            self.assertEqual(rewrite("/s10/lobby_truly.php", body + another),
                             (body + another, "field-count-2"))
            self.assertEqual(rewrite("/s10/lobby_truly.php", another), (another, "unexpected-lobby"))
            config.write_text('{"enabled":false}', encoding="utf8")
            self.assertEqual(rewrite("/s10/lobby_truly.php", body), (body, "disabled"))

    def test_encoded_and_javascript_field_syntax(self):
        old = b"Lobby_" + b"a" * 32 + b".swf"
        for body in (b"lobbyFileName='" + old + b"'", b'%22lobbyFileName%22%3A%22' + old,
                     b'\\"lobbyFileName\\":\\"' + old):
            match = lobby_streamer._BOOTSTRAP_FIELD.search(body)
            self.assertIsNotNone(match, body)
            self.assertEqual(match.group(1), old)


if __name__ == "__main__":
    unittest.main()
