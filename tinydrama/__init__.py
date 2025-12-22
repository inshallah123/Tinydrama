"""
TinyDrama - 基于 Python 标准库的简易浏览器自动化工具

仅使用标准库实现，通过 Chrome DevTools Protocol (CDP) 控制浏览器。

基本用法:
    from tinydrama import Browser

    browser = Browser()
    frame = browser.launch()
    frame.goto("https://example.com")
    frame.click("button")
    browser.close()

使用 with 语句:
    with Browser() as browser:
        frame = browser.launch()
        frame.goto("https://example.com")
        frame.fill("#search", "hello")

iframe 操作:
    frame = browser.launch()
    frame.goto("https://example.com")
    child = frame.iframe("#my-iframe")
    child.click("button")
"""

from .browser import Browser
from .frame import Frame, FrameManager
from .cdp import CDPSession, CDPError, WebSocketClient

__all__ = [
    # 主要 API
    "Browser",
    "Frame",
    # 异常
    "CDPError",
    # 高级用法
    "FrameManager",
    "CDPSession",
    "WebSocketClient",
]

__version__ = "0.2.0"
