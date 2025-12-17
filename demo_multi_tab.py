"""
多标签页演示脚本
启动 Edge，开两个 tab 分别访问不同网站，报告页面元素
"""

from tinydrama import MiniBrowser


def report_page_info(tab, name: str):
    """报告页面基本信息"""
    print(f"\n{'='*50}")
    print(f"[{name}] 页面信息")
    print(f"{'='*50}")

    # 获取页面标题
    title = tab.execute_script("document.title")
    print(f"标题: {title}")

    # 获取页面 URL
    url = tab.execute_script("window.location.href")
    print(f"URL: {url}")

    # 统计页面元素
    stats = tab.execute_script("""
        (function() {
            return {
                inputs: document.querySelectorAll('input').length,
                buttons: document.querySelectorAll('button').length,
                links: document.querySelectorAll('a').length,
                forms: document.querySelectorAll('form').length,
                images: document.querySelectorAll('img').length,
                iframes: document.querySelectorAll('iframe').length
            };
        })()
    """)

    print(f"输入框: {stats['inputs']} 个")
    print(f"按钮: {stats['buttons']} 个")
    print(f"链接: {stats['links']} 个")
    print(f"表单: {stats['forms']} 个")
    print(f"图片: {stats['images']} 个")
    print(f"iframe: {stats['iframes']} 个")

    # 列出所有输入框
    inputs = tab.execute_script("""
        Array.from(document.querySelectorAll('input')).slice(0, 10).map(el => ({
            type: el.type,
            name: el.name || '(无name)',
            id: el.id || '(无id)',
            placeholder: el.placeholder || ''
        }))
    """)

    if inputs:
        print(f"\n前 {len(inputs)} 个输入框:")
        for i, inp in enumerate(inputs, 1):
            print(f"  {i}. type={inp['type']}, name={inp['name']}, id={inp['id']}")


def main():
    browser = MiniBrowser()

    try:
        # 启动 Edge 浏览器
        print("启动 Edge 浏览器...")
        tab1 = browser.launch(browser="edge")

        # 创建第二个标签页
        print("创建第二个标签页...")
        tab2 = browser.new_tab()

        # Tab1 访问工商银行
        print("\n[Tab1] 正在访问工商银行...")
        tab1.goto("https://im.icbc.com.cn/ICBCMPServer/index.jsp")

        # Tab2 访问期货市场监控中心
        print("[Tab2] 正在访问期货市场监控中心...")
        tab2.goto("https://investorservice.cfmmc.com/")

        # 报告两个页面的信息
        report_page_info(tab1, "工商银行")
        report_page_info(tab2, "期货监控中心")

        print("\n" + "="*50)
        print("演示完成！按 Enter 关闭浏览器...")
        input()

    finally:
        browser.close()


if __name__ == "__main__":
    main()
