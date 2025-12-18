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
from dataclasses import dataclass, field


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

        if not response.startswith(b"HTTP/1.1 101"):
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
    """Chrome DevTools Protocol 会话 - 纯通信层"""

    def __init__(self, ws_url: str, timeout: float = 30):
        self.ws = WebSocketClient(ws_url, timeout)
        self.ws.connect()
        self._msg_id = 0
        self._responses = {}
        self._event_handlers: list = []

    def on_event(self, handler):
        """注册事件回调"""
        self._event_handlers.append(handler)

    def send(self, method: str, params: Optional[dict] = None) -> dict:
        """发送CDP命令并等待响应"""
        self._msg_id += 1
        msg_id = self._msg_id

        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params
        self.ws.send(json.dumps(message))

        while msg_id not in self._responses:
            try:
                data = self.ws.recv()
                msg = json.loads(data)

                if "id" in msg:
                    self._responses[msg["id"]] = msg
                else:
                    self._dispatch_event(msg)
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
                    self._dispatch_event(msg)
        except (socket.timeout, BlockingIOError):
            # socket.timeout: 阻塞模式下超时
            # BlockingIOError: 非阻塞模式下无数据可读 (timeout=0)
            pass
        finally:
            self.ws.sock.settimeout(self.ws.timeout)

    def _dispatch_event(self, event: dict):
        """分发事件给所有注册的处理器"""
        for handler in self._event_handlers:
            handler(event)

    def close(self):
        self.ws.close()


@dataclass
class TabState:
    """Tab 业务状态 - 集中管理所有状态"""
    # 页面状态
    frame_tree: dict = field(default_factory=dict)
    current_frame_id: Optional[str] = None
    # CDP 事件状态
    frame_contexts: dict[str, int] = field(default_factory=dict)  # frame_id -> context_id
    pending_downloads: dict[str, dict] = field(default_factory=dict)
    completed_downloads: dict[str, dict] = field(default_factory=dict)
    pending_dialog: Optional[dict] = None


class Tab:
    """单个标签页 - 包含所有页面操作方法"""

    def __init__(self, cdp: CDPSession, target_id: str):
        self.cdp = cdp
        self.target_id = target_id
        self.state = TabState()
        # 注册事件处理
        cdp.on_event(self._on_event)

    def _on_event(self, event: dict):
        """处理 CDP 事件，更新状态"""
        method = event.get("method")
        params = event.get("params", {})

        if method == "Runtime.executionContextCreated":
            ctx = params["context"]
            aux_data = ctx.get("auxData", {})
            frame_id = aux_data.get("frameId")
            is_default = aux_data.get("isDefault", False)
            if frame_id and is_default:
                self.state.frame_contexts[frame_id] = ctx["id"]

        elif method == "Runtime.executionContextDestroyed":
            ctx_id = params.get("executionContextId")
            self.state.frame_contexts = {
                k: v for k, v in self.state.frame_contexts.items() if v != ctx_id
            }

        elif method == "Browser.downloadWillBegin":
            guid = params.get("guid")
            if guid:
                self.state.pending_downloads[guid] = params

        elif method == "Browser.downloadProgress":
            guid = params.get("guid")
            state = params.get("state")
            if guid and state == "completed":
                self.state.completed_downloads[guid] = params
                self.state.pending_downloads.pop(guid, None)

        elif method == "Page.javascriptDialogOpening":
            self.state.pending_dialog = params

    # ==================== 页面导航 ====================

    def goto(self, url: str):
        """导航到URL"""
        self.cdp.send("Page.navigate", {"url": url})
        self.wait_for_load()
        self._update_frame_tree()

    def _update_frame_tree(self):
        """更新 frame 树"""
        result = self.cdp.send("Page.getFrameTree")
        self.state.frame_tree = result.get("frameTree", {})
        self.state.current_frame_id = self.state.frame_tree.get("frame", {}).get("id")

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
        # 先处理待接收事件，确保 context 映射是最新的
        self.cdp.poll_events(timeout=0)

        params = {
            "expression": expression,
            "returnByValue": return_by_value,
        }

        if self.state.current_frame_id and self.state.current_frame_id in self.state.frame_contexts:
            params["contextId"] = self.state.frame_contexts[self.state.current_frame_id]

        try:
            result = self.cdp.send("Runtime.evaluate", params)
        except Exception as e:
            # context 失效时（页面导航中），等待新 context 事件到达后重试
            if "Cannot find context" in str(e):
                self.cdp.poll_events(timeout=0.5)
                if self.state.current_frame_id and self.state.current_frame_id in self.state.frame_contexts:
                    params["contextId"] = self.state.frame_contexts[self.state.current_frame_id]
                else:
                    params.pop("contextId", None)
                result = self.cdp.send("Runtime.evaluate", params)
            else:
                raise

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

    def _scroll_into_view(self, selector: str):
        """滚动元素到视口内"""
        result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
        object_id = result.get("objectId")
        if object_id:
            self.cdp.send("DOM.scrollIntoViewIfNeeded", {"objectId": object_id})

    def click(self, selector: str):
        """点击元素"""
        self.wait_for_selector(selector)
        self._scroll_into_view(selector)
        elem = self.query_selector(selector)
        assert elem is not None
        x, y = elem["x"], elem["y"]

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

    def fill(self, selector: str, value: str):
        """填充表单值"""
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

    def switch_to_frame(self, selector: Optional[str] = None, name: Optional[str] = None, index: Optional[int] = None, timeout: float = 5.0):
        """切换到iframe"""
        self._update_frame_tree()

        if selector:
            result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
            object_id = result.get("objectId")
            if not object_id:
                raise Exception(f"未找到iframe: {selector}")

            node_info = self.cdp.send("DOM.describeNode", {"objectId": object_id})
            frame_id = node_info["node"].get("frameId")
            if not frame_id:
                raise Exception(f"元素不是iframe: {selector}")

            self.state.current_frame_id = frame_id

        elif name is not None:
            frames = []
            self._collect_frames(self.state.frame_tree, frames)
            for frame in frames:
                if frame["name"] == name:
                    self.state.current_frame_id = frame["id"]
                    break
            else:
                raise Exception(f"未找到name为'{name}'的iframe")

        elif index is not None:
            frames = []
            self._collect_frames(self.state.frame_tree, frames)
            child_frames = [f for f in frames if f["depth"] > 0]
            if index >= len(child_frames):
                raise Exception(f"iframe索引越界: {index}")
            self.state.current_frame_id = child_frames[index]["id"]

        else:
            raise Exception("必须指定selector、name或index之一")

        # 等待 iframe 的 context 就绪
        for _ in range(int(timeout * 10)):
            self.cdp.poll_events(timeout=0.1)
            if self.state.current_frame_id in self.state.frame_contexts:
                return
        raise Exception(f"iframe context 未就绪: {self.state.current_frame_id}")

    def switch_to_main_frame(self):
        """切换回主frame"""
        self._update_frame_tree()
        self.state.current_frame_id = self.state.frame_tree.get("frame", {}).get("id")

    # ==================== 其他操作 ====================

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

    def enable_download(self, download_path: str):
        """启用下载并指定保存目录（浏览器全局设置）"""
        self.cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_path,
            "eventsEnabled": True
        })

    def wait_for_download(self, timeout: float = 60) -> dict:
        """等待下载完成"""
        my_frame_id = self.state.current_frame_id
        if not my_frame_id:
            raise Exception("未初始化frame，请先导航到页面")
        guid = None
        start = time.time()

        while time.time() - start < timeout:
            self.cdp.poll_events()
            for g, info in list(self.state.pending_downloads.items()):
                if info.get("frameId") == my_frame_id:
                    guid = g
                    del self.state.pending_downloads[g]
                    break
            if guid:
                break
            time.sleep(0.1)

        if not guid:
            raise Exception("未检测到下载开始")

        while time.time() - start < timeout:
            self.cdp.poll_events()
            if guid in self.state.completed_downloads:
                result = self.state.completed_downloads.pop(guid)
                return result
            time.sleep(0.1)

        raise Exception("下载超时")

    def upload_file(self, selector: str, file_path: str):
        """上传文件"""
        self.wait_for_selector(selector)

        result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
        object_id = result.get("objectId")
        if not object_id:
            raise Exception(f"未找到文件输入元素: {selector}")

        node_info = self.cdp.send("DOM.describeNode", {"objectId": object_id})
        backend_node_id = node_info["node"]["backendNodeId"]

        self.cdp.send("DOM.setFileInputFiles", {
            "backendNodeId": backend_node_id,
            "files": [file_path]
        })

    # ==================== 弹窗处理 ====================

    def handle_dialog(self, accept: bool = True, prompt_text: str = ""):
        """处理弹窗（alert/confirm/prompt）"""
        self.cdp.send("Page.handleJavaScriptDialog", {
            "accept": accept,
            "promptText": prompt_text
        })

    def wait_for_dialog(self, timeout: float = 10) -> dict:
        """等待弹窗出现"""
        start = time.time()
        while time.time() - start < timeout:
            self.cdp.poll_events()
            if self.state.pending_dialog:
                result = self.state.pending_dialog
                self.state.pending_dialog = None
                return result
            time.sleep(0.1)
        raise Exception("等待弹窗超时")

    # ==================== 等待条件 ====================

    def wait_for_text(self, text: str, timeout: float = 10):
        """等待页面出现指定文本"""
        start = time.time()
        while time.time() - start < timeout:
            content = self._evaluate("document.body.innerText || ''")
            if text in content:
                return
            time.sleep(0.1)
        raise Exception(f"等待文本超时: {text}")

    def wait_for_url(self, url_pattern: str, timeout: float = 10):
        """等待 URL 包含指定字符串"""
        start = time.time()
        while time.time() - start < timeout:
            current_url = self._evaluate("window.location.href")
            if url_pattern in current_url:
                return
            time.sleep(0.1)
        raise Exception(f"等待URL超时: {url_pattern}")

    def activate(self):
        """激活此标签页（使其可见）"""
        self.cdp.send("Target.activateTarget", {"targetId": self.target_id})

    # ==================== 便捷方法 ====================

    def click_by_text(self, text: str, tag: str = "*", exact: bool = False, timeout: float = 10):
        """通过文本内容点击元素"""
        if exact:
            condition = f"el.textContent?.trim() === {json.dumps(text)}"
        else:
            condition = f"el.textContent?.includes({json.dumps(text)})"

        js = f"""
        (function() {{
            for (const el of document.querySelectorAll('{tag}')) {{
                if ({condition} && el.offsetParent !== null) {{
                    el.setAttribute('data-td-tmp', '1');
                    return true;
                }}
            }}
            return false;
        }})()
        """
        start = time.time()
        while time.time() - start < timeout:
            if self._evaluate(js):
                self.click("[data-td-tmp='1']")
                self._evaluate("document.querySelector('[data-td-tmp]')?.removeAttribute('data-td-tmp')")
                return
            time.sleep(0.1)
        raise Exception(f"未找到文本: {text}")

    def query_all(self, selector: str) -> list[dict]:
        """查询所有匹配的元素"""
        js = f"""
        Array.from(document.querySelectorAll({json.dumps(selector)})).map((el, i) => {{
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return {{
                index: i,
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                text: el.textContent?.trim().substring(0, 50) || '',
                visible: style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null,
                x: rect.x + rect.width / 2,
                y: rect.y + rect.height / 2
            }};
        }})
        """
        return self._evaluate(js) or []

    def click_all(self, selector: str, delay: float = 0.3) -> int:
        """点击所有匹配的元素，返回点击数量"""
        elements = self.query_all(selector)
        visible = [el for el in elements if el.get("visible")]
        count = 0
        for _ in visible:
            try:
                self._scroll_into_view(selector)
                # 重新获取坐标（滚动后可能变化）
                fresh = self.query_all(selector)
                if count < len(fresh) and fresh[count].get("visible"):
                    x, y = fresh[count]["x"], fresh[count]["y"]
                    self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                    self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    count += 1
                    time.sleep(delay)
            except Exception:
                break
        return count

    def find_by_text(self, text: str, tag: str = "*", exact: bool = False) -> Optional[dict]:
        """通过文本查找元素，返回元素信息"""
        if exact:
            condition = f"el.textContent?.trim() === {json.dumps(text)}"
        else:
            condition = f"el.textContent?.includes({json.dumps(text)})"

        js = f"""
        (function() {{
            for (const el of document.querySelectorAll('{tag}')) {{
                if ({condition}) {{
                    const rect = el.getBoundingClientRect();
                    return {{
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                        class: el.className || null,
                        text: el.textContent?.trim().substring(0, 100),
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2
                    }};
                }}
            }}
            return null;
        }})()
        """
        return self._evaluate(js)

    def count(self, selector: str) -> int:
        """统计匹配元素数量"""
        return self._evaluate(f"document.querySelectorAll({json.dumps(selector)}).length") or 0

    def hover(self, selector: str):
        """悬停在元素上"""
        self.wait_for_selector(selector)
        self._scroll_into_view(selector)
        elem = self.query_selector(selector)
        assert elem is not None
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": elem["x"], "y": elem["y"]})

    def double_click(self, selector: str):
        """双击元素"""
        self.wait_for_selector(selector)
        self._scroll_into_view(selector)
        elem = self.query_selector(selector)
        assert elem is not None
        x, y = elem["x"], elem["y"]
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 2})
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 2})

    def select_by_text(self, selector: str, text: str):
        """通过选项文本选择下拉框"""
        self.wait_for_selector(selector)
        js = f"""
        (function() {{
            const sel = document.querySelector({json.dumps(selector)});
            if (!sel) return false;
            for (const opt of sel.options) {{
                if (opt.text?.includes({json.dumps(text)})) {{
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
            }}
            return false;
        }})()
        """
        if not self._evaluate(js):
            raise Exception(f"未找到选项: {text}")

    def click_js(self, selector: str):
        """通过 JS click() 点击元素（更可靠，适合小图标等）"""
        self.wait_for_selector(selector)
        clicked = self._evaluate(f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (el) {{ el.click(); return true; }}
            return false;
        }})()
        """)
        if not clicked:
            raise Exception(f"点击失败: {selector}")

    def get_options(self, selector: str) -> list[dict]:
        """获取下拉框的所有选项"""
        js = f"""
        (function() {{
            const sel = document.querySelector({json.dumps(selector)});
            if (!sel) return [];
            return Array.from(sel.options).map(o => ({{
                value: o.value,
                text: o.text,
                selected: o.selected
            }}));
        }})()
        """
        return self._evaluate(js) or []

    def get_selected_text(self, selector: str) -> str:
        """获取下拉框当前选中的文本"""
        js = f"document.querySelector({json.dumps(selector)})?.selectedOptions[0]?.text || ''"
        return self._evaluate(js)

    def is_checked(self, selector: str) -> bool:
        """检查复选框是否选中"""
        js = f"document.querySelector({json.dumps(selector)})?.checked || false"
        return self._evaluate(js)


class MiniBrowser:
    """浏览器管理器"""

    def __init__(self, debug_port: int = 9222, timeout: float = 30):
        self.debug_port = debug_port
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self._tabs: dict[str, Tab] = {}  # target_id -> Tab

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ==================== 启动与连接 ====================

    def launch(self, kill_existing: bool = True, browser: str = "auto") -> 'Tab':
        """启动浏览器并返回初始标签页"""
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
        for _ in range(20):
            try:
                return self._connect_first_tab()
            except Exception as e:
                last_error = e
                time.sleep(0.1)
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

    def connect(self) -> 'Tab':
        """连接到已运行的浏览器，返回第一个标签页"""
        return self._connect_first_tab()

    def _get_targets(self) -> list:
        """获取所有调试目标"""
        conn = http.client.HTTPConnection("127.0.0.1", self.debug_port)
        conn.request("GET", "/json")
        response = conn.getresponse()
        targets = json.loads(response.read().decode())
        conn.close()
        return targets

    def _connect_first_tab(self) -> 'Tab':
        """连接到第一个可用的标签页"""
        targets = self._get_targets()

        page_target = None
        for target in targets:
            if target.get("type") == "page":
                page_target = target
                break

        if not page_target:
            raise Exception("未找到可用的页面")

        return self._create_tab(page_target)

    def _create_tab(self, target: dict) -> 'Tab':
        """根据 target 信息创建 Tab 对象"""
        tid = target["id"]
        ws_url = target["webSocketDebuggerUrl"]

        cdp = CDPSession(ws_url, self.timeout)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("DOM.enable")

        tab = Tab(cdp, tid)
        tab._update_frame_tree()

        self._tabs[tid] = tab
        return tab

    # ==================== 标签页管理 ====================

    def new_tab(self, url: str = "about:blank") -> 'Tab':
        """新建标签页并返回 Tab 对象"""
        # 使用任意现有 tab 的 cdp 发送命令
        if not self._tabs:
            raise Exception("没有可用的标签页")

        any_tab = next(iter(self._tabs.values()))
        result = any_tab.cdp.send("Target.createTarget", {"url": url})
        target_id = result["targetId"]

        time.sleep(0.1)  # 等待标签页准备好

        # 获取新标签页的 WebSocket URL
        targets = self._get_targets()
        for target in targets:
            if target.get("id") == target_id:
                return self._create_tab(target)

        raise Exception("无法连接到新标签页")

    def get_tabs(self) -> list['Tab']:
        """获取所有已连接的标签页"""
        return list(self._tabs.values())

    def close_tab(self, tab: 'Tab'):
        """关闭指定标签页"""
        tab.cdp.send("Target.closeTarget", {"targetId": tab.target_id})
        tab.cdp.close()
        self._tabs.pop(tab.target_id, None)

    def close(self):
        """关闭浏览器"""
        if self._tabs:
            try:
                any_tab = next(iter(self._tabs.values()))
                any_tab.cdp.send("Browser.close")
            except Exception:
                pass

            for tab in self._tabs.values():
                try:
                    tab.cdp.close()
                except Exception:
                    pass
            self._tabs.clear()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
