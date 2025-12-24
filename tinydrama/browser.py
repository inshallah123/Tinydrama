"""
Browser 模块 - 浏览器管理

提供浏览器启动、连接和标签页管理功能。
"""

import subprocess
import time
import http.client
import json
import os
import shutil
import sys
from typing import Optional

# Windows 注册表模块（仅 Windows 可用）
if sys.platform == "win32":
    import winreg

from .cdp import CDPSession
from .frame import Frame, FrameManager


class Browser:
    """浏览器管理器"""

    def __init__(self, debug_port: int = 9222, timeout: float = 30):
        self.debug_port = debug_port
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self._managers: dict[str, FrameManager] = {}  # target_id -> FrameManager
        self._browser_cdp: Optional[CDPSession] = None  # browser-level 连接
        self._pending_downloads: dict[str, dict] = {}  # guid -> event params
        self._completed_downloads: dict[str, dict] = {}  # guid -> event params

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ==================== 启动与连接 ====================

    def launch(self, kill_existing: bool = True, browser: str = "auto") -> Frame:
        """启动浏览器并返回主 Frame

        Args:
            kill_existing: 是否先关闭已存在的浏览器进程
            browser: 浏览器类型，可选 "auto", "chrome", "edge" 或浏览器路径

        Returns:
            主 Frame 对象
        """
        if browser == "auto":
            browser_path = self._find_browser()
        elif browser == "chrome":
            browser_path = self._find_browser("chrome")
        elif browser == "edge":
            browser_path = self._find_browser("edge")
        else:
            browser_path = browser

        process_name = "msedge.exe" if "edge" in browser_path.lower() else "chrome.exe"

        if kill_existing:
            # 清理旧连接
            for manager in self._managers.values():
                try:
                    manager._cdp.close()
                except Exception:
                    pass
            self._managers.clear()
            if self._browser_cdp:
                try:
                    self._browser_cdp.close()
                except Exception:
                    pass
                self._browser_cdp = None

            # 只关闭占用调试端口的浏览器，不影响用户正常使用的浏览器
            self._close_debug_port_browser()

        # Chrome 136+ 要求 --user-data-dir 配合 --remote-debugging-port 使用
        # 参见: https://developer.chrome.com/blog/remote-debugging-port
        # 配置目录按浏览器类型分开存放，避免格式冲突
        browser_name = "edge" if "edge" in browser_path.lower() else "chrome"
        user_data_dir = os.path.join(os.getcwd(), ".tinydrama", browser_name)
        args = [
            browser_path,
            f"--remote-debugging-port={self.debug_port}",
            f"--user-data-dir={user_data_dir}",
            "--disable-restore-session-state",
        ]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(10):
            try:
                return self._connect_first_tab()
            except Exception:
                time.sleep(0.3)
        raise Exception("浏览器连接超时")

    def _find_browser(self, browser_type: str = "auto") -> str:
        """查找浏览器路径

        查找顺序：
        1. shutil.which() - PATH 环境变量
        2. Windows 注册表 - App Paths
        3. 硬编码常见路径 - 备选
        """
        browsers = []
        if browser_type == "chrome":
            browsers = [("chrome", "chrome.exe")]
        elif browser_type == "edge":
            browsers = [("msedge", "msedge.exe")]
        else:  # auto: Chrome 优先
            browsers = [("chrome", "chrome.exe"), ("msedge", "msedge.exe")]

        for cmd, exe in browsers:
            # 1. PATH 环境变量
            path = shutil.which(cmd)
            if path:
                return path

            # 2. Windows 注册表
            if sys.platform == "win32":
                path = self._find_in_registry(exe)
                if path:
                    return path

            # 3. 硬编码常见路径（备选）
            path = self._find_in_common_paths(exe)
            if path:
                return path

        raise Exception(f"未找到浏览器: {browser_type}")

    def _find_in_registry(self, exe_name: str) -> Optional[str]:
        """从 Windows 注册表查找浏览器路径"""
        try:
            key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                path, _ = winreg.QueryValueEx(key, "")
                if os.path.exists(path):
                    return path
        except (FileNotFoundError, OSError):
            pass
        return None

    def _find_in_common_paths(self, exe_name: str) -> Optional[str]:
        """从常见安装路径查找浏览器"""
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        common_paths = {
            "chrome.exe": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.join(local_appdata, r"Google\Chrome\Application\chrome.exe"),
            ],
            "msedge.exe": [
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ],
        }
        for path in common_paths.get(exe_name, []):
            if os.path.exists(path):
                return path
        return None

    def _close_debug_port_browser(self):
        """关闭占用调试端口的浏览器（不影响用户正常使用的浏览器）"""
        try:
            ws_url = self._cdp_http("/json/version")["webSocketDebuggerUrl"]
            cdp = CDPSession(ws_url, timeout=5)
            try:
                cdp.send("Browser.close")
            except Exception:
                pass
            cdp.close()
            time.sleep(0.5)
        except Exception:
            pass

    def _cdp_http(self, path: str):
        """CDP HTTP 接口请求"""
        conn = http.client.HTTPConnection("127.0.0.1", self.debug_port)
        conn.request("GET", path)
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        conn.close()
        return data

    def connect(self) -> Frame:
        """连接到已运行的浏览器，返回主 Frame"""
        return self._connect_first_tab()

    def _connect_browser_cdp(self):
        """建立 browser-level CDP 连接"""
        if self._browser_cdp:
            return
        ws_url = self._cdp_http("/json/version")["webSocketDebuggerUrl"]
        self._browser_cdp = CDPSession(ws_url, self.timeout)
        self._browser_cdp.on_event(self._handle_browser_event)

    def _handle_browser_event(self, event: dict):
        """处理 browser-level 事件"""
        method = event.get("method")
        params = event.get("params", {})

        if method == "Browser.downloadWillBegin":
            guid = params.get("guid")
            if guid:
                self._pending_downloads[guid] = params
        elif method == "Browser.downloadProgress":
            guid = params.get("guid")
            state = params.get("state")
            if guid and state == "completed":
                self._completed_downloads[guid] = params
                self._pending_downloads.pop(guid, None)

    def _connect_first_tab(self) -> Frame:
        """连接到第一个可用的标签页"""
        targets = self._cdp_http("/json")

        page_target = None
        for target in targets:
            if target.get("type") == "page":
                page_target = target
                break

        if not page_target:
            raise Exception("未找到可用的页面")

        return self._create_frame(page_target)

    def _create_frame(self, target: dict) -> Frame:
        """根据 target 信息创建 FrameManager 和主 Frame"""
        tid = target["id"]
        ws_url = target["webSocketDebuggerUrl"]

        cdp = CDPSession(ws_url, self.timeout)
        manager = FrameManager(cdp, tid)
        self._managers[tid] = manager

        return manager.get_main_frame()

    # ==================== 标签页管理 ====================

    def new_tab(self, url: str = "about:blank") -> Frame:
        """新建标签页并返回主 Frame"""
        if not self._managers:
            raise Exception("没有可用的连接")

        any_manager = next(iter(self._managers.values()))
        result = any_manager._cdp.send("Target.createTarget", {"url": url})
        target_id = result["targetId"]

        time.sleep(0.1)

        targets = self._cdp_http("/json")
        for target in targets:
            if target.get("id") == target_id:
                return self._create_frame(target)

        raise Exception("无法连接到新标签页")

    def close_tab(self, frame: Frame):
        """关闭指定标签页"""
        if not frame._target_id:
            raise Exception("只能关闭根 frame（Tab）")

        manager = frame._manager
        manager._cdp.send("Target.closeTarget", {"targetId": frame._target_id})
        manager._cdp.close()
        self._managers.pop(frame._target_id, None)

    # ==================== 下载功能 ====================

    def enable_download(self, download_path: str):
        """启用下载并指定保存目录"""
        self._connect_browser_cdp()
        assert self._browser_cdp is not None
        self._browser_cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_path,
            "eventsEnabled": True
        })

    def wait_for_download(self, frame: Optional[Frame] = None, timeout: float = 60) -> dict:
        """等待下载完成

        Args:
            frame: 可选，指定等待哪个 frame 触发的下载（与 CDP frameId 一致）
            timeout: 超时时间（秒）

        Returns:
            下载完成信息，包含 guid, totalBytes, receivedBytes, state, frameId 等
        """
        if not self._browser_cdp:
            raise Exception("请先调用 enable_download")

        target_frame_id = frame._frame_id if frame else None
        guid = None
        start = time.time()

        def is_match(params: dict) -> bool:
            """检查下载是否来自指定 frame"""
            if target_frame_id is None:
                return True
            return params.get("frameId") == target_frame_id

        # 等待下载开始
        while time.time() - start < timeout:
            self._browser_cdp.poll_events()
            # 查找匹配的下载
            for g, params in self._pending_downloads.items():
                if is_match(params):
                    guid = g
                    break
            if guid:
                break
            time.sleep(0.1)

        if not guid:
            raise TimeoutError("未检测到下载开始")

        # 等待下载完成
        while time.time() - start < timeout:
            self._browser_cdp.poll_events()
            if guid in self._completed_downloads:
                result = self._completed_downloads.pop(guid)
                # 合并开始事件的信息（包含 url, suggestedFilename 等）
                begin_info = self._pending_downloads.pop(guid, {})
                return {**begin_info, **result}
            time.sleep(0.1)

        raise TimeoutError("下载超时")

    def close(self):
        """关闭浏览器"""
        errors = []

        # 关闭 browser-level 连接
        if self._browser_cdp:
            try:
                self._browser_cdp.send("Browser.close")
            except Exception as e:
                errors.append(f"Browser.close 失败: {e}")
            try:
                self._browser_cdp.close()
            except Exception:
                pass
            self._browser_cdp = None

        # 关闭所有 target 连接
        for manager in self._managers.values():
            try:
                manager._cdp.close()
            except Exception as e:
                errors.append(f"CDPSession.close 失败: {e}")
        self._managers.clear()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception as e:
                errors.append(f"进程终止失败，尝试强制 kill: {e}")
                self.process.kill()

        if errors:
            raise Exception("关闭浏览器时出错:\n" + "\n".join(errors))
