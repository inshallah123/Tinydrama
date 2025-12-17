# Tinydrama

一个基于纯 Python 标准库的简易浏览器自动化工具，通过 Chrome DevTools Protocol (CDP) 控制浏览器。

> 名字灵感来自 Playwright（剧作家），Tinydrama 意为"小戏剧"。

## 特性

- **零依赖** - 仅使用 Python 标准库，无需安装任何第三方包
- **轻量级** - 单文件 ~750 行，复制即用
- **多标签页** - 每个 Tab 独立对象，支持并行操作
- **学习友好** - 适合学习 WebSocket、CDP 协议和浏览器自动化原理

## 快速开始

```python
from tinydrama import MiniBrowser

browser = MiniBrowser()
tab = browser.launch(browser="edge")  # 返回 Tab 对象

tab.goto("https://www.baidu.com")
tab.fill("#kw", "Python")
tab.click("#su")
tab.wait_for_text("百度百科")
tab.screenshot("result.png")

browser.close()
```

## 多标签页

```python
browser = MiniBrowser()
tab1 = browser.launch()

# 每个 Tab 是独立对象
tab2 = browser.new_tab("https://example.com")

# 可以交替操作，状态互不影响
tab1.goto("https://site-a.com")
tab1.fill("#user", "alice")

tab2.fill("#search", "query")
tab2.click("#go")

browser.close()
```

## API 参考

### MiniBrowser（浏览器管理器）

| 方法 | 说明 |
|------|------|
| `launch(browser="auto")` | 启动浏览器，返回初始 Tab |
| `connect()` | 连接已运行的浏览器，返回 Tab |
| `new_tab(url)` | 新建标签页，返回 Tab |
| `get_tabs()` | 获取所有已连接的 Tab |
| `close_tab(tab)` | 关闭指定标签页 |
| `close()` | 关闭浏览器 |

### Tab（页面操作）

**导航**

| 方法 | 说明 |
|------|------|
| `goto(url)` | 导航到 URL |
| `wait_for_load()` | 等待页面加载完成 |
| `wait_for_url(pattern)` | 等待 URL 包含指定字符串 |

**元素操作**

| 方法 | 说明 |
|------|------|
| `click(selector)` | 点击元素 |
| `fill(selector, value)` | 填充输入框 |
| `select(selector, value)` | 选择下拉框选项 |
| `check(selector, checked)` | 勾选/取消复选框 |

**元素读取**

| 方法 | 说明 |
|------|------|
| `query_selector(selector)` | 查询元素信息 |
| `wait_for_selector(selector)` | 等待元素出现 |
| `get_text(selector)` | 获取元素文本 |
| `get_value(selector)` | 获取输入框的值 |
| `get_attribute(selector, attr)` | 获取元素属性 |
| `wait_for_text(text)` | 等待页面出现指定文本 |

**iframe**

| 方法 | 说明 |
|------|------|
| `switch_to_frame(selector/name/index)` | 切换到 iframe |
| `switch_to_main_frame()` | 切换回主页面 |

**文件与截图**

| 方法 | 说明 |
|------|------|
| `screenshot(path)` | 页面截图 |
| `upload_file(selector, path)` | 上传文件 |
| `enable_download(path)` | 启用下载到指定目录 |
| `wait_for_download()` | 等待下载完成 |

**其他**

| 方法 | 说明 |
|------|------|
| `execute_script(js)` | 执行 JavaScript |
| `handle_dialog(accept)` | 处理弹窗 |
| `wait_for_dialog()` | 等待弹窗出现 |
| `activate()` | 激活此标签页 |
| `close()` | 关闭此标签页 |

## 架构

```
MiniBrowser（浏览器管理器）
    │
    ├── Tab（页面对象，包含所有操作方法）
    │   └── CDPSession（CDP 通信）
    │       └── WebSocketClient（WebSocket 协议）
    │
    └── Tab（每个标签页独立）
        └── CDPSession
```

## 环境要求

- Python 3.10+
- Windows + Chrome 或 Edge 浏览器

## 项目文件

```
tinydrama.py    # 主程序，单文件即可使用
README.md       # 文档
```

## 适用场景

- 个人自动化办公（填表、下载报表）
- 简单的数据抓取
- 学习浏览器自动化原理

## License

MIT
