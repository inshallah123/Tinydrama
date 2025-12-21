"""
Browser 模块 - 浏览器管理

提供浏览器启动、连接和标签页管理功能。
"""

import subprocess
import time
import http.client
import json
import os
from typing import Optional

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

            os.system(f"taskkill /f /im {process_name} 2>nul")
            for _ in range(10):
                result = subprocess.run("tasklist", capture_output=True, text=True, shell=True)
                if process_name not in result.stdout:
                    break
                time.sleep(0.1)

        args = [
            browser_path,
            f"--remote-debugging-port={self.debug_port}",
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
        """查找浏览器路径"""
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        edge_paths = [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]

        if browser_type == "chrome":
            paths = chrome_paths
        elif browser_type == "edge":
            paths = edge_paths
        else:
            paths = chrome_paths + edge_paths

        for p in paths:
            if os.path.exists(p):
                return p
        raise Exception(f"未找到浏览器: {browser_type}")

    def connect(self) -> Frame:
        """连接到已运行的浏览器，返回主 Frame"""
        return self._connect_first_tab()

    def _get_targets(self) -> list:
        """获取所有调试目标"""
        conn = http.client.HTTPConnection("127.0.0.1", self.debug_port)
        conn.request("GET", "/json")
        response = conn.getresponse()
        targets = json.loads(response.read().decode())
        conn.close()
        return targets

    def _get_browser_ws_url(self) -> str:
        """获取 browser-level WebSocket URL"""
        conn = http.client.HTTPConnection("127.0.0.1", self.debug_port)
        conn.request("GET", "/json/version")
        response = conn.getresponse()
        info = json.loads(response.read().decode())
        conn.close()
        return info["webSocketDebuggerUrl"]

    def _connect_browser_cdp(self):
        """建立 browser-level CDP 连接"""
        if self._browser_cdp:
            return
        ws_url = self._get_browser_ws_url()
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
        targets = self._get_targets()

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

        targets = self._get_targets()
        for target in targets:
            if target.get("id") == target_id:
                return self._create_frame(target)

        raise Exception("无法连接到新标签页")

    def get_frames(self) -> list[Frame]:
        """获取所有根 Frame（即所有 Tab）"""
        frames = []
        for manager in self._managers.values():
            for frame in manager._frames.values():
                if frame.is_root:
                    frames.append(frame)
        return frames

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

    def wait_for_download(self, timeout: float = 60) -> dict:
        """等待下载完成

        Returns:
            下载完成信息，包含 guid, totalBytes, receivedBytes, state 等
        """
        if not self._browser_cdp:
            raise Exception("请先调用 enable_download")

        guid = None
        start = time.time()

        # 等待下载开始
        while time.time() - start < timeout:
            self._browser_cdp.poll_events()
            if self._pending_downloads:
                guid = next(iter(self._pending_downloads.keys()))
                break
            time.sleep(0.1)

        if not guid:
            raise TimeoutError("未检测到下载开始")

        # 等待下载完成
        while time.time() - start < timeout:
            self._browser_cdp.poll_events()
            if guid in self._completed_downloads:
                return self._completed_downloads.pop(guid)
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
