"""
Frame 模块 - 页面操作的核心

包含 Frame（统一表示主页面和 iframe）和 FrameManager（事件管理）。
"""

import json
import time
import base64
from typing import Optional, Any
from .cdp import CDPSession, CDPError


class Frame:
    """Frame - 统一表示主页面和 iframe

    - frame_id: 固定标识
    - session_id: 跨域 iframe 有独立值，同源 iframe 为 None
    - context_id: 仅同源 iframe 需要（用于区分和主页面的执行环境）

    根 frame 和跨域 iframe 不指定 contextId，让浏览器自动使用默认 context。
    """

    def __init__(self, manager: 'FrameManager', frame_id: str, parent: Optional['Frame'] = None, target_id: Optional[str] = None, owner_selector: Optional[str] = None):
        self._manager = manager
        self._frame_id = frame_id
        self._parent = parent
        self._target_id = target_id  # 只有根 frame 有
        self._owner_selector = owner_selector  # iframe 在父 frame 中的 selector
        # 动态状态 - 由 FrameManager 通过事件更新
        self._context_id: Optional[int] = None
        self._session_id: Optional[str] = None  # 跨域 iframe 才有

    @property
    def is_root(self) -> bool:
        """是否是根 frame（即 Tab）"""
        return self._parent is None

    @property
    def cdp(self) -> CDPSession:
        return self._manager._cdp

    # ==================== 内部方法（由 FrameManager 调用）====================

    def _set_context(self, context_id: int, session_id: Optional[str] = None):
        """设置执行上下文（由 FrameManager 调用）
        注意：session_id 始终更新，因为 iframe 导航可能导致跨域状态变化
        （同源 → 跨域 或 跨域 → 同源）
        """
        self._context_id = context_id
        self._session_id = session_id

    def _needs_context_id(self) -> bool:
        """是否需要指定 contextId（同源 iframe 需要，根 frame 和跨域 iframe 不需要）"""
        # 根 frame：不需要，使用页面默认 context
        # 跨域 iframe：有独立 session，不需要，使用该 session 默认 context
        # 同源 iframe：共享主页面 session，需要 contextId 来区分
        return not self.is_root and self._session_id is None

    def _ensure_context(self, timeout: float = 10.0):
        """确保 context 已就绪（仅同源 iframe 需要）"""
        if not self._needs_context_id():
            return

        if self._context_id is not None:
            return

        start = time.time()
        while time.time() - start < timeout:
            self._manager._cdp.poll_events(timeout=0.1)
            self._manager._flush_pending_enables()
            if self._context_id is not None:
                return

        raise Exception(f"Frame context 未就绪: {self._frame_id}")

    def _refresh_state(self, timeout: float = 5.0):
        """刷新 frame 状态（处理 iframe 导航后的 context/session 变化）
        当 iframe 内部导航导致跨域状态变化时：
        - 同源 → 跨域：需要获取新的 session_id
        - 跨域 → 同源：session_id 变为 None，需要 context_id
        - 跨域 → 跨域(不同源)：需要获取新的 session_id
        """
        # 清空当前状态，等待事件更新
        self._context_id = None
        self._session_id = None

        start = time.time()
        while time.time() - start < timeout:
            self._manager._cdp.poll_events(timeout=0.1)
            self._manager._flush_pending_enables()
            # 等待 context 被设置（无论是同源还是跨域，都会有 context 事件）
            if self._context_id is not None:
                return

        raise Exception(f"Frame 状态刷新超时: {self._frame_id}")

    def _get_viewport_offset(self) -> tuple[float, float]:
        """获取当前 frame 相对于主视口的偏移量（用于 iframe 内的鼠标操作）"""
        if self._parent is None:
            return (0.0, 0.0)

        # 递归获取父 frame 的偏移量
        parent_offset = self._parent._get_viewport_offset()

        # 从父 frame 查询 iframe 元素的位置
        if self._owner_selector:
            rect = self._parent._evaluate(f"""
            (function() {{
                const el = document.querySelector({json.dumps(self._owner_selector)});
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                return {{ x: rect.x, y: rect.y }};
            }})()
            """)
            if rect:
                return (parent_offset[0] + rect["x"], parent_offset[1] + rect["y"])

        return parent_offset

    # ==================== JavaScript 执行 ====================

    def _evaluate(self, expression: str, return_by_value: bool = True) -> Any:
        """执行 JavaScript 表达式

        - 根 frame / 跨域 iframe：不指定 contextId，让浏览器使用默认 context
        - 同源 iframe：需要指定 contextId 来区分和主页面
        """
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
        }
        # 同源 iframe 需要指定 contextId
        if self._needs_context_id():
            self._ensure_context()
            params["contextId"] = self._context_id

        try:
            result = self.cdp.send("Runtime.evaluate", params, session_id=self._session_id)
        except CDPError as e:
            # Context/Session 失效时自动恢复（iframe 导航可能导致跨域状态变化）
            # -32000: Context 错误 (Cannot find context, Execution context was destroyed)
            # -32001: Session 错误 (Session with given id not found, No target with given id)
            if e.code in (-32000, -32001):
                self._refresh_state()
                # 重新构建 params（跨域状态可能已变化）
                params = {"expression": expression, "returnByValue": return_by_value}
                if self._needs_context_id():
                    params["contextId"] = self._context_id
                result = self.cdp.send("Runtime.evaluate", params, session_id=self._session_id)
            else:
                raise

        if "exceptionDetails" in result:
            raise Exception(f"JS执行错误: {result['exceptionDetails']}")

        value = result.get("result", {})
        if return_by_value:
            return value.get("value")
        return value

    # ==================== 页面导航 ====================

    def goto(self, url: str):
        """导航到 URL"""
        params = {"url": url}
        # 非根 frame 需要指定 frameId
        if not self.is_root:
            params["frameId"] = self._frame_id
        self.cdp.send("Page.navigate", params, session_id=self._session_id)
        self.wait_for_load()

    def wait_for_load(self, timeout: float = 30):
        """等待页面加载完成"""
        start = time.time()
        while time.time() - start < timeout:
            result = self._evaluate("document.readyState")
            if result == "complete":
                return
            time.sleep(0.1)
        raise Exception("等待页面加载超时")

    # ==================== 元素查询 ====================

    def query_selector(self, selector: str) -> Optional[dict]:
        """查询元素，返回元素信息"""
        js = f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const computedWidth = parseFloat(style.width) || 0;
            const computedHeight = parseFloat(style.height) || 0;
            const isVisible = style.display !== 'none' &&
                              style.visibility !== 'hidden' &&
                              (computedWidth > 0 || rect.width > 0);
            return {{
                tagName: el.tagName,
                id: el.id,
                className: el.className,
                x: rect.x + rect.width / 2,
                y: rect.y + rect.height / 2,
                width: rect.width || computedWidth,
                height: rect.height || computedHeight,
                visible: isVisible
            }};
        }})()
        """
        return self._evaluate(js)

    def wait_for_selector(self, selector: str, timeout: float = 10) -> dict:
        """等待元素出现"""
        start = time.time()
        while time.time() - start < timeout:
            elem = self.query_selector(selector)
            if elem and elem.get("visible"):
                return elem
            time.sleep(0.1)
        raise Exception(f"等待元素超时: {selector}")

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
                text: (el.textContent ? el.textContent.trim().substring(0, 50) : ''),
                visible: style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null,
                x: rect.x + rect.width / 2,
                y: rect.y + rect.height / 2
            }};
        }})
        """
        result = self._evaluate(js)
        if result is None:
            raise Exception(f"查询元素失败: {selector}")
        return result


    # ==================== 元素操作 ====================

    def _scroll_into_view(self, selector: str):
        """滚动元素到视口内"""
        result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
        object_id = result.get("objectId")
        if not object_id:
            raise Exception(f"无法获取元素 objectId: {selector}")
        self.cdp.send("DOM.scrollIntoViewIfNeeded", {"objectId": object_id}, session_id=self._session_id)

    def _dispatch_mouse(self, selector: str, click_count: int = 1):
        """分发原生鼠标事件"""
        self._scroll_into_view(selector)
        elem = self.query_selector(selector)
        assert elem is not None
        offset_x, offset_y = self._get_viewport_offset()
        x, y = elem["x"] + offset_x, elem["y"] + offset_y
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": click_count})
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": click_count})

    def click(self, selector: str, native: bool = False):
        """点击元素

        Args:
            selector: CSS 选择器
            native: 是否使用原生鼠标事件（默认 False 使用 JS 点击，更可靠）
        """
        self.wait_for_selector(selector)
        if native:
            self._dispatch_mouse(selector, 1)
        else:
            clicked = self._evaluate(f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (el) {{ el.click(); return true; }}
                return false;
            }})()
            """)
            if not clicked:
                raise Exception(f"点击失败: {selector}")

    def click_by_text(self, text: str, tag: str = "*", exact: bool = False,
                      timeout: float = 10, native: bool = False):
        """通过文本内容点击元素

        Args:
            text: 要匹配的文本
            tag: HTML 标签过滤（默认 * 匹配所有）
            exact: 是否精确匹配（默认 False 为包含匹配）
            timeout: 超时时间（秒）
            native: 是否使用原生鼠标事件
        """
        if exact:
            condition = f"el.textContent && el.textContent.trim() === {json.dumps(text)}"
        else:
            condition = f"el.textContent && el.textContent.includes({json.dumps(text)})"

        js = f"""
        (function() {{
            const matches = [];
            for (const el of document.querySelectorAll('{tag}')) {{
                if ({condition} && el.offsetParent !== null) {{
                    matches.push(el);
                }}
            }}
            if (matches.length === 1) {{
                matches[0].setAttribute('data-td-tmp', '1');
            }}
            return matches.length;
        }})()
        """
        start = time.time()
        while time.time() - start < timeout:
            count = self._evaluate(js)
            if count == 1:
                self.click("[data-td-tmp='1']", native=native)
                self._evaluate("document.querySelector('[data-td-tmp]')?.removeAttribute('data-td-tmp')")
                return
            elif count > 1:
                raise Exception(f"文本 '{text}' 匹配到 {count} 个元素，请使用更精确的选择器")
            time.sleep(0.1)
        raise Exception(f"未找到文本: {text}")

    def double_click(self, selector: str, native: bool = False):
        """双击元素

        Args:
            selector: CSS 选择器
            native: 是否使用原生鼠标事件（默认 False 使用 JS）
        """
        self.wait_for_selector(selector)
        if native:
            self._dispatch_mouse(selector, 2)
        else:
            self._evaluate(f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (el) el.dispatchEvent(new MouseEvent('dblclick', {{ bubbles: true }}));
            }})()
            """)

    def hover(self, selector: str):
        """悬停在元素上（使用原生鼠标事件，因为 JS 无法触发 CSS :hover）"""
        self.wait_for_selector(selector)
        self._scroll_into_view(selector)
        elem = self.query_selector(selector)
        assert elem is not None
        offset_x, offset_y = self._get_viewport_offset()
        x, y = elem["x"] + offset_x, elem["y"] + offset_y
        self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def fill(self, selector: str, value: str, native: bool = False):
        """填充表单值

        Args:
            selector: CSS 选择器
            value: 要填充的值
            native: 是否使用原生键盘输入（默认 False，React/Vue 等框架设为 True）
        """
        self.wait_for_selector(selector)
        if native:
            self.click(selector, native=True)
            # Ctrl+A 全选 + Backspace 清空
            self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2
            }, session_id=self._session_id)
            self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a", "code": "KeyA"
            }, session_id=self._session_id)
            self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8
            }, session_id=self._session_id)
            self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Backspace", "code": "Backspace"
            }, session_id=self._session_id)
            self.cdp.send("Input.insertText", {"text": value}, session_id=self._session_id)
        else:
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

    def select(self, selector: str, *, value: Optional[str] = None, text: Optional[str] = None):
        """选择下拉框选项

        Args:
            selector: CSS 选择器
            value: 通过 value 属性选择
            text: 通过选项文本选择（模糊匹配）
        注意：value 和 text 必须指定其一
        """
        if value is None and text is None:
            raise ValueError("必须指定 value 或 text")

        self.wait_for_selector(selector)

        if value is not None:
            js = f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                el.value = {json.dumps(value)};
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()
            """
            self._evaluate(js)
        else:
            js = f"""
            (function() {{
                const sel = document.querySelector({json.dumps(selector)});
                if (!sel) return false;
                for (const opt of sel.options) {{
                    if (opt.text && opt.text.includes({json.dumps(text)})) {{
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
        js = f"(function(){{var el=document.querySelector({json.dumps(selector)});return el?el.textContent:''}})()"
        return self._evaluate(js)

    def get_value(self, selector: str) -> str:
        """获取输入框的值"""
        js = f"(function(){{var el=document.querySelector({json.dumps(selector)});return el?el.value:''}})()"
        return self._evaluate(js)

    def get_attribute(self, selector: str, attr: str) -> str:
        """获取元素属性"""
        js = f"(function(){{var el=document.querySelector({json.dumps(selector)});return el?el.getAttribute({json.dumps(attr)}):null}})()"
        return self._evaluate(js)

    def is_checked(self, selector: str) -> bool:
        """检查复选框是否选中"""
        js = f"(function(){{var el=document.querySelector({json.dumps(selector)});return el?el.checked:false}})()"
        return self._evaluate(js)

    # ==================== iframe 操作 ====================

    def iframe(self, selector: str, timeout: float = 5.0) -> 'Frame':
        """获取 iframe 的 Frame 对象

        Args:
            selector: iframe 元素的 CSS 选择器
            timeout: 等待 iframe context 就绪的超时时间

        Returns:
            iframe 对应的 Frame 对象
        """
        self.wait_for_selector(selector)

        # 获取 iframe 元素的 frameId
        result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
        object_id = result.get("objectId")
        if not object_id:
            raise Exception(f"未找到 iframe: {selector}")

        node_info = self.cdp.send("DOM.describeNode", {"objectId": object_id}, session_id=self._session_id)
        frame_id = node_info["node"].get("frameId")
        if not frame_id:
            raise Exception(f"元素不是 iframe: {selector}")

        # 获取或创建 Frame 对象
        child_frame = self._manager._ensure_frame(frame_id, parent=self, owner_selector=selector)

        # 等待 context 就绪
        child_frame._ensure_context(timeout)

        return child_frame

    # ==================== 截图 ====================

    def screenshot(self, path: Optional[str] = None) -> bytes:
        """截图"""
        result = self.cdp.send("Page.captureScreenshot", {"format": "png"}, session_id=self._session_id)
        data = base64.b64decode(result["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    # ==================== 脚本执行 ====================

    def execute_script(self, script: str) -> Any:
        """执行自定义 JavaScript"""
        return self._evaluate(script)

    # ==================== 文件操作 ====================

    def upload_file(self, selector: str, file_path: str):
        """上传文件"""
        self.wait_for_selector(selector)

        result = self._evaluate(f"document.querySelector({json.dumps(selector)})", return_by_value=False)
        object_id = result.get("objectId")
        if not object_id:
            raise Exception(f"未找到文件输入元素: {selector}")

        node_info = self.cdp.send("DOM.describeNode", {"objectId": object_id}, session_id=self._session_id)
        backend_node_id = node_info["node"]["backendNodeId"]

        self.cdp.send("DOM.setFileInputFiles", {
            "backendNodeId": backend_node_id,
            "files": [file_path]
        }, session_id=self._session_id)

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

    # ==================== Tab 级操作 ====================

    def activate(self):
        """激活此标签页（使其可见）- 仅根 frame"""
        if not self._target_id:
            raise Exception("只有根 frame 可以 activate")
        self.cdp.send("Target.activateTarget", {"targetId": self._target_id})

    # ==================== 弹窗功能 ====================

    def handle_dialog(self, accept: bool = True, prompt_text: str = ""):
        """处理弹窗（alert/confirm/prompt）

        处理当前 frame 触发的弹窗。
        """
        self._manager._handle_dialog(self._frame_id, accept, prompt_text)

    def wait_for_dialog(self, timeout: float = 10) -> dict:
        """等待弹窗出现

        等待当前 frame 触发的弹窗（与 CDP frameId 一致）。

        Returns:
            弹窗信息，包含 type, message, frameId 等
        """
        return self._manager._wait_for_dialog(self._frame_id, timeout)


class FrameManager:
    """Frame 管理器 - 集中管理事件监听和 Frame 对象"""

    # ==================== 初始化 ====================

    def __init__(self, cdp: CDPSession, target_id: str):
        self._cdp = cdp
        self._target_id = target_id # 调试对象，TAB/跨域 iframe
        self._frames: dict[str, Frame] = {}  # frame_id -> Frame
        self._pending_sessions: dict[str, dict] = {}  # session_id -> target_info
        self._sessions_to_enable: list[str] = []  # 待启用 Runtime 的 session
        self._pending_contexts: dict[str, tuple[int, Optional[str]]] = {}  # frame_id -> (context_id, session_id)
        self._pending_dialogs: dict[str, dict] = {}  # frame_id -> dialog params
        cdp.on_event(self._handle_event)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("DOM.enable")
        cdp.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
            "filter": [{"type": "iframe", "exclude": False}]
        })

    def get_main_frame(self) -> Frame:
        """获取主 frame"""
        result = self._cdp.send("Page.getFrameTree")
        main_frame = result.get("frameTree", {}).get("frame", {})
        if not main_frame.get("id"):
            raise Exception("无法获取主 frame")
        return self._ensure_frame(main_frame["id"], target_id=self._target_id)

    def _ensure_frame(self, frame_id: str, parent: Optional[Frame] = None,
                      target_id: Optional[str] = None, owner_selector: Optional[str] = None) -> Frame:
        """获取或创建指向 frame 的代理对象"""
        if frame_id in self._frames:
            return self._frames[frame_id]

        frame = Frame(self, frame_id, parent=parent, target_id=target_id, owner_selector=owner_selector)
        self._frames[frame_id] = frame

        if frame_id in self._pending_contexts:
            context_id, session_id = self._pending_contexts.pop(frame_id)
            frame._set_context(context_id, session_id)

        return frame

    def _flush_pending_enables(self):
        """处理待启用的 session（延迟执行，避免嵌套调用）"""
        while self._sessions_to_enable:
            session_id = self._sessions_to_enable.pop(0)
            try:
                self._cdp.send("Runtime.enable", session_id=session_id)
            except Exception as e:
                # 只忽略 session 失效的错误（跨域 iframe 可能已被移除）
                if "session" not in str(e).lower() and "target" not in str(e).lower():
                    raise

    def _handle_event(self, event: dict):
        """事件分发"""
        method = event.get("method")
        if not method:
            return

        handlers = {
            "Runtime.executionContextCreated": self._on_context_created,
            "Runtime.executionContextDestroyed": self._on_context_destroyed,
            "Target.attachedToTarget": self._on_target_attached,
            "Page.javascriptDialogOpening": self._on_dialog_opening,
        }
        handler = handlers.get(method)
        if handler:
            handler(event)

    # ==================== 事件处理器 ====================

    def _on_context_created(self, event: dict):
        """处理 JS 上下文创建"""
        params = event.get("params", {})
        session_id = event.get("sessionId")
        ctx = params["context"]
        aux_data = ctx.get("auxData", {})
        frame_id = aux_data.get("frameId")
        context_id = ctx["id"]
        is_default = aux_data.get("isDefault", False)

        if not frame_id:
            return

        frame = self._frames.get(frame_id)
        if frame:
            if is_default or frame._context_id is None:
                frame._set_context(context_id, session_id)
        else:
            if is_default or frame_id not in self._pending_contexts:
                self._pending_contexts[frame_id] = (context_id, session_id)

    def _on_context_destroyed(self, event: dict):
        """处理 JS 上下文销毁"""
        params = event.get("params", {})
        ctx_id = params.get("executionContextId")
        for frame in self._frames.values():
            if frame._context_id == ctx_id:
                frame._context_id = None
                break

    def _on_target_attached(self, event: dict):
        """处理跨域 iframe 附加"""
        params = event.get("params", {})
        new_session_id = params.get("sessionId")
        target_info = params.get("targetInfo", {})

        if target_info.get("type") == "iframe" and new_session_id:
            self._pending_sessions[new_session_id] = target_info
            # 延迟启用，避免在事件处理器中嵌套调用 send
            self._sessions_to_enable.append(new_session_id)

    def _on_dialog_opening(self, event: dict):
        """处理弹窗出现"""
        params = event.get("params", {})
        frame_id = params.get("frameId")
        if frame_id:
            self._pending_dialogs[frame_id] = params

    # ==================== 弹窗 ====================

    def _handle_dialog(self, frame_id: str, accept: bool = True, prompt_text: str = ""):
        """处理弹窗"""
        self._cdp.send("Page.handleJavaScriptDialog", {
            "accept": accept,
            "promptText": prompt_text
        })
        # 清理已处理的弹窗
        self._pending_dialogs.pop(frame_id, None)

    def _wait_for_dialog(self, frame_id: Optional[str] = None, timeout: float = 10) -> dict:
        """等待弹窗出现

        Args:
            frame_id: 可选，指定等待哪个 frame 的弹窗（与 CDP frameId 一致）
            timeout: 超时时间（秒）

        Returns:
            弹窗信息，包含 type, message, frameId 等
        """
        start = time.time()
        while time.time() - start < timeout:
            self._cdp.poll_events()
            # 查找匹配的弹窗
            if frame_id:
                if frame_id in self._pending_dialogs:
                    return self._pending_dialogs[frame_id]
            else:
                if self._pending_dialogs:
                    # 返回第一个弹窗
                    first_frame_id = next(iter(self._pending_dialogs.keys()))
                    return self._pending_dialogs[first_frame_id]
            time.sleep(0.1)
        raise Exception("等待弹窗超时")
