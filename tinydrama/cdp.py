"""
Chrome DevTools Protocol 通信层

包含 WebSocket 客户端和 CDP 会话管理，这是稳定的底层通信模块。
"""

import socket
import struct
import base64
import json
import os
from urllib.parse import urlparse
from typing import Optional


class CDPError(Exception):
    """CDP 协议错误

    常见错误码:
    - -32000: Context 相关错误 (Cannot find context, Execution context was destroyed)
    - -32001: Session/Target 相关错误 (Session with given id not found, No target with given id)
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"CDP错误 [{code}]: {message}")


class WebSocketClient:
    """简易 WebSocket 客户端实现"""

    def __init__(self, url: str, timeout: float = 30):
        parsed = urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def connect(self):
        """建立 WebSocket 连接"""
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(self.timeout)

        # WebSocket 握手
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
        """发送 WebSocket 消息"""
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
        """接收 WebSocket 消息"""
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
        """精确接收 n 字节"""
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

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        """发送 CDP 命令并等待响应

        Args:
            method: CDP 方法名
            params: 方法参数
            session_id: 可选的 sessionId，用于发送到子 target（如跨域 iframe）

        Returns:
            CDP 响应结果
        """
        self._msg_id += 1
        msg_id = self._msg_id

        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
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
            err = response["error"]
            raise CDPError(err.get("code", 0), err.get("message", str(err)))

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
            pass
        finally:
            self.ws.sock.settimeout(self.ws.timeout)

    def _dispatch_event(self, event: dict):
        """分发事件给所有注册的处理器"""
        for handler in self._event_handlers:
            handler(event)

    def close(self):
        self.ws.close()
