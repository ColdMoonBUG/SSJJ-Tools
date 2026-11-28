# -*- coding: utf-8 -*-
"""大厅左上角「直播模式」跟随遮罩（纯外部 Overlay，零 SWF 改写）。

原理：
- 官方大厅由 AirLobbyPreloader.exe（默认 1024×650 无边框 AIR 窗口）渲染，
  在微端模式下通常作为 MicroClient.exe 的嵌套子窗口存在（标题含 SSJJ_FlashAir_Battle）。
- 本模块使用 PySide6 无边框点击穿透置顶窗口，按 120ms 定时器轮询定位大厅客户端区域
  左上角昵称条，实时绘制自定义昵称，并通过 Win32 SetWindowPos(HWND_TOPMOST) 巩固置顶。
- 关闭时彻底停止定时器并隐藏窗口，零 CPU 占用；窗口最小化或切到其他应用时自动隐藏。
- 仅在状态变化时向 %LOCALAPPDATA%\\ssjj-toolbox\\lobby_overlay.log 写一行诊断日志。
- 在无 GUI / 测试环境下导入安全不报错。
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

# 大厅设计基准尺寸（见 AirLobbyPreloader.as:29-30）及左上角昵称条相对坐标
_BASE_W = 1024.0
_BASE_H = 650.0
_NICK_REL_X = 40.0 / _BASE_W
_NICK_REL_Y = 4.0 / _BASE_H
_NICK_REL_W = 248.0 / _BASE_W
_NICK_REL_H = 26.0 / _BASE_H

_POLL_INTERVAL_MS = 120
_TARGET_IMAGE = "airlobbypreloader.exe"
_MICROCLIENT_IMAGE = "microclient.exe"
_WDLAUNCHER_IMAGE = "wdlauncher.exe"
_AIR_CHAT_IMAGE = "airalonechat.exe"
_AIR_TITLE_PREFIX = "ssjj_flashair_battle"
_FOREGROUND_ALLOW_IMAGES = (
    _TARGET_IMAGE,
    _MICROCLIENT_IMAGE,
    _WDLAUNCHER_IMAGE,
    _AIR_CHAT_IMAGE,
)
_MIN_LOBBY_W = 640
_MIN_LOBBY_H = 400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_GA_ROOT = 2
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = ctypes.c_void_p(-1).value
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_DEFAULT_FAKE_NAME = "神秘玩家"
_LOG_MAX_BYTES = 512 * 1024

try:
    from PySide6.QtCore import QPoint, QRect, Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
    from PySide6.QtWidgets import QWidget

    _HAS_QT = True
except Exception:  # pragma: no cover - 无 GUI 环境兜底
    _HAS_QT = False
    QWidget = object


# ---------------------------------------------------------------- 诊断日志
def _log_path():
    local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local_app, "ssjj-toolbox", "lobby_overlay.log")


def _log_event(msg):
    """仅在状态变化时写 1 行带时间戳日志（单文件超 512KB 自动截断重写）。"""
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a"
        try:
            if os.path.isfile(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(path, mode, encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------- Win32 64 位 API 绑定
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_U32 = None
_K32 = None

if os.name == "nt":
    try:
        _U32 = ctypes.WinDLL("user32", use_last_error=True)
        _K32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _K32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        _K32.OpenProcess.restype = wt.HANDLE
        _K32.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD),
        ]
        _K32.QueryFullProcessImageNameW.restype = wt.BOOL
        _K32.CloseHandle.argtypes = [wt.HANDLE]
        _K32.CloseHandle.restype = wt.BOOL
        _K32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        _K32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        _K32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        _K32.Process32FirstW.restype = wt.BOOL
        _K32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        _K32.Process32NextW.restype = wt.BOOL

        _U32.EnumWindows.argtypes = [_WNDENUMPROC, wt.LPARAM]
        _U32.EnumWindows.restype = wt.BOOL
        _U32.EnumChildWindows.argtypes = [wt.HWND, _WNDENUMPROC, wt.LPARAM]
        _U32.EnumChildWindows.restype = wt.BOOL
        _U32.GetForegroundWindow.argtypes = []
        _U32.GetForegroundWindow.restype = wt.HWND
        _U32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        _U32.GetAncestor.restype = wt.HWND
        _U32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        _U32.GetWindowThreadProcessId.restype = wt.DWORD
        _U32.IsWindowVisible.argtypes = [wt.HWND]
        _U32.IsWindowVisible.restype = wt.BOOL
        _U32.IsIconic.argtypes = [wt.HWND]
        _U32.IsIconic.restype = wt.BOOL
        _U32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
        _U32.GetClientRect.restype = wt.BOOL
        _U32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
        _U32.GetWindowRect.restype = wt.BOOL
        _U32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(_POINT)]
        _U32.ClientToScreen.restype = wt.BOOL
        _U32.SetWindowPos.argtypes = [
            wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wt.UINT,
        ]
        _U32.SetWindowPos.restype = wt.BOOL
        _U32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        _U32.GetWindowLongW.restype = wt.LONG
        _U32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
        _U32.SetWindowLongW.restype = wt.LONG
        _U32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        _U32.GetWindowTextW.restype = ctypes.c_int
    except Exception:
        _U32 = None
        _K32 = None


def _query_process_basename(pid):
    """通过 OpenProcess + QueryFullProcessImageNameW 获取进程可执行文件名（小写）。"""
    if not pid or _K32 is None:
        return ""
    try:
        handle = _K32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf_len = wt.DWORD(512)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if _K32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return os.path.basename(buf.value).lower()
        finally:
            _K32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _has_running_process(image_lower):
    """使用 Toolhelp32 快照判断指定进程名（小写）是否存在。"""
    if _K32 is None:
        return False
    try:
        snap = _K32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE_VALUE:
            return False
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _K32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if (entry.szExeFile or "").lower() == image_lower:
                    return True
                ok = _K32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _K32.CloseHandle(snap)
    except Exception:
        pass
    return False


def _get_window_pid(hwnd):
    if not hwnd or _U32 is None:
        return 0
    try:
        pid = wt.DWORD(0)
        _U32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _get_window_title(hwnd):
    if not hwnd or _U32 is None:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = _U32.GetWindowTextW(hwnd, buf, 256)
        return buf.value[:n] if n > 0 else ""
    except Exception:
        return ""


def _get_client_screen_rect(hwnd, min_w=_MIN_LOBBY_W, min_h=_MIN_LOBBY_H):
    """返回指定窗口客户端区域在屏幕坐标系中的 (x, y, w, h)（物理像素），失败返回 None。"""
    if not hwnd or _U32 is None:
        return None
    try:
        rc = _RECT()
        if not _U32.GetClientRect(hwnd, ctypes.byref(rc)):
            return None
        w = int(rc.right - rc.left)
        h = int(rc.bottom - rc.top)
        if w < min_w or h < min_h:
            return None
        pt = _POINT(0, 0)
        if not _U32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return (int(pt.x), int(pt.y), w, h)
    except Exception:
        return None


def _estimate_lobby_rect_from_microclient(mc_rect):
    """按 Mini.as:348-361 布局公式从 MicroClient 客户区兜底估算大厅区域。"""
    mx, my, mw, mh = mc_rect
    lh = min(650.0, max(1.0, mh - 50.0 - 36.0))
    lw = min(1024.0, max(1.0, mw - 10.0 - 10.0))
    off_x = max(0.0, (mw - lw) * 0.5)
    return (int(round(mx + off_x)), int(round(my + 50.0)), int(round(lw)), int(round(lh)))


def find_lobby_window():
    """查找可见且未最小化的官方大厅主窗口（最小 640×400，联合枚举顶层与全部大窗口子窗口）。

    返回 (hwnd, pid, (x, y, w, h))；未找到时返回 None。
    """
    if os.name != "nt" or _U32 is None:
        return None
    try:
        candidates = {}
        large_tops = []
        host_tops = []
        pid_cache = {}

        def _proc_of(pid):
            name = pid_cache.get(pid)
            if name is None:
                name = _query_process_basename(pid)
                pid_cache[pid] = name
            return name

        def _is_air_window(hwnd, pid):
            cname = _proc_of(pid) if pid else ""
            if cname == _TARGET_IMAGE:
                return True
            ctitle = _get_window_title(hwnd).lower()
            return _AIR_TITLE_PREFIX in ctitle

        def _enum_top(hwnd, _lparam):
            try:
                if not _U32.IsWindowVisible(hwnd) or _U32.IsIconic(hwnd):
                    return 1
                rect = _get_client_screen_rect(hwnd, min_w=_MIN_LOBBY_W, min_h=_MIN_LOBBY_H)
                if rect is None:
                    return 1
                pid = _get_window_pid(hwnd)
                if not pid:
                    return 1
                hwnd_int = int(hwnd)
                large_tops.append(hwnd_int)
                if _is_air_window(hwnd, pid):
                    candidates[hwnd_int] = (hwnd_int, pid, rect)
                else:
                    name = _proc_of(pid)
                    if name in (_MICROCLIENT_IMAGE, _WDLAUNCHER_IMAGE):
                        host_tops.append((hwnd_int, pid, rect))
            except Exception:
                pass
            return 1

        _U32.EnumWindows(_WNDENUMPROC(_enum_top), 0)

        # 对所有可见非最小化大顶层窗口（涵盖 MicroClient / WDlauncher 及任意宿主）统一递归枚举子窗口
        for top_hwnd in large_tops:
            def _enum_child(chwnd, _lparam):
                try:
                    if not _U32.IsWindowVisible(chwnd) or _U32.IsIconic(chwnd):
                        return 1
                    crect = _get_client_screen_rect(chwnd, min_w=_MIN_LOBBY_W, min_h=_MIN_LOBBY_H)
                    if crect is None:
                        return 1
                    cpid = _get_window_pid(chwnd)
                    if not _is_air_window(chwnd, cpid):
                        return 1
                    chwnd_int = int(chwnd)
                    candidates[chwnd_int] = (chwnd_int, cpid, crect)
                except Exception:
                    pass
                return 1

            _U32.EnumChildWindows(top_hwnd, _WNDENUMPROC(_enum_child), 0)

        # 顶层与子窗口候选合并后，按面积降序取最大者（稳锁 1024×650 主大厅窗口）
        if candidates:
            ranked = sorted(candidates.values(), key=lambda item: item[2][2] * item[2][3], reverse=True)
            return ranked[0]

        # 兜底：若 MicroClient / WDlauncher 主窗口可见且 AirLobbyPreloader.exe 进程存在，按 Mini.as:348-361 推算
        if host_tops and _has_running_process(_TARGET_IMAGE):
            host_tops.sort(key=lambda item: item[2][2] * item[2][3], reverse=True)
            mc_hwnd, mc_pid, mc_rect = host_tops[0]
            return (mc_hwnd, mc_pid, _estimate_lobby_rect_from_microclient(mc_rect))

        return None
    except Exception:
        return None


def _is_lobby_or_self_foreground(lobby_hwnd, lobby_pid):
    """判断当前前台窗口是否为大厅窗口、其顶层宿主、MicroClient 或工具箱自身。"""
    if os.name != "nt" or _U32 is None:
        return False
    try:
        root_hwnd = _U32.GetAncestor(lobby_hwnd, _GA_ROOT)
        if root_hwnd and _U32.IsIconic(root_hwnd):
            return False
        fg = _U32.GetForegroundWindow()
        if not fg:
            return False
        fg_int = int(fg)
        if fg_int == int(lobby_hwnd):
            return True
        if root_hwnd and fg_int == int(root_hwnd):
            return True
        fg_pid = _get_window_pid(fg)
        if fg_pid:
            if fg_pid in (int(lobby_pid), os.getpid()):
                return True
            if _query_process_basename(fg_pid) in _FOREGROUND_ALLOW_IMAGES:
                return True
        return False
    except Exception:
        return False


def _foreground_proc_name():
    if os.name != "nt" or _U32 is None:
        return ""
    try:
        fg = _U32.GetForegroundWindow()
        if not fg:
            return ""
        return _query_process_basename(_get_window_pid(fg)) or ("pid:%d" % _get_window_pid(fg))
    except Exception:
        return ""


def compute_overlay_rect(client_rect):
    """根据大厅客户端区域 (x, y, w, h) 按 1024×650 比例计算左上角昵称遮罩矩形。"""
    cx, cy, cw, ch = client_rect
    ox = int(round(cx + cw * _NICK_REL_X))
    oy = int(round(cy + ch * _NICK_REL_Y))
    ow = max(140, int(round(cw * _NICK_REL_W)))
    oh = max(22, int(round(ch * _NICK_REL_H)))
    return (ox, oy, ow, oh)


def _physical_to_logical(rect):
    """将 Win32 物理像素坐标换算为 Qt 逻辑像素坐标（适配 125%/150%/200% DPI 缩放）。"""
    x, y, w, h = rect
    if not _HAS_QT:
        return rect
    try:
        screen = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
        dpr = float(screen.devicePixelRatio()) if screen else 1.0
        if dpr > 0.0 and abs(dpr - 1.0) > 0.01:
            return (
                int(round(x / dpr)),
                int(round(y / dpr)),
                max(100, int(round(w / dpr))),
                max(18, int(round(h / dpr))),
            )
    except Exception:
        pass
    return rect


# ---------------------------------------------------------------- PySide6 遮罩组件
if _HAS_QT:

    class LobbyNameOverlay(QWidget):
        """跟随大厅左上角昵称条的点击穿透置顶遮罩。"""

        def __init__(self, parent=None):
            super().__init__(parent)
            flags = (
                Qt.FramelessWindowHint
                | Qt.Tool
                | Qt.WindowStaysOnTopHint
                | Qt.WindowTransparentForInput
            )
            if hasattr(Qt, "WindowDoesNotAcceptFocus"):
                flags |= Qt.WindowDoesNotAcceptFocus
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)

            self._enabled = False
            self._lobby_overlay = True
            self._fake_name = _DEFAULT_FAKE_NAME
            self._last_geom = None
            self._last_state_key = None

            self._timer = QTimer(self)
            self._timer.setInterval(_POLL_INTERVAL_MS)
            self._timer.timeout.connect(self._poll)

        def _note_state(self, key, detail):
            if key != self._last_state_key:
                self._last_state_key = key
                _log_event(detail)

        def _enforce_win32_topmost(self):
            """通过 Win32 扩展样式与 SetWindowPos(HWND_TOPMOST) 巩固置顶且绝不抢焦点。"""
            if os.name != "nt" or _U32 is None:
                return
            try:
                hwnd = int(self.winId())
                if not hwnd:
                    return
                ex = _U32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
                want = ex | _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
                if want != ex:
                    _U32.SetWindowLongW(hwnd, _GWL_EXSTYLE, want)
                _U32.SetWindowPos(
                    hwnd,
                    _HWND_TOPMOST,
                    0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                )
            except Exception:
                pass

        def is_active(self):
            return bool(self._enabled and self._lobby_overlay)

        def set_config(self, enabled, fake_name, lobby_overlay=True):
            """更新直播模式配置；仅在 enabled 且 lobby_overlay 为 True 时运行轮询。"""
            self._enabled = bool(enabled)
            self._lobby_overlay = bool(lobby_overlay)
            name = str(fake_name or "").strip()
            self._fake_name = name or _DEFAULT_FAKE_NAME

            if self.is_active():
                _log_event("config active fake_name=%r" % (self._fake_name,))
                if not self._timer.isActive():
                    self._timer.start()
                self._poll()
                self.update()
            else:
                self._note_state(
                    "disabled",
                    "config disabled enabled=%s lobby_overlay=%s" % (self._enabled, self._lobby_overlay),
                )
                if self._timer.isActive():
                    self._timer.stop()
                self._last_geom = None
                if self.isVisible():
                    self.hide()

        def refresh_now(self):
            if self.is_active():
                self._poll()

        def stop(self):
            """停止定时器并关闭遮罩，释放资源。"""
            try:
                if self._timer.isActive():
                    self._timer.stop()
            except Exception:
                pass
            self._enabled = False
            self._last_geom = None
            self._note_state("stopped", "overlay stopped")
            try:
                if self.isVisible():
                    self.hide()
                self.close()
            except Exception:
                pass

        def _poll(self):
            if not self.is_active():
                if self.isVisible():
                    self.hide()
                return
            try:
                hit = find_lobby_window()
                if not hit:
                    self._note_state("no_window", "hidden: no visible lobby window found")
                    if self.isVisible():
                        self.hide()
                    return
                hwnd, pid, client_rect = hit
                if not _is_lobby_or_self_foreground(hwnd, pid):
                    fg_name = _foreground_proc_name()
                    self._note_state(
                        ("not_fg", fg_name),
                        "hidden: not foreground (lobby=0x%x pid=%d fg=%s)" % (hwnd, pid, fg_name or "none"),
                    )
                    if self.isVisible():
                        self.hide()
                    return
                phys_rect = compute_overlay_rect(client_rect)
                lx, ly, lw, lh = _physical_to_logical(phys_rect)
                geom = (lx, ly, lw, lh)
                need_topmost = False
                if geom != self._last_geom:
                    self._last_geom = geom
                    self.setGeometry(QRect(lx, ly, lw, lh))
                    need_topmost = True
                if not self.isVisible():
                    self.show()
                    need_topmost = True
                if need_topmost:
                    self._enforce_win32_topmost()
                self._note_state(
                    ("shown", hwnd, geom),
                    "shown: hwnd=0x%x pid=%d client=%s overlay=%s name=%r"
                    % (hwnd, pid, client_rect, geom, self._fake_name),
                )
                self.update()
            except Exception as e:
                self._note_state(("err", repr(e)), "poll error: %r" % (e,))
                try:
                    if self.isVisible():
                        self.hide()
                except Exception:
                    pass

        def paintEvent(self, _event):
            try:
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setRenderHint(QPainter.TextAntialiasing, True)

                rect = self.rect().adjusted(0, 0, -1, -1)
                path = QPainterPath()
                path.addRoundedRect(rect, 4.0, 4.0)

                # 深色半透明底色与微弱金属边框，贴合大厅左上角视觉风格
                painter.fillPath(path, QColor(14, 18, 26, 236))
                painter.setPen(QPen(QColor(74, 102, 142, 128), 1.0))
                painter.drawPath(path)

                font_px = max(11, min(16, int(round(self.height() * 0.52))))
                font = QFont("Microsoft YaHei UI")
                font.setPixelSize(font_px)
                font.setBold(True)
                painter.setFont(font)

                text_rect = rect.adjusted(8, 0, -8, 0)
                fm = QFontMetrics(font)
                elided = fm.elidedText(self._fake_name, Qt.ElideRight, max(10, text_rect.width()))

                # 轻微文字阴影提升对比度
                painter.setPen(QColor(0, 0, 0, 180))
                painter.drawText(text_rect.translated(1, 1), Qt.AlignVCenter | Qt.AlignLeft, elided)
                painter.setPen(QColor(240, 246, 255, 250))
                painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
                painter.end()
            except Exception:
                pass

else:  # pragma: no cover - 无 GUI 环境安全占位

    class LobbyNameOverlay(object):
        """无 PySide6 环境下的空实现，保证导入与调用不报错。"""

        def __init__(self, parent=None):
            self._enabled = False
            self._lobby_overlay = True
            self._fake_name = _DEFAULT_FAKE_NAME

        def is_active(self):
            return False

        def set_config(self, enabled, fake_name, lobby_overlay=True):
            self._enabled = bool(enabled)
            self._lobby_overlay = bool(lobby_overlay)
            self._fake_name = str(fake_name or "").strip() or _DEFAULT_FAKE_NAME

        def refresh_now(self):
            pass

        def stop(self):
            self._enabled = False
