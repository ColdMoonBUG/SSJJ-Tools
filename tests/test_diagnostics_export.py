import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolbox"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
from risk import proxy


class DiagnosticExportTest(unittest.TestCase):
    def test_session_append_redaction_and_residual_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            game = root / "game"
            battle = game / "battle" / "5_64"
            battle.mkdir(parents=True)
            (battle / ".ver.dat").write_bytes(b"proxy bytes")
            log = root / "risk_inject.log"
            log.write_text("older session\n", encoding="utf-8")
            baseline = diagnostics._stat(log)
            with log.open("a", encoding="utf-8") as f:
                f.write('event password="hunter2" username=alice '
                        'url=http://example.test/login.php?token=secret&server=5\n')
                f.write("Cookie: sid=veryprivate; other=something\n")
                f.write("traceback line one\n  continued line\n")
            session = root / "session.log"
            session.write_text("startup\n", encoding="utf-8")
            output = root / "report.txt"
            with patch.object(diagnostics, "_START", time.time() - 10), \
                 patch.object(diagnostics, "_LOG", session), \
                 patch.object(diagnostics, "_BASELINE", {str(log.resolve()): baseline}), \
                 patch.object(diagnostics, "_sources", return_value={str(log.resolve()): log}), \
                 patch.object(diagnostics, "_game_dir", return_value=game), \
                 patch.object(diagnostics, "_ROOT", root), \
                 patch.object(diagnostics, "_windows_events", return_value="Faulting module: example.dll"), \
                 patch.object(diagnostics, "_windows_integrity", return_value="VBS status=2"), \
                 patch.object(diagnostics, "_probe_av_and_mitigations", return_value=["[本机杀软与安全事件]", "  AV: mock"]):
                self.assertEqual(diagnostics.export(output), str(output.resolve()))
            text = output.read_text(encoding="utf-8")
            self.assertIn("Faulting module: example.dll", text)
            self.assertIn("=== DLL 加载策略与安全环境探针 ===", text)
            self.assertIn("traceback line one\n  continued line", text)
            self.assertIn(".ver.dat", text)
            self.assertNotIn("older session", text)
            for secret in ("hunter2", "alice", "secret", "veryprivate", "other=something"):
                self.assertNotIn(secret, text)

    def test_redact_myeasylog2_escaped_json_and_preserves_config_keys(self):
        line_escaped = (
            r'launch args: "wd.ssjj.4399://lobby?{\"isRpg2\":\"0\",\"serverId\":\"3\",'
            r'\"username\":\"3212608853\",\"sign\":\"0f30adb92b00b71e292606341afc4837\",'
            r'\"fcmClientIp\":\"198.51.100.10\",\"fcmSign\":\"ec5c570c9f8ca17747a4560127eedffa\",'
            r'\"signConfigFileName\":\"sign_ca3909761ae4bd9d39e8a234cc42fa63.xml\",'
            r'\"globalSignConfigFileName\":\"sign_cd48d5b936f4358ec18eb7b3edf7e544.xml\",'
            r'\"testClientSignConfigFileName\":\"\",'
            r'\"cdnIp\":\"120.221.107.158\",'
            r'\"extParams\":\"%7B%5C%22isMicroClient%5C%22%3A1%2C%5C%22clientIp%5C%22%3A%5C%22203.0.113.42%5C%22%2C%5C%22deviceInfo%5C%22%3A%5C%22AA%3ABB%3ACC%3ADD%3AEE%3AFF%5C%22%7D\"}&isUnityLobby=1"'
        )
        line_plain = (
            'Launcher la: {"isRpg2":"0","serverId":"3","username":"3212608853",'
            '"sign":"0f30adb92b00b71e292606341afc4837","fcmClientIp":"198.51.100.10",'
            '"clientIp":"203.0.113.43","cdnIp":"120.221.107.158",'
            '"fcmSign":"ec5c570c9f8ca17747a4560127eedffa",'
            '"signConfigFileName":"sign_ca3909761ae4bd9d39e8a234cc42fa63.xml",'
            '"extParams":"%7B%5C%22clientIp%5C%22%3A%5C%22203.0.113.44%5C%22%2C%5C%22deviceInfo%5C%22%3A%5C%22AA%3ABB%3ACC%3ADD%3AEE%3AFF%5C%22%7D"}'
        )
        out = diagnostics.redact(line_escaped + "\n" + line_plain)
        for secret in (
            "3212608853",
            "0f30adb92b00b71e292606341afc4837",
            "ec5c570c9f8ca17747a4560127eedffa",
            "198.51.100.10",
            "203.0.113.42",
            "203.0.113.43",
            "203.0.113.44",
            "AA%3ABB%3ACC%3ADD%3AEE%3AFF",
        ):
            self.assertNotIn(secret, out)
        self.assertIn(r'\"username\":\"[REDACTED]\"', out)
        self.assertIn(r'\"sign\":\"[REDACTED]\"', out)
        self.assertIn(r'\"fcmSign\":\"[REDACTED]\"', out)
        self.assertIn(r'\"fcmClientIp\":\"[REDACTED]\"', out)
        self.assertIn('"clientIp":"[REDACTED]"', out)
        self.assertIn('%5C%22clientIp%5C%22%3A%5C%22[REDACTED]%5C%22', out)
        self.assertIn('"cdnIp":"120.221.107.158"', out)
        self.assertIn('"username":"[REDACTED]"', out)
        self.assertIn('"sign":"[REDACTED]"', out)
        self.assertIn('signConfigFileName', out)
        self.assertIn('globalSignConfigFileName', out)
        self.assertIn('testClientSignConfigFileName', out)
        self.assertIn('sign_ca3909761ae4bd9d39e8a234cc42fa63.xml', out)
        self.assertIn(r'\"serverId\":\"3\"', out)

    def test_redact_uname_battle_sign_and_custom_protocol_urls(self):
        sample = "\n".join([
            "[1125/210752:ERROR:GameGuardManager.cpp(362)] NP send user name error: id:7,uname:3212608853,type:1,gcid:0",
            "21:08:48,205 INFO GameBootConfig: ServerId: 3, Sign: e3c993e8754765201741739a312c268a, SignFileName: , Timestamp: 1795612115122",
            "Connect Tcp, RoomId 123 UserId 456 Sign 0123456789abcdef0123456789abcdef LoginType Unity",
            "assign=keepme design=keep2 resign=keep3",
            "proto1=wd.ssjj.4399://lobby?username=alice&sign=secret_sig&serverId=3",
            "proto2=wd.ssjj.4399://lobby?%7B%22username%22%3A%22bob_encoded%22%2C%22sign%22%3A%22deadbeef01234567%22%2C%22signConfigFileName%22%3A%22keep.xml%22%7D",
            "encoded=foo%3Fusername%3Dcharlie_enc%26sign%3Dcafe0123456789ab%26server%3D3",
        ])
        out = diagnostics.redact(sample)
        for secret in (
            "3212608853",
            "e3c993e8754765201741739a312c268a",
            "0123456789abcdef0123456789abcdef",
            "alice",
            "secret_sig",
            "bob_encoded",
            "deadbeef01234567",
            "charlie_enc",
            "cafe0123456789ab",
        ):
            self.assertNotIn(secret, out)
        self.assertIn("uname:[REDACTED],type:1", out)
        self.assertIn("Sign: [REDACTED], SignFileName: ,", out)
        self.assertIn("Sign [REDACTED] LoginType Unity", out)
        self.assertIn("assign=keepme design=keep2 resign=keep3", out)
        self.assertIn("signConfigFileName%22%3A%22keep.xml", out)

    def test_redact_empty_values_do_not_corrupt_following_fields(self):
        sample = (
            r'\"username\":\"\",\"testClientSignConfigFileName\":\"\",\"serverId\":\"3\"'
            '\n'
            'url=https://ptlogin.4399.com/login?username=&next=ok'
        )
        out = diagnostics.redact(sample)
        self.assertIn(r'\"username\":\"\",\"testClientSignConfigFileName\":\"\",\"serverId\":\"3\"', out)
        self.assertIn("next=%5BREDACTED%5D", out)

    def test_redact_cli_la_and_json_org_base64_payloads(self):
        b64 = "ChtodHRwOi8vY2RuLjQzOTlzc2pqLmNvbS9AzOS8SFXFkMTk5LnNzamouYWJkMDA3LmNvbRiNDiCjDyj25afyq9BM"
        sample = "\n".join([
            f"Run From Command SSJJ_BattleClient_Unity.exe -npopen 1 --la {b64} --ua wd",
            f"Launcher la: -window-mode borderless --la {b64} --ua wd",
            "22:12:33,613 INFO  GameController - GameBootConfig: --la",
            f"22:12:33,613 INFO  GameController - GameBootConfig: {b64}",
            "22:12:33,613 INFO  GameController - GameBootConfig: --ua",
            f"22:12:33,616 INFO  GameController - json org:{b64}",
            "22:12:33,613 INFO  GameController - GameBootConfig: -configmd5 4e7e6b4b42b0d83472d8dae65a13a13c",
        ])
        out = diagnostics.redact(sample)
        self.assertNotIn(b64, out)
        self.assertIn("-npopen 1 --la [REDACTED] --ua wd", out)
        self.assertIn("-window-mode borderless --la [REDACTED] --ua wd", out)
        self.assertIn("GameBootConfig: --la\n22:12:33,613 INFO  GameController - GameBootConfig: [REDACTED]", out)
        self.assertIn("GameController - json org:[REDACTED]", out)
        self.assertIn("-configmd5 4e7e6b4b42b0d83472d8dae65a13a13c", out)

    def test_file_state_lists_battle_dirs_all_ver_dats_baks_and_proxy_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            game = root / "game"
            src = root / "src_version.dll"
            src.write_bytes(b"built-proxy-bytes")
            src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
            src_md5 = hashlib.md5(src.read_bytes()).hexdigest()

            b3 = game / "battle" / "3_64"
            b3.mkdir(parents=True)
            (b3 / "SSJJ_BattleClient_Unity.exe").write_bytes(b"unity-exe")
            (b3 / "hid.dll").write_bytes(src.read_bytes())
            (b3 / ".hid.dat").write_bytes(src.read_bytes())
            (b3 / "hid.dll.bak-20261127-100000").write_bytes(b"hid-bak")
            (b3 / "version.dll").write_bytes(src.read_bytes())
            (b3 / ".ver.dat").write_bytes(src.read_bytes())
            (b3 / ".ver1.dat").write_bytes(b"older-alt")
            (b3 / ".ver10.dat").write_bytes(b"alt-10")
            (b3 / "version.dll.bak-20261125-130049").write_bytes(b"bak-bytes")

            state = {"ts": 1700000000.0, "md5": src_md5, "installed": {str(b3): {"md5": src_md5, "ts": 1700000000.0}}}
            (root / "proxy_state.json").write_text(json.dumps(state), encoding="utf-8")

            with patch.object(diagnostics, "_game_dir", return_value=game), \
                 patch.object(diagnostics, "_ROOT", root), \
                 patch.object(proxy, "dll_source", return_value=str(src)):
                report = diagnostics._file_state()

            self.assertIn("proxy_source=", report)
            self.assertIn(src_sha, report)
            self.assertIn(src_md5, report)
            self.assertIn("DUAL_CONFLICT", report)
            self.assertIn("LEGACY_VERSION_RESIDUAL", report)
            self.assertIn("BAK_RESIDUAL", report)
            self.assertIn("MULTI_ALT", report)
            self.assertIn("hid.dll", report)
            self.assertIn(".hid.dat", report)
            self.assertIn("hid.dll.bak-20261127-100000", report)
            self.assertIn(".ver.dat", report)
            self.assertIn(".ver1.dat", report)
            self.assertIn(".ver10.dat", report)
            self.assertIn("version.dll.bak-20261125-130049", report)
            self.assertIn("proxy_state updated=", report)
            self.assertIn("proxy_state.entry dir=", report)

    def test_dll_load_policy_probe_lists_registry_and_battle_binary_attrs(self):
        with tempfile.TemporaryDirectory() as folder:
            game = Path(folder) / "game"
            b3 = game / "battle" / "3_64"
            b3.mkdir(parents=True)
            exe_bytes = b"unity-exe-payload"
            player_bytes = b"unity-player-payload"
            hid_bytes = b"proxy-hid-dll"
            dll_bytes = b"proxy-version-dll"
            (b3 / "SSJJ_BattleClient_Unity.exe").write_bytes(exe_bytes)
            (b3 / "UnityPlayer.dll").write_bytes(player_bytes)
            (b3 / "hid.dll").write_bytes(hid_bytes)
            (b3 / "version.dll").write_bytes(dll_bytes)

            with patch.object(diagnostics, "_game_dir", return_value=game), \
                 patch.object(diagnostics, "_probe_av_and_mitigations", return_value=["[本机杀软与安全事件]", "  AV: Windows Defender"]):
                probe = diagnostics._dll_load_policy_probe()

            self.assertIn("KnownDLLs:", probe)
            self.assertIn("has_hid=", probe)
            self.assertIn("SessionManager:", probe)
            self.assertIn("IFEO(SSJJ_BattleClient_Unity.exe):", probe)
            self.assertIn("AppCompatFlags\\Layers(HKLM):", probe)
            self.assertIn("AppCompatFlags\\Layers(HKCU):", probe)
            self.assertIn(hashlib.sha256(exe_bytes).hexdigest()[:16], probe)
            self.assertIn(hashlib.sha256(player_bytes).hexdigest()[:16], probe)
            self.assertIn(hashlib.sha256(hid_bytes).hexdigest()[:16], probe)
            self.assertIn(hashlib.sha256(dll_bytes).hexdigest()[:16], probe)
            self.assertIn("motw=none", probe)

    def test_recreated_log_reads_from_start(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "debug.log"
            log.write_text("old text much longer than new\n", encoding="utf-8")
            baseline = diagnostics._stat(log)
            log.write_text("new crash\n", encoding="utf-8")
            self.assertIn("new crash", diagnostics._read_session_part(log, baseline))

    def test_remove_renamed_proxy_keeps_unknown_file(self):
        with tempfile.TemporaryDirectory() as folder:
            battle = Path(folder)
            dll_name = getattr(proxy, "NAME", "hid.dll")
            alt_name = getattr(proxy, "RENAMED", ".hid.dat")
            own = battle / alt_name
            own.write_bytes(b"our proxy")
            unknown = battle / dll_name
            unknown.write_bytes(b"unknown original")
            known_hash = hashlib.md5(own.read_bytes()).hexdigest()
            state = {"md5": known_hash, "installed": {str(battle): {"md5": known_hash}}}
            with patch.object(proxy, "battle_dirs", return_value=[str(battle)]), \
                 patch.object(proxy, "_load_state", return_value=state), \
                 patch.object(proxy, "_save_state"), \
                 patch.object(proxy, "our_md5", return_value=known_hash):
                ok, msg = proxy.uninstall()
            self.assertTrue(ok, msg)
            self.assertFalse(own.exists())
            self.assertEqual(unknown.read_bytes(), b"unknown original")


if __name__ == "__main__":
    unittest.main()
