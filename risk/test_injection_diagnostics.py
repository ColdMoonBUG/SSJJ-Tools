# -*- coding: utf-8 -*-
"""只验证注入结果判定；不打开游戏进程。"""
import os
import json
import sys
import tempfile
import unittest
from unittest import mock

from . import guard, injector


class InjectionDiagnosticsTests(unittest.TestCase):
    def test_trace_is_durable_and_correlates_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "injection_trace.jsonl")
            with mock.patch.object(injector, "TRACE_PATH", path):
                self.assertEqual(injector.trace_event(123, "attempt-1", "remote_thread.begin",
                                                      start_address="0x1000"), path)
                self.assertEqual(injector.trace_event(123, "attempt-1", "remote_thread.created",
                                                      tid=456), path)
            with open(path, "r", encoding="utf-8") as source:
                entries = [json.loads(line) for line in source]
            self.assertEqual([entry["stage"] for entry in entries],
                             ["remote_thread.begin", "remote_thread.created"])
            self.assertEqual({entry["attempt"] for entry in entries}, {"attempt-1"})
            self.assertEqual(entries[1]["tid"], 456)

    def test_marker_must_be_new_and_match_exact_pid(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "risk_inject.log")
            with open(path, "wb") as f:
                f.write("载荷载入 pid=123 运行时=old\n".encode("utf-8"))
            offsets = {path: os.path.getsize(path)}
            self.assertFalse(injector._payload_start_logged(123, [path], offsets))
            with open(path, "ab") as f:
                f.write("载荷载入 pid=1234 运行时=new\n".encode("utf-8"))
            self.assertFalse(injector._payload_start_logged(123, [path], offsets))
            with open(path, "ab") as f:
                f.write("载荷载入 pid=123 运行时=new\n".encode("utf-8"))
            self.assertTrue(injector._payload_start_logged(123, [path], offsets))

    def test_fallback_log_path(self):
        with tempfile.TemporaryDirectory() as root:
            fallback = os.path.join(root, "ssjj_risk.log")
            with open(fallback, "wb") as f:
                f.write("载荷载入 pid=456 运行时=new\n".encode("utf-8"))
            self.assertTrue(injector._payload_start_logged(
                456, [os.path.join(root, "missing.log"), fallback], {fallback: 0}))

    def test_other_instance_is_not_reported_as_success(self):
        pid = 987654
        try:
            with mock.patch.object(guard, "_claim", return_value=False), \
                    mock.patch.object(injector, "trace_event"), \
                    mock.patch.object(guard, "log", side_effect=lambda s: s):
                ok, detail = guard.inject_now(pid=pid, target="example.exe")
            self.assertFalse(ok)
            self.assertIn("未确认", detail)
            self.assertIn(pid, guard._done_pids)
        finally:
            guard._done_pids.discard(pid)

    def test_post_attempt_error_is_not_retried(self):
        pid = 987655
        try:
            with mock.patch.object(guard, "_claim", return_value=True), \
                    mock.patch.object(injector, "trace_event"), \
                    mock.patch.object(guard.injector, "build_payload",
                                      return_value=([], "fixture.dll")), \
                    mock.patch.object(guard.injector, "inject",
                                      side_effect=injector.InjectionAttempted(
                                          RuntimeError("read failed"), ["diagnostic"])), \
                    mock.patch.object(guard, "log", side_effect=lambda s: s), \
                    mock.patch.object(guard, "_release") as release:
                ok, detail = guard.inject_now(pid=pid, target="example.exe")
            self.assertFalse(ok)
            self.assertIn("本局不重试", detail)
            self.assertIn(pid, guard._done_pids)
            release.assert_not_called()
        finally:
            guard._done_pids.discard(pid)

    def test_skin_launch_starts_injection(self):
        toolbox_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "toolbox")
        with mock.patch.object(sys, "path", [toolbox_dir] + sys.path):
            from toolbox import oneclick
        messages = []
        with mock.patch.object(oneclick, "_load_rules", return_value=[{"src": "test"}]), \
                mock.patch.object(guard, "inject_state", return_value={"enabled": False}), \
                mock.patch.object(guard, "inject_start", return_value=(True, "started")) as start, \
                mock.patch("risk.config.get", return_value=False), \
                mock.patch.object(oneclick.quick_login, "active_official_lobby",
                                  return_value=False), \
                mock.patch.object(oneclick.quick_login, "launch_official",
                                  return_value=(True, "game launched")) as launch:
            ok, detail = oneclick.launch(
                oneclick.MODE_INJECT, "", "", "user", "password", "server",
                emit=messages.append)
        self.assertTrue(ok)
        self.assertEqual(detail, "game launched")
        self.assertTrue(any("started" in msg for msg in messages))
        start.assert_called_once()
        launch.assert_called_once()

    def test_skin_launch_reuses_open_lobby(self):
        toolbox_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "toolbox")
        with mock.patch.object(sys, "path", [toolbox_dir] + sys.path):
            from toolbox import oneclick
        with mock.patch.object(oneclick, "_load_rules", return_value=[{"src": "test"}]), \
                mock.patch.object(guard, "inject_state", return_value={"enabled": False}), \
                mock.patch.object(guard, "inject_start", return_value=(True, "started")) as start, \
                mock.patch("risk.config.get", return_value=False), \
                mock.patch.object(oneclick.quick_login, "active_official_lobby",
                                  return_value=True), \
                mock.patch.object(oneclick.quick_login, "launch_direct") as launch:
            ok, detail = oneclick.launch(
                oneclick.MODE_INJECT, "", "", "", "", "server")
        self.assertTrue(ok)
        self.assertIn("已在大厅", detail)
        start.assert_called_once()
        launch.assert_not_called()

    def test_direct_login_does_not_force_close_existing_clients(self):
        toolbox_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "toolbox")
        with mock.patch.object(sys, "path", [toolbox_dir] + sys.path):
            from toolbox import quick_login
        with mock.patch.object(quick_login, "find_microclient_exe",
                               return_value=r"C:\Game\MicroClient.exe"), \
                mock.patch.object(quick_login, "active_official_lobby",
                                  return_value=False), \
                mock.patch.object(quick_login, "get_login_url",
                                  return_value=(True, "http://example.test/login.php", False)), \
                mock.patch.object(quick_login, "cleanup_official_game_procs") as cleanup, \
                mock.patch.object(quick_login.ctypes.windll.shell32, "ShellExecuteW",
                                  return_value=42), \
                mock.patch.object(quick_login, "_log_launch"), \
                mock.patch.object(quick_login, "_watch_launch"):
            ok, _detail = quick_login.launch_direct("user", "pass", "server")
        self.assertTrue(ok)
        cleanup.assert_not_called()

    def test_skin_launch_stops_if_guard_did_not_start(self):
        toolbox_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "toolbox")
        with mock.patch.object(sys, "path", [toolbox_dir] + sys.path):
            from toolbox import oneclick
        with mock.patch.object(oneclick, "_load_rules", return_value=[{"src": "test"}]), \
                mock.patch.object(guard, "inject_state", return_value={"enabled": False}), \
                mock.patch.object(guard, "inject_start", return_value=(False, "failed")), \
                mock.patch("risk.config.get", return_value=False), \
                mock.patch.object(oneclick.quick_login, "launch_direct") as launch:
            ok, detail = oneclick.launch(
                oneclick.MODE_INJECT, "", "", "user", "password", "server")
        self.assertFalse(ok)
        self.assertIn("已停止", detail)
        launch.assert_not_called()

    def test_mono_detection_waits_eight_tenths_before_injection(self):
        pid = 987656
        waits = []

        def setting(key, default=None):
            return {
                "inject.targets": ["example.exe"],
                "inject.enabled": True,
                "inject.settle_seconds": 0.8,
                "inject.auto_elevate": False,
            }.get(key, default)

        def injected(*args, **kwargs):
            guard._stop.set()
            return True, "done"

        guard._stop.clear()
        try:
            with mock.patch.object(guard.config, "get", side_effect=setting), \
                    mock.patch.object(injector, "trace_event"), \
                    mock.patch.object(guard.injector, "list_processes",
                                      return_value=[{"pid": pid}]), \
                    mock.patch.object(guard.injector, "find_mono",
                                      return_value={"name": "mono.dll", "base": 0x1000, "path": "mono.dll"}), \
                    mock.patch.object(guard, "inject_now", side_effect=injected) as start, \
                    mock.patch.object(guard._stop, "wait",
                                      side_effect=lambda t: waits.append(t) or False), \
                    mock.patch.object(guard, "log"):
                guard._watch()
            self.assertAlmostEqual(waits[0], 0.8, delta=0.1)
            start.assert_called_once()
        finally:
            guard._stop.clear()
            guard._done_pids.discard(pid)


if __name__ == "__main__":
    unittest.main()
