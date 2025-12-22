# Tinydrama

一个基于纯 Python 标准库的简易浏览器自动化工具，通过 Chrome DevTools Protocol (CDP) 控制浏览器。

> 名字灵感来自 Playwright（剧作家），Tinydrama 意为"小戏剧"。

## 特性

- **零依赖** - 仅使用 Python 标准库，无需安装任何第三方包
- **轻量级** - 模块化设计，约 700 行代码
- **统一的 Frame 模型** - 主页面和 iframe 使用相同的 API，无需切换状态
- **多标签页** - 每个 Tab 独立对象，支持并行操作
- **自动恢复** - iframe 导航后自动恢复 context，无需重新获取
- **学习友好** - 适合学习 WebSocket、CDP 协议和浏览器自动化原理

## 快速开始

```python
from tinydrama import Browser

browser = Browser()
frame = browser.launch()  # 自动查找 Chrome 或 Edge

frame.goto("https://www.baidu.com")
frame.fill("#kw", "Python")
frame.click("#su")
frame.wait_for_text("百度百科")
frame.screenshot("result.png")

browser.close()
```

## iframe 操作

```python
frame = browser.launch()
frame.goto("https://example.com")

# 获取 iframe，返回独立的 Frame 对象
child = frame.iframe("#my-iframe")
child.fill("input", "hello")
child.click("button")

# 主页面和 iframe 可以交替操作，无需切换
frame.click("#submit")
child.click("#confirm")
```

## 多标签页

```python
browser = Browser()
tab1 = browser.launch()

# 每个 Tab 是独立的 Frame 对象
tab2 = browser.new_tab("https://example.com")

# 可以交替操作，状态互不影响
tab1.goto("https://site-a.com")
tab1.fill("#user", "alice")

tab2.fill("#search", "query")
tab2.click("#go")

browser.close()
```

## 下载文件

```python
browser = Browser()
frame = browser.launch()
frame.goto("https://example.com/downloads")

# 在 Browser 级别启用下载
browser.enable_download("D:/downloads")

# 触发下载
frame.click("#download-btn")

# 等待下载完成
result = browser.wait_for_download(frame=frame, timeout=120)
print(f"下载完成: {result.get('suggestedFilename')}")

browser.close()
```

## API 参考

### Browser（浏览器管理器）

| 方法 | 说明 |
|------|------|
| `launch(kill_existing=True, browser="auto")` | 启动浏览器，返回主 Frame |
| `connect()` | 连接已运行的浏览器，返回主 Frame |
| `new_tab(url)` | 新建标签页，返回 Frame |
| `close_tab(frame)` | 关闭指定标签页 |
| `enable_download(path)` | 启用下载到指定目录 |
| `wait_for_download(frame, timeout)` | 等待下载完成 |
| `close()` | 关闭浏览器 |

`browser` 参数支持：`"auto"`（自动查找）、`"chrome"`、`"edge"` 或浏览器可执行文件路径。

### Frame（页面操作）

**导航**

| 方法 | 说明 |
|------|------|
| `goto(url)` | 导航到 URL |
| `wait_for_load()` | 等待页面加载完成 |
| `wait_for_url(pattern)` | 等待 URL 包含指定字符串 |

**元素操作**

| 方法 | 说明 |
|------|------|
| `click(selector, native=False)` | 点击元素（默认 JS 点击，native=True 使用鼠标事件） |
| `click_by_text(text, tag, exact)` | 通过文本点击元素 |
| `double_click(selector, native=False)` | 双击元素 |
| `hover(selector)` | 悬停在元素上 |
| `fill(selector, value)` | 填充输入框 |
| `select(selector, value=, text=)` | 选择下拉框选项 |
| `check(selector, checked)` | 勾选/取消复选框 |

**元素查询**

| 方法 | 说明 |
|------|------|
| `query_selector(selector)` | 查询元素信息 |
| `query_all(selector)` | 查询所有匹配元素 |
| `wait_for_selector(selector)` | 等待元素出现 |
| `wait_for_text(text)` | 等待页面出现指定文本 |

**元素读取**

| 方法 | 说明 |
|------|------|
| `get_text(selector)` | 获取元素文本 |
| `get_value(selector)` | 获取输入框的值 |
| `get_attribute(selector, attr)` | 获取元素属性 |
| `is_checked(selector)` | 检查复选框是否选中 |

**iframe**

| 方法 | 说明 |
|------|------|
| `iframe(selector)` | 获取 iframe 的 Frame 对象 |
| `is_root` | 是否是根 Frame（Tab） |

**文件与截图**

| 方法 | 说明 |
|------|------|
| `screenshot(path)` | 页面截图 |
| `upload_file(selector, path)` | 上传文件 |

**其他**

| 方法 | 说明 |
|------|------|
| `execute_script(js)` | 执行 JavaScript |
| `handle_dialog(accept, prompt_text)` | 处理弹窗 |
| `wait_for_dialog()` | 等待弹窗出现 |
| `activate()` | 激活此标签页 |

## 架构

```
Browser（浏览器管理器）
    │
    ├── browser-level CDPSession（下载事件监听）
    │
    └── FrameManager（事件管理，内部）
            │
            ├── Frame（主页面）
            │       └── Frame（iframe）
            │               └── Frame（嵌套 iframe）
            │
            └── CDPSession（CDP 通信）
                    └── WebSocketClient（WebSocket 协议）
```

## 项目结构

```
tinydrama/
├── __init__.py      # 导出公共 API
├── cdp.py           # WebSocket + CDP 通信层 + CDPError
├── frame.py         # Frame + FrameManager
└── browser.py       # Browser 管理器
```

## 环境要求

- Python 3.10+
- Windows + Chrome 或 Edge 浏览器

## 适用场景

- 个人自动化办公（填表、下载报表）
- 简单的数据抓取
- 学习浏览器自动化原理

## License

MIT
