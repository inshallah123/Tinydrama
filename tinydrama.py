"""
Mini Browser Automation - 基于Python标准库的简易浏览器自动化工具
仅使用标准库实现，通过Chrome DevTools Protocol (CDP) 控制浏览器
"""

import socket
import struct
import hashlib
import base64
import json
import subprocess
import time
import http.client
import re
import os
from urllib.parse import urlparse
from typing import Optional, Any


class WebSocketClient:
    """简易WebSocket客户端实现"""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str):
        self.url = url
        parsed = urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.sock: Optional[socket.socket] = None

    def connect(self):
        """建立WebSocket连接"""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(30)

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
        fin = header[0] & 0x80
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
        """关闭连接"""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass


class CDPSession:
    """Chrome DevTools Protocol 会话"""

    def __init__(self, ws_url: str):
        self.ws = WebSocketClient(ws_url)
        self.ws.connect()
        self._msg_id = 0
        self._responses = {}
        self._events = []

    def send(self, method: str, params: dict = None) -> dict:
        """发送CDP命令并等待响应"""
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
                    # 事件消息
                    self._events.append(msg)
            except socket.timeout:
                raise Exception(f"等待响应超时: {method}")

        response = self._responses.pop(msg_id)
        if "error" in response:
            raise Exception(f"CDP错误: {response['error']}")

        return response.get("result", {})

    def close(self):
        self.ws.close()


class MiniBrowser:
    """简易浏览器自动化类"""

    def __init__(self, debug_port: int = 9222):
        self.debug_port = debug_port
        self.process: Optional[subprocess.Popen] = None
        self.session: Optional[CDPSession] = None
        self._frame_tree = {}
        self._current_frame_id = None
        self._execution_context_id = None

    def launch(self, browser_path: str = None, headless: bool = False):
        """启动浏览器"""
        if browser_path is None:
            # 尝试常见的Chrome/Edge路径
            paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
            for p in paths:
                if os.path.exists(p):
                    browser_path = p
                    break

        if not browser_path:
            raise Exception("未找到浏览器，请手动指定browser_path")

        args = [
            browser_path,
            f"--remote-debugging-port={self.debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,800",
            "--window-position=0,0",
            f"--user-data-dir={os.path.join(os.environ.get('TEMP', '/tmp'), 'mini_browser_profile')}",
        ]

        if headless:
            args.append("--headless=new")

        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)  # 等待浏览器启动

        self._connect()

    def connect(self, debug_port: int = None):
        """连接到已运行的浏览器"""
        if debug_port:
            self.debug_port = debug_port
        self._connect()

    def _connect(self):
        """内部连接方法"""
        # 获取调试端点
        conn = http.client.HTTPConnection("127.0.0.1", self.debug_port)
        conn.request("GET", "/json")
        response = conn.getresponse()
        targets = json.loads(response.read().decode())
        conn.close()

        # 找到页面target
        page_target = None
        for target in targets:
            if target.get("type") == "page":
                page_target = target
                break

        if not page_target:
            raise Exception("未找到可用的页面")

        ws_url = page_target["webSocketDebuggerUrl"]
        self.session = CDPSession(ws_url)

        # 启用必要的域
        self.session.send("Page.enable")
        self.session.send("Runtime.enable")
        self.session.send("DOM.enable")

        # 获取frame树
        self._update_frame_tree()

    def _update_frame_tree(self):
        """更新frame树"""
        result = self.session.send("Page.getFrameTree")
        self._frame_tree = result.get("frameTree", {})
        self._current_frame_id = self._frame_tree.get("frame", {}).get("id")
        self._update_execution_context()

    def _update_execution_context(self):
        """更新当前frame的执行上下文"""
        # 创建一个隔离的执行上下文
        result = self.session.send("Page.createIsolatedWorld", {
            "frameId": self._current_frame_id,
            "worldName": "mini_browser"
        })
        self._execution_context_id = result.get("executionContextId")

    def goto(self, url: str, wait_until: str = "load"):
        """导航到URL"""
        self.session.send("Page.navigate", {"url": url})
        self.wait_for_load()
        self._update_frame_tree()

    def wait_for_load(self, timeout: float = 30):
        """等待页面加载完成"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                # 直接在主世界中执行，不传 contextId，避免导航后隔离上下文失效的问题
                result = self.session.send("Runtime.evaluate", {
                    "expression": "document.readyState",
                    "returnByValue": True
                })
                if result.get("result", {}).get("value") == "complete":
                    return
            except:
                pass
            time.sleep(0.1)
        raise Exception("等待页面加载超时")

    def _evaluate(self, expression: str, return_by_value: bool = True, use_isolated_context: bool = False) -> Any:
        """执行JavaScript表达式"""
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
        }
        # 默认在主世界中执行，仅在明确需要时使用隔离上下文
        if use_isolated_context and self._execution_context_id:
            params["contextId"] = self._execution_context_id

        result = self.session.send("Runtime.evaluate", params)

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
            try:
                elem = self.query_selector(selector)
                if elem and elem.get("visible"):
                    return elem
            except:
                pass
            time.sleep(0.1)
        raise Exception(f"等待元素超时: {selector}")

    def click(self, selector: str):
        """点击元素"""
        elem = self.wait_for_selector(selector)
        x, y = elem["x"], elem["y"]

        # 模拟鼠标点击
        self.session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x, "y": y,
            "button": "left",
            "clickCount": 1
        })
        self.session.send("Input.dispatchMouseEvent", {
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
            self.session.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": char,
            })
            self.session.send("Input.dispatchKeyEvent", {
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

    def switch_to_frame(self, selector: str = None, name: str = None, index: int = None):
        """切换到iframe

        Args:
            selector: iframe的CSS选择器
            name: iframe的name属性
            index: iframe的索引（从0开始）
        """
        self._update_frame_tree()

        if selector:
            # 通过选择器找到iframe
            js = f"""
            (function(selector) {{
                const iframe = document.querySelector(selector);
                if (!iframe) return null;
                return iframe.contentWindow ? true : false;
            }})({json.dumps(selector)})
            """
            if not self._evaluate(js):
                raise Exception(f"未找到iframe: {selector}")

            # 获取iframe的frame id
            js = f"""
            (function(selector) {{
                const iframe = document.querySelector(selector);
                return iframe ? iframe.name || iframe.id : null;
            }})({json.dumps(selector)})
            """
            frame_name = self._evaluate(js)
            name = frame_name if frame_name else None

        frames = self.get_frames()

        if name is not None:
            for frame in frames:
                if frame["name"] == name:
                    self._current_frame_id = frame["id"]
                    self._update_execution_context()
                    return
            raise Exception(f"未找到name为'{name}'的iframe")

        if index is not None:
            # 获取子frame（排除主frame）
            child_frames = [f for f in frames if f["depth"] > 0]
            if index >= len(child_frames):
                raise Exception(f"iframe索引越界: {index}")
            self._current_frame_id = child_frames[index]["id"]
            self._update_execution_context()
            return

        raise Exception("必须指定selector、name或index之一")

    def switch_to_main_frame(self):
        """切换回主frame"""
        self._update_frame_tree()
        self._current_frame_id = self._frame_tree.get("frame", {}).get("id")
        self._update_execution_context()

    # ==================== 其他实用方法 ====================

    def screenshot(self, path: str = None) -> bytes:
        """截图"""
        result = self.session.send("Page.captureScreenshot", {"format": "png"})
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

    def close(self):
        """关闭浏览器"""
        if self.session:
            try:
                self.session.send("Browser.close")
            except:
                pass
            self.session.close()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()


# 便捷函数
def create_browser(headless: bool = False, debug_port: int = 9222) -> MiniBrowser:
    """创建并启动浏览器"""
    browser = MiniBrowser(debug_port)
    browser.launch(headless=headless)
    return browser


if __name__ == "__main__":
    # 使用示例
    print("Mini Browser 自动化示例")
    print("=" * 40)

    browser = create_browser(headless=False)

    try:
        # 访问百度
        browser.goto("https://www.baidu.com")
        print("已打开百度")

        # 输入搜索内容 - 使用百度新版的 chat-textarea
        browser.fill("#chat-textarea", "Python自动化")
        print("已输入搜索内容")

        # 按回车搜索
        browser.session.send("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13
        })
        browser.session.send("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13
        })
        print("已按回车搜索")

        # 等待结果
        browser.wait(2)

        # 截图
        browser.screenshot("search_result.png")
        print("已保存截图: search_result.png")

    finally:
        browser.close()
        print("浏览器已关闭")
