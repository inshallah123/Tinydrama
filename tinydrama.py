"""
Mini Browser Automation - 基于Python标准库的简易浏览器自动化工具
仅使用标准库实现，通过Chrome DevTools Protocol (CDP) 控制浏览器
"""

import socket
import struct
import base64
import json
import subprocess
import time
import http.client
import os
from urllib.parse import urlparse
from typing import Optional, Any


class WebSocketClient:
    """简易WebSocket客户端实现"""

    def __init__(self, url: str, timeout: float = 30):
        parsed = urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def connect(self):
        """建立WebSocket连接"""
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(self.timeout)

        # WebSocket握手
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.send(handshake.encode())

        # 读取握手响应
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(1024)

        if b"101" not in response:
            raise Exception(f"WebSocket握手失败: {response.decode()}")

    def send(self, data: str):
        """发送WebSocket消息"""
        payload = data.encode('utf-8')
        length = len(payload)
        # 构建帧头
        header = bytearray()
        header.append(0x81)  # FIN + text frame
        # 客户端必须使用掩码
        mask_key = os.urandom(4)
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask_key)
        # 应用掩码
        masked = bytearray(payload)
        for i in range(length):
            masked[i] ^= mask_key[i % 4]
        self.sock.send(bytes(header) + bytes(masked))

    def recv(self) -> str:
        """接收WebSocket消息"""
        # 读取帧头
        header = self._recv_exact(2)
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        if masked:
            mask_key = self._recv_exact(4)
            payload = bytearray(self._recv_exact(length))
            for i in range(length):
                payload[i] ^= mask_key[i % 4]
            payload = bytes(payload)
        else:
            payload = self._recv_exact(length)

        if opcode == 0x08:  # close frame
            raise Exception("WebSocket连接已关闭")

        return payload.decode('utf-8')

    def _recv_exact(self, n: int) -> bytes:
        """精确接收n字节"""
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise Exception("连接断开")
            data += chunk
        return data

    def close(self):
        self.sock.close()

class CDPSession:
    """Chrome DevTools Protocol 会话"""

    def __init__(self, ws_url: str, timeout: float = 30):
        self.ws = WebSocketClient(ws_url, timeout)
        self.ws.connect()
        self._msg_id = 0
        self._responses = {}
        # 实时处理的事件存储
        self._frame_contexts: dict[str, int] = {}  # frame_id -> context_id
        self._pending_downloads: dict[str, dict] = {}  # guid -> download info
        self._completed_downloads: dict[str, dict] = {}  # guid -> completion info
        self._pending_dialog: Optional[dict] = None

    def send(self, method: str, params: Optional[dict] = None) -> dict:
        """发送CDP命令并等待响应，仅 method → 无参数命令，method + params → 带参数命令"""
        self._msg_id += 1
        msg_id = self._msg_id

        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params
        self.ws.send(json.dumps(message))
        # 等待响应
        while msg_id not in self._responses:
            try:
                data = self.ws.recv()
                msg = json.loads(data)

                if "id" in msg:
                    self._responses[msg["id"]] = msg
                else:
                    # 实时处理事件
                    self._handle_event(msg)
            except socket.timeout:
                raise Exception(f"等待响应超时: {method}")

        response = self._responses.pop(msg_id)
        if "error" in response:
            raise Exception(f"CDP错误: {response['error']}")

        return response.get("result", {})

    def poll_events(self, timeout: float = 0.1):
        """主动接收并处理待处理的事件"""
        self.ws.sock.settimeout(timeout)
        try:
            while True:
                data = self.ws.recv()
                msg = json.loads(data)
                if "id" in msg:
                    self._responses[msg["id"]] = msg
                else:
                    self._handle_event(msg)
        except socket.timeout:
            pass  # 没有更多消息
        finally:
            self.ws.sock.settimeout(self.ws.timeout)

    def _handle_event(self, event: dict):
        """实时处理 CDP 事件"""
        method = event.get("method")
        params = event.get("params", {})

        if method == "Runtime.executionContextCreated":
            ctx = params["context"]
            aux_data = ctx.get("auxData", {})
            frame_id = aux_data.get("frameId")
            is_default = aux_data.get("isDefault", False)
            if frame_id and is_default:
                self._frame_contexts[frame_id] = ctx["id"]

        elif method == "Runtime.executionContextDestroyed":
            # 清理已销毁的 context
            ctx_id = params.get("executionContextId")
            self._frame_contexts = {k: v for k, v in self._frame_contexts.items() if v != ctx_id}

        elif method == "Browser.downloadWillBegin":
            guid = params.get("guid")
            if guid:
                self._pending_downloads[guid] = params

        elif method == "Browser.downloadProgress":
            guid = params.get("guid")
            state = params.get("state")
            if guid and state == "completed":
                self._completed_downloads[guid] = params
                self._pending_downloads.pop(guid, None)

        elif method == "Page.javascriptDialogOpening":
            self._pending_dialog = params

    def close(self):
        self.ws.close()


class MiniBrowser:
    """简易浏览器自动化类"""

    # ==================== 初始化与生命周期 ====================

    def __init__(self, debug_port: int = 9222, timeout: float = 30):
        self.debug_port = debug_port
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self._sessions: dict[str, CDPSession] = {}  # target_id -> CDPSession
        self._current_target_id: Optional[str] = None
        self._frame_tree: dict = {}
        self._current_frame_id: Optional[str] = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def cdp(self) -> CDPSession:
        """获取当前标签页的CDP会话"""
        assert self._current_target_id is not None, "未连接浏览器"
        return self._sessions[self._current_target_id]

    def launch(self, kill_existing: bool = True, browser: str = "auto"):
        """启动浏览器并连接（使用默认profile，支持安全控件）

        Args:
            kill_existing: 是否先杀掉已有浏览器进程（默认True）
            browser: 浏览器选择 - "auto"（自动检测）| "chrome" | "edge" | 完整路径
        """
        # 确定浏览器路径和进程名
        if browser == "auto":
            browser_path = self._find_browser()
        elif browser == "chrome":
            browser_path = self._find_browser("chrome")
        elif browser == "edge":
            browser_path = self._find_browser("edge")
        else:
            browser_path = browser  # 用户传入完整路径

        # 根据路径判断进程名
        process_name = "msedge.exe" if "edge" in browser_path.lower() else "chrome.exe"

        if kill_existing:
            os.system(f"taskkill /f /im {process_name} 2>nul")
            # 轮询直到进程消失
            for _ in range(10):
                result = subprocess.run("tasklist", capture_output=True, text=True, shell=True)
                if process_name not in result.stdout:
                    break
                time.sleep(0.1)

        args = [
            browser_path,
            f"--remote-debugging-port={self.debug_port}",
        ]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 轮询直到能连接
        last_error = None
        for _ in range(20):
            try:
                self._connect()
                return
            except Exception as e:
                last_error = e
                time.sleep(0.1)
        raise Exception(f"浏览器启动超时: {last_error}")

    def _find_browser(self, browser_type: str = "auto") -> str:
        """查找浏览器路径

        Args:
            browser_type: "auto" | "chrome" | "edge"
        """
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
        else:  # auto: 优先 Chrome
            paths = chrome_paths + edge_paths

        for p in paths:
            if os.path.exists(p):
                return p
        raise Exception(f"未找到浏览器: {browser_type}")

    def connect(self):
        """连接到已运行的浏览器"""
        self._connect()

    def _get_targets(self) -> list:
        """获取所有调试目标"""
        conn = http.client.HTTPConnection("127.0.0.1", self.debug_port)
        conn.request("GET", "/json")
        response = conn.getresponse()
        targets = json.loads(response.read().decode())
        conn.close()
        return targets

    def _connect(self, target_id: Optional[str] = None):
        """内部连接方法"""
        targets = self._get_targets()

        # 找到目标页面
        page_target = None
        for target in targets:
            if target.get("type") == "page":
                if target_id is None or target.get("id") == target_id:
                    page_target = target
                    break

        if not page_target:
            raise Exception("未找到可用的页面")

        tid = page_target["id"]
        ws_url = page_target["webSocketDebuggerUrl"]
        session = CDPSession(ws_url, self.timeout)

        # 存储到字典并设置为当前
        self._sessions[tid] = session
        self._current_target_id = tid

        # 启用必要的域
        self.cdp.send("Page.enable")
        self.cdp.send("Runtime.enable")
        self.cdp.send("DOM.enable")

        # 获取frame树（执行上下文由 CDPSession 实时维护）
        self._update_frame_tree()

    # ==================== 页面导航 ====================

    def goto(self, url: str):
        """导航到URL"""
        self.cdp.send("Page.navigate", {"url": url})
        self.wait_for_load()
        self._update_frame_tree()

    def _update_frame_tree(self):
        """更新frame树"""
        result = self.cdp.send("Page.getFrameTree")
        self._frame_tree = result.get("frameTree", {})
        self._current_frame_id = self._frame_tree.get("frame", {}).get("id")

    def wait_for_load(self, timeout: float = 30):
        """等待页面加载完成"""
        start = time.time()
        while time.time() - start < timeout:
            result = self._evaluate("document.readyState")
            if result == "complete":
                return
            time.sleep(0.1)
        raise Exception("等待页面加载超时")

    def _evaluate(self, expression: str, return_by_value: bool = True) -> Any:
        """执行JavaScript表达式（在当前 frame 中）"""
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
        }

        # 如果有当前 frame 的 context，使用它（从 CDPSession 实时维护的数据中获取）
        if self._current_frame_id and self._current_frame_id in self.cdp._frame_contexts:
            params["contextId"] = self.cdp._frame_contexts[self._current_frame_id]

        result = self.cdp.send("Runtime.evaluate", params)

        if "exceptionDetails" in result:
            raise Exception(f"JS执行错误: {result['exceptionDetails']}")

        value = result.get("result", {})
        if return_by_value:
            return value.get("value")
        return value

    def _call_function(self, func: str, *args) -> Any:
        """调用JavaScript函数"""
        args_json = json.dumps(args)
        expression = f"({func}).apply(null, {args_json})"
        return self._evaluate(expression)

    # ==================== 元素查询 ====================

    def query_selector(self, selector: str) -> Optional[dict]:
        """查询元素，返回元素信息"""
        js = """
        function(selector) {
            const el = document.querySelector(selector);
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            // 使用 computedStyle 判断可见性，因为 getBoundingClientRect 在某些情况下返回 0
            const computedWidth = parseFloat(style.width) || 0;
            const computedHeight = parseFloat(style.height) || 0;
            const isVisible = style.display !== 'none' &&
                              style.visibility !== 'hidden' &&
                              (computedWidth > 0 || rect.width > 0);
            return {
                tagName: el.tagName,
                id: el.id,
                className: el.className,
                x: rect.x + rect.width / 2,
                y: rect.y + rect.height / 2,
                width: rect.width || computedWidth,
                height: rect.height || computedHeight,
                visible: isVisible
            };
        }
        """
        return self._call_function(js, selector)

    def wait_for_selector(self, selector: str, timeout: float = 10) -> dict:
        """等待元素出现"""
        start = time.time()
        while time.time() - start < timeout:
            elem = self.query_selector(selector)
            if elem and elem.get("visible"):
                return elem
            time.sleep(0.1)
        raise Exception(f"等待元素超时: {selector}")

    # ==================== 元素操作 ====================

    def click(self, selector: str):
        """点击元素"""
        elem = self.wait_for_selector(selector)
        x, y = elem["x"], elem["y"]

        # 模拟鼠标点击
        self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x, "y": y,
            "button": "left",
            "clickCount": 1
        })
        self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x, "y": y,
            "button": "left",
            "clickCount": 1
        })

    def type_text(self, selector: str, text: str, clear: bool = True):
        """在输入框中输入文本"""
        self.click(selector)
        time.sleep(0.1)

        if clear:
            # 清空现有内容
            js = f"document.querySelector({json.dumps(selector)}).value = ''"
            self._evaluate(js)

        # 逐字符输入
        for char in text:
            self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": char,
            })
            self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "text": char,
            })

    def fill(self, selector: str, value: str):
        """直接填充表单值（更快）"""
        self.wait_for_selector(selector)
        js = f"""
        (function(selector, value) {{
            const el = document.querySelector(selector);
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})({json.dumps(selector)}, {json.dumps(value)})
        """
        self._evaluate(js)

    def select(self, selector: str, value: str):
        """选择下拉框选项"""
        self.wait_for_selector(selector)
        js = f"""
        (function(selector, value) {{
            const el = document.querySelector(selector);
            el.value = value;
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})({json.dumps(selector)}, {json.dumps(value)})
        """
        self._evaluate(js)

    def check(self, selector: str, checked: bool = True):
        """勾选/取消勾选复选框"""
        self.wait_for_selector(selector)
        js = f"""
        (function(selector, checked) {{
            const el = document.querySelector(selector);
            if (el.checked !== checked) {{
                el.click();
            }}
        }})({json.dumps(selector)}, {json.dumps(checked)})
        """
        self._evaluate(js)

    # ==================== 元素读取 ====================

    def get_text(self, selector: str) -> str:
        """获取元素文本"""
        js = f"document.querySelector({json.dumps(selector)})?.textContent || ''"
        return self._evaluate(js)

    def get_value(self, selector: str) -> str:
        """获取输入框的值"""
        js = f"document.querySelector({json.dumps(selector)})?.value || ''"
        return self._evaluate(js)

    def get_attribute(self, selector: str, attr: str) -> str:
        """获取元素属性"""
        js = f"document.querySelector({json.dumps(selector)})?.getAttribute({json.dumps(attr)})"
        return self._evaluate(js)

    # ==================== iframe 支持 ====================

    def get_frames(self) -> list:
        """获取所有iframe列表"""
        frames = []
        self._collect_frames(self._frame_tree, frames)
        return frames

    def _collect_frames(self, node: dict, frames: list, depth: int = 0):
        """递归收集frame信息"""
        frame = node.get("frame", {})
        frames.append({
            "id": frame.get("id"),
            "name": frame.get("name", ""),
            "url": frame.get("url", ""),
            "depth": depth
        })
        for child in node.get("childFrames", []):
            self._collect_frames(child, frames, depth + 1)

    def switch_to_frame(self, selector: Optional[str] = None, name: Optional[str] = None, index: Optional[int] = None):
        """切换到iframe

        Args:
            selector: iframe的CSS选择器
            name: iframe的name属性
            index: iframe的索引（从0开始）
        """
        self._update_frame_tree()

        if selector:
            # 通过 CDP 直接获取 iframe 元素对应的 frameId
            result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
            object_id = result.get("objectId")
            if not object_id:
                raise Exception(f"未找到iframe: {selector}")

            node_info = self.cdp.send("DOM.describeNode", {"objectId": object_id})
            frame_id = node_info["node"].get("frameId")
            if not frame_id:
                raise Exception(f"元素不是iframe: {selector}")

            self._current_frame_id = frame_id
            return

        frames = self.get_frames()

        if name is not None:
            for frame in frames:
                if frame["name"] == name:
                    self._current_frame_id = frame["id"]
                    return
            raise Exception(f"未找到name为'{name}'的iframe")

        if index is not None:
            # 获取子frame（排除主frame）
            child_frames = [f for f in frames if f["depth"] > 0]
            if index >= len(child_frames):
                raise Exception(f"iframe索引越界: {index}")
            self._current_frame_id = child_frames[index]["id"]
            return

        raise Exception("必须指定selector、name或index之一")

    def switch_to_main_frame(self):
        """切换回主frame"""
        self._update_frame_tree()
        self._current_frame_id = self._frame_tree.get("frame", {}).get("id")

    # ==================== 其他实用方法 ====================

    def screenshot(self, path: Optional[str] = None) -> bytes:
        """截图"""
        result = self.cdp.send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(result["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def execute_script(self, script: str) -> Any:
        """执行自定义JavaScript"""
        return self._evaluate(script)

    def wait(self, seconds: float):
        """等待指定秒数"""
        time.sleep(seconds)

    def submit_form(self, selector: str):
        """提交表单"""
        js = f"document.querySelector({json.dumps(selector)})?.submit()"
        self._evaluate(js)

    def enable_download(self, download_path: str):
        """启用下载并指定保存目录

        注意：下载路径是浏览器全局设置，多标签页共享同一路径
        """
        self.cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_path,
            "eventsEnabled": True
        })

    def wait_for_download(self, timeout: float = 60) -> dict:
        """等待当前标签页的下载完成，返回下载信息"""
        my_frame_id = self._current_frame_id
        if not my_frame_id:
            raise Exception("未初始化frame，请先导航到页面")
        guid = None
        start = time.time()

        # 1. 找属于当前 frame 的下载，获取 guid
        while time.time() - start < timeout:
            self.cdp.poll_events()
            for g, info in list(self.cdp._pending_downloads.items()):
                if info.get("frameId") == my_frame_id:
                    guid = g
                    del self.cdp._pending_downloads[g]
                    break
            if guid:
                break
            time.sleep(0.1)

        if not guid:
            raise Exception("未检测到下载开始")

        # 2. 等待该 guid 的下载完成
        while time.time() - start < timeout:
            self.cdp.poll_events()
            if guid in self.cdp._completed_downloads:
                result = self.cdp._completed_downloads.pop(guid)
                return result
            time.sleep(0.1)

        raise Exception("下载超时")

    def upload_file(self, selector: str, file_path: str):
        """上传文件（支持 iframe）"""
        self.wait_for_selector(selector)

        # 在当前 frame 中获取元素的 objectId
        result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
        object_id = result.get("objectId")
        if not object_id:
            raise Exception(f"未找到文件输入元素: {selector}")

        # 将 objectId 转换为 backendNodeId
        node_info = self.cdp.send("DOM.describeNode", {"objectId": object_id})
        backend_node_id = node_info["node"]["backendNodeId"]

        # 设置文件
        self.cdp.send("DOM.setFileInputFiles", {
            "backendNodeId": backend_node_id,
            "files": [file_path]
        })

    # ==================== 多标签页 ====================

    def new_tab(self, url: str = "about:blank", switch: bool = False) -> str:
        """新建标签页，返回 target_id

        Args:
            url: 新标签页的 URL
            switch: 是否自动切换到新标签页
        """
        result = self.cdp.send("Target.createTarget", {"url": url})
        target_id = result["targetId"]

        if switch:
            time.sleep(0.1)  # 等待标签页准备好
            self.switch_tab(target_id)

        return target_id

    def get_tabs(self) -> list:
        """获取所有标签页"""
        result = self.cdp.send("Target.getTargets")
        return [t for t in result["targetInfos"] if t["type"] == "page"]

    def switch_tab(self, target_id: str):
        """切换到指定标签页"""
        # 如果该 tab 还没有连接，建立新连接
        if target_id not in self._sessions:
            self._connect(target_id)
        else:
            self._current_target_id = target_id
            self._update_frame_tree()

        # 激活窗口（让用户可见）
        self.cdp.send("Target.activateTarget", {"targetId": target_id})

    def close_tab(self, target_id: str):
        """关闭指定标签页"""
        # 先关闭浏览器标签页（用当前连接发送命令）
        self.cdp.send("Target.closeTarget", {"targetId": target_id})

        # 关闭该 tab 的 WebSocket 连接
        if target_id in self._sessions:
            self._sessions[target_id].close()
            del self._sessions[target_id]

        # 如果关闭的是当前 tab，切换到其他 tab
        if target_id == self._current_target_id:
            if self._sessions:
                self._current_target_id = next(iter(self._sessions))
                self._update_frame_tree()
            else:
                self._current_target_id = None

    # ==================== 弹窗处理 ====================

    def handle_dialog(self, accept: bool = True, prompt_text: str = ""):
        """处理弹窗（alert/confirm/prompt）"""
        self.cdp.send("Page.handleJavaScriptDialog", {
            "accept": accept,
            "promptText": prompt_text
        })

    def wait_for_dialog(self, timeout: float = 10) -> dict:
        """等待弹窗出现，返回弹窗信息"""
        start = time.time()
        while time.time() - start < timeout:
            self.cdp.poll_events()
            if self.cdp._pending_dialog:
                result = self.cdp._pending_dialog
                self.cdp._pending_dialog = None
                return result
            time.sleep(0.1)
        raise Exception("等待弹窗超时")

    # ==================== 更多等待条件 ====================

    def wait_for_text(self, text: str, timeout: float = 10):
        """等待页面出现指定文本"""
        start = time.time()
        while time.time() - start < timeout:
            content = self._evaluate("document.body.innerText || ''")
            if text in content:
                return
            time.sleep(0.1)
        raise Exception(f"等待文本超时: {text}")

    def wait_for_text_gone(self, text: str, timeout: float = 10):
        """等待指定文本消失"""
        start = time.time()
        while time.time() - start < timeout:
            content = self._evaluate("document.body.innerText || ''")
            if text not in content:
                return
            time.sleep(0.1)
        raise Exception(f"等待文本消失超时: {text}")

    def wait_for_selector_gone(self, selector: str, timeout: float = 10):
        """等待元素消失"""
        start = time.time()
        while time.time() - start < timeout:
            elem = self.query_selector(selector)
            if elem is None:
                return
            time.sleep(0.1)
        raise Exception(f"等待元素消失超时: {selector}")

    def wait_for_url(self, url_pattern: str, timeout: float = 10):
        """等待 URL 包含指定字符串"""
        start = time.time()
        while time.time() - start < timeout:
            current_url = self._evaluate("window.location.href")
            if url_pattern in current_url:
                return
            time.sleep(0.1)
        raise Exception(f"等待URL超时: {url_pattern}")

    def close(self):
        """关闭浏览器"""
        # 尝试发送关闭命令
        if self._sessions:
            try:
                self.cdp.send("Browser.close")
            except Exception:
                pass  # 浏览器可能已关闭

            # 关闭所有 WebSocket 连接
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    pass  # 连接可能已断开
            self._sessions.clear()
            self._current_target_id = None

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()