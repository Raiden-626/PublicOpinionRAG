"""分析脚本(临时): 按 bilibili 评论/弹幕链接定位真实行,dump 其结构。"""
import re
from bs4 import BeautifulSoup


def find_rows(path, label, link_re):
    s = BeautifulSoup(open(path, encoding="utf-8").read(), "lxml")
    anchors = [a for a in s.select("a[href]") if link_re.search(a.get("href") or "")]
    print(f"\n##### {label}  匹配链接数={len(anchors)}")
    if not anchors:
        return
    a = anchors[0]
    print("first href:", a.get("href"))
    # 向上走到一个较完整的行容器(5 层)
    node = a
    for _ in range(5):
        node = node.parent
    print("--- enclosing row prettified (<=6000) ---")
    print(node.prettify()[:6000])
    print("--- row 内匹配链接 ---")
    for x in node.select("a[href]"):
        if link_re.search(x.get("href") or ""):
            print(" ", x.get("href"))
    print("--- row text ---")
    print(repr(node.get_text(" | ", strip=True)[:600]))
    if len(anchors) > 1:
        n2 = anchors[1]
        for _ in range(5):
            n2 = n2.parent
        print("--- 第二行(对比, <=2000) ---")
        print(n2.prettify()[:2000])


find_rows("reply_rendered.html", "评论",
          re.compile(r"bilibili\.com/(video|opus)|root_id=|oid="))
find_rows("danmu_rendered.html", "弹幕",
          re.compile(r"bilibili\.com/video|dmid=|oid="))
