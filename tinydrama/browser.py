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

    def __enter__(self):
        return self

    def __exit__(self, *args):
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

        last_error = None
        for _ in range(50):
            try:
                return self._connect_first_tab()
            except Exception as e:
                last_error = e
                time.sleep(0.2)
        raise Exception(f"浏览器启动超时: {last_error}")

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

    def close(self):
        """关闭浏览器"""
        if self._managers:
            try:
                any_manager = next(iter(self._managers.values()))
                any_manager._cdp.send("Browser.close")
            except Exception:
                pass

            for manager in self._managers.values():
                try:
                    manager._cdp.close()
                except Exception:
                    pass
            self._managers.clear()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()


# 兼容性别名
MiniBrowser = Browser
