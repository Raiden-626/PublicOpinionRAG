"""探测脚本(临时): 渲染 aicu.cc 评论/弹幕页,等真实数据(非零计数),
记录 api.aicu.cc 请求状态,dump 渲染后 HTML。

用法: python _probe.py [--headful]
"""
import sys, re, time
from playwright.sync_api import sync_playwright

URLS = [
    ("reply", "https://www.aicu.cc/reply?uid=2", "评论数"),
    ("danmu", "https://www.aicu.cc/videodanmu?uid=2", "弹幕数"),
]


def grab(page, name, url, marker):
    print(f"\n=== {name}: {url}")
    api_reqs = []
    page.on("response", lambda r: api_reqs.append((r.url.split("?")[0], r.status))
            if "aicu.cc/api" in r.url else None)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # 等 shell 出现
    try:
        page.wait_for_function(
            f"()=>document.body&&document.body.innerText.includes('{marker}')",
            timeout=30000)
    except Exception as e:
        print("  shell wait fail:", e)

    # 等非零计数,最多 90s
    cnt = -1
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            t = page.inner_text("body", timeout=2000)
            m = re.search(marker + r"\s*[:：]\s*(\d+)", t)
            cnt = int(m.group(1)) if m else -1
        except Exception:
            cnt = -1
        if cnt and cnt > 0:
            break
        page.wait_for_timeout(3000)

    print(f"  count={cnt}")
    print(f"  api.aicu.cc requests: {api_reqs[:15]}")
    page.wait_for_timeout(1500)
    html = page.content()
    open(f"{name}_rendered.html", "w", encoding="utf-8").write(html)
    print(f"  saved {name}_rendered.html len={len(html)}")


def main():
    headless = "--headful" not in sys.argv
    print("headless =", headless)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless)
        c = b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900}, locale="zh-CN")
        pg = c.new_page()
        for name, url, marker in URLS:
            grab(pg, name, url, marker)
        b.close()


if __name__ == "__main__":
    main()
