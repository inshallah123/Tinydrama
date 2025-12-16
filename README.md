# Tinydrama

一个基于纯 Python 标准库的简易浏览器自动化工具，通过 Chrome DevTools Protocol (CDP) 控制浏览器。

> 名字灵感来自 Playwright（剧作家），Tinydrama 意为"小戏剧"。

## 特性

- **零依赖** - 仅使用 Python 标准库，无需安装任何第三方包
- **轻量级** - 单文件实现，代码简洁易懂
- **学习友好** - 适合学习 WebSocket、CDP 协议和浏览器自动化原理

## 已实现功能

| 功能 | 方法 | 说明 |
|------|------|------|
| 启动浏览器 | `launch()` | 自动检测 Chrome/Edge |
| 页面导航 | `goto(url)` | 导航到指定 URL |
| 元素点击 | `click(selector)` | CSS 选择器定位并点击 |
| 文本输入 | `type_text(selector, text)` | 模拟键盘逐字输入 |
| 快速填充 | `fill(selector, value)` | 直接设置输入框值 |
| 下拉选择 | `select(selector, value)` | 选择下拉框选项 |
| 复选框 | `check(selector, checked)` | 勾选/取消复选框 |
| 获取文本 | `get_text(selector)` | 获取元素文本内容 |
| 获取属性 | `get_attribute(selector, attr)` | 获取元素属性值 |
| 等待元素 | `wait_for_selector(selector)` | 等待元素出现 |
| iframe 切换 | `switch_to_frame()` | 支持 selector/name/index |
| 截图 | `screenshot(path)` | 页面截图保存为 PNG |
| 执行 JS | `execute_script(script)` | 执行自定义 JavaScript |

## 快速开始

```python
from tinydrama import create_browser

browser = create_browser()

try:
    browser.goto("https://www.baidu.com")
    browser.fill("#kw", "Python")
    browser.click("#su")
    browser.wait(2)
    browser.screenshot("result.png")
finally:
    browser.close()
```

## 环境要求

- Python 3.7+
- Chrome 或 Edge 浏览器

## 项目结构

```
tinydrama/
├── tinydrama.py    # 主程序
└── README.md
```

## 核心组件

```
┌─────────────────┐
│   MiniBrowser   │  高级 API（click, fill, goto...）
├─────────────────┤
│   CDPSession    │  CDP 协议封装（命令/响应/事件）
├─────────────────┤
│ WebSocketClient │  WebSocket 协议实现
├─────────────────┤
│     socket      │  TCP 连接
└─────────────────┘
```

## 学习价值

通过这个项目可以学习：

- **网络协议**: TCP Socket、HTTP、WebSocket 握手与帧解析
- **浏览器原理**: Chrome DevTools Protocol、DOM 操作
- **异步编程**: 事件驱动、请求/响应模型

## License

MIT
