"""抓取 + 入库: Playwright 渲染 aicu.cc → 解析评论/弹幕卡 → MySQL + Chroma。

aicu.cc 是带 Cloudflare/ticket 的 React SPA,纯 requests 拿不到数据,必须用浏览器渲染。
该站对每个 uid 仅展示最新约 100 条评论 / 100 条弹幕(顶部"评论数/弹幕数"为历史总数,仅计数,
UI 不翻页)。本模块抓取当前可见的全部卡片。

用法(模块):
    from ingest import scrape_uid, ingest_uid
    data = scrape_uid(2)              # 只抓取,返回结构化数据 + 落 JSON
    ingest_uid(2)                     # 抓取 + 入 MySQL + 入 Chroma

CLI(经 main.py):
    python main.py ingest 2 --kinds comment,danmu
"""
import json
import os
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---- 页面配置 ----
PAGES = {
    "comment": {
        "url": "https://www.aicu.cc/reply?uid={uid}",
        "marker": "评论数",
        "link_re": re.compile(r"#reply\d+|root=\d+|oid=\d+"),
        "table": "bilibili_comment",
    },
    "danmu": {
        "url": "https://www.aicu.cc/videodanmu?uid={uid}",
        "marker": "弹幕数",
        "link_re": re.compile(r"dmid=\d+|oid=\d+"),
        "table": "bilibili_danmu",
    },
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 时间解析: "2025/4/10 13:21:00" / "2023/10/19 15:10:18"
_TIME_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})")
# 弹幕偏移: "(20.6s)"
_OFFSET_RE = re.compile(r"\(([\d.]+)s\)")
# av 号
_AV_RE = re.compile(r"av(\d+)", re.I)
# 评论 root_id / oid
_REPLY_RE = re.compile(r"#reply(\d+)")
_ROOT_RE = re.compile(r"[?&]root=(\d+)")
_OID_RE = re.compile(r"[?&]oid=(\d+)")
# 弹幕 dmid
_DMID_RE = re.compile(r"[?&]dmid=(\d+)")


def _parse_ctime(s):
    """'2025/4/10 13:21:00' -> datetime; 失败返回 None。"""
    m = _TIME_RE.search(s or "")
    if not m:
        return None
    try:
        y, mo, d, h, mi, se = map(int, m.groups())
        return datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None


def _is_data_card(card):
    """排除用户资料卡(含头像/查询粉丝牌)、广告卡、筛选控件卡。"""
    if card.select(".MuiAvatar-root"):
        return False
    txt = card.get_text(" ", strip=True)
    if "查询粉丝牌" in txt or "曾用名" in txt:
        return False
    if "广告" in txt or "dwz.junwfk.com" in str(card):
        return False
    # 真数据卡必含 uid: 爱来自aicu.cc 这行
    if "爱来自aicu.cc" not in txt:
        return False
    return True


def parse_comment_card(card, uid):
    """从一张评论卡提取结构化字段。"""
    caps = [c.get_text(" ", strip=True) for c in card.select("span.MuiTypography-caption")]
    bodies = [p.get_text(" ", strip=True) for p in card.select("p.MuiTypography-body1")]
    content = bodies[0] if bodies else ""
    # 首个 caption = "时间 [点赞数?]"; 第二个 = "uid:X 爱来自aicu.cc"
    time_cap = caps[0] if caps else ""
    ctime = _parse_ctime(time_cap)
    # 末尾数字疑似 like_count
    like = None
    mt = re.search(r"(\d+)\s*$", time_cap.split("爱来自")[0])
    if mt:
        like = int(mt.group(1))

    hrefs = [a.get("href", "") for a in card.select("a[href]")]
    oid = root_id = None
    for h in hrefs:
        if h.startswith("/"):
            h = "https://www.bilibili.com" + h
        m = _AV_RE.search(h)
        if m:
            oid = int(m.group(1))
        m = _OID_RE.search(h)
        if m:
            oid = int(m.group(1))
        m = _REPLY_RE.search(h)
        if m:
            root_id = int(m.group(1))
        m = _ROOT_RE.search(h)
        if m:
            root_id = int(m.group(1))
    # 直达链接(方式0 优先)
    url = next((h for h in hrefs if "#reply" in h), hrefs[0] if hrefs else None)

    return {
        "uid": int(uid),
        "oid": oid,
        "bvid": None,
        "rpid": root_id,  # root_id 即评论 rpid,作去重键
        "content": content,
        "ctime": ctime,
        "like_count": like,
        "category": None,
        "source": "aicu.cc",
        "url": url,
    }


def parse_danmu_card(card, uid):
    """从一张弹幕卡提取结构化字段。"""
    caps = [c.get_text(" ", strip=True) for c in card.select("span.MuiTypography-caption")]
    bodies = [p.get_text(" ", strip=True) for p in card.select("p.MuiTypography-body1")]
    content = bodies[0] if bodies else ""
    time_cap = caps[0] if caps else ""
    ctime = _parse_ctime(time_cap)
    mo = _OFFSET_RE.search(time_cap)
    offset = float(mo.group(1)) if mo else None

    hrefs = [a.get("href", "") for a in card.select("a[href]")]
    oid = dmid = None
    for h in hrefs:
        if h.startswith("/"):
            h = "https://www.bilibili.com" + h
        m = _AV_RE.search(h)
        if m:
            oid = int(m.group(1))
        m = _OID_RE.search(h)
        if m:
            oid = int(m.group(1))
        m = _DMID_RE.search(h)
        if m:
            dmid = int(m.group(1))
    url = next((h for h in hrefs if "dmid=" in h), hrefs[0] if hrefs else None)

    return {
        "uid": int(uid),
        "oid": oid,
        "bvid": None,
        "dmid": dmid,
        "content": content,
        "video_offset": offset,
        "ctime": ctime,
        "mode": None,
        "color": None,
        "fontsize": None,
        "extra": None,
        "source": "aicu.cc",
        "url": url,
    }


def _wait_for_count(page, marker, timeout=120):
    """等顶部计数出现并 >0(aicu.cc 有排队/ticket 机制,需较长等待)。返回总数。"""
    deadline = time.time() + timeout
    cnt = 0
    while time.time() < deadline:
        try:
            txt = page.inner_text("body", timeout=2000)
            m = re.search(marker + r"\s*[:：]\s*(\d+)", txt)
            cnt = int(m.group(1)) if m else 0
        except Exception:
            cnt = 0
        if cnt and cnt > 0:
            break
        page.wait_for_timeout(3000)
    return cnt


def _collect_kind(page, kind, uid):
    """渲染单页并解析当前 DOM 内全部数据卡。返回 (records, total)。"""
    cfg = PAGES[kind]
    url = cfg["url"].format(uid=uid)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function(
            f"()=>document.body&&document.body.innerText.includes('{cfg['marker']}')",
            timeout=30000,
        )
    except Exception:
        pass
    total = _wait_for_count(page, cfg["marker"])

    # 尝试滚动加载更多(尽力而为:站点的列表在 window 或内层容器上滚动)
    for _ in range(6):
        page.mouse.wheel(0, 60000)
        page.evaluate("() => {"
                      "  let b=null,d=0;"
                      "  for(const e of document.querySelectorAll('div')){"
                      "    const x=e.scrollHeight-e.clientHeight; if(x>d){d=x;b=e;}}"
                      "  if(b){b.scrollTop=b.scrollHeight;}"
                      "}")
        page.wait_for_timeout(900)

    html = page.content()
    soup = BeautifulSoup(html, "lxml")
    cards = [c for c in soup.select("div.MuiCard-root") if _is_data_card(c)]

    parser = parse_comment_card if kind == "comment" else parse_danmu_card
    records = []
    seen = set()
    for c in cards:
        rec = parser(c, uid)
        key = rec.get("rpid") if kind == "comment" else rec.get("dmid")
        if key is None:
            key = (rec.get("oid"), rec.get("content"), str(rec.get("ctime")))
        if key in seen:
            continue
        seen.add(key)
        records.append(rec)
    return records, total


def scrape_uid(uid, kinds=("comment", "danmu"), headless=True):
    """抓取指定 uid 的评论/弹幕,返回 {kind: [records]}。同时落 JSON 便于核对。

    不依赖 MySQL/Chroma,纯抓取,可单独验证。
    """
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless)
        ctx = b.new_context(user_agent=_UA, viewport={"width": 1366, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        for kind in kinds:
            if kind not in PAGES:
                continue
            recs, total = _collect_kind(page, kind, uid)
            out[kind] = recs
            print(f"[{kind}] uid={uid} 抓到 {len(recs)} 条(站点报告总数={total})")
            _dump_json(uid, kind, recs)
        b.close()
    return out


def _dump_json(uid, kind, records):
    """落盘 JSON 便于人工核对抓取结果。"""
    os.makedirs("scrape_out", exist_ok=True)
    path = f"scrape_out/uid{uid}_{kind}.json"
    # datetime 不可直接 json 化
    ser = []
    for r in records:
        d = dict(r)
        if isinstance(d.get("ctime"), datetime):
            d["ctime"] = d["ctime"].strftime("%Y-%m-%d %H:%M:%S")
        ser.append(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ser, f, ensure_ascii=False, indent=2)
    print(f"  -> 已保存 {path}")


# ---- 入库(MySQL + Chroma)----
def _to_mysql_row(rec, kind):
    """把抓取记录转为 MySQL 行 dict(ctime 转 datetime 已是,JSON 字段 None)。"""
    row = dict(rec)
    # db.insert_* 期望键与表列一致;ctime 保持 datetime
    return row


def _sample(rec, kind):
    """抽取页面展示用的精简样本(ctime 转 str)。"""
    d = {
        "content": rec["content"],
        "ctime": rec["ctime"].strftime("%Y-%m-%d %H:%M") if rec.get("ctime") else None,
        "url": rec.get("url"),
        "like_count": rec.get("like_count"),
    }
    if kind == "danmu":
        d["video_offset"] = rec.get("video_offset")
    return d


def ingest_uid(uid, kinds=("comment", "danmu"), max_pages=None, headless=True):
    """抓取 + 入 MySQL + 入 Chroma。

    - MySQL: 按 rpid/dmid 去重(INSERT IGNORE),返回新分配 id
    - Chroma: 仅对新增行做向量化入库(metadata 携 mysql_table/mysql_id/uid)
    返回 dict: {fetched:{comment,danmu}, inserted:{comment,danmu}, samples:{comment,danmu}}
    """
    import db
    import vector_store
    from clients import embed

    db.ensure_tables(uid)
    data = scrape_uid(uid, kinds=kinds, headless=headless)

    result = {"fetched": {}, "inserted": {}, "samples": {}}
    for kind, records in data.items():
        table = db.table_for(kind, uid)
        insert = db.insert_comment if kind == "comment" else db.insert_danmu
        new_rows = []
        for rec in records:
            row = _to_mysql_row(rec, kind)
            mysql_id = insert(uid, row)
            if mysql_id is None:
                continue  # 重复,跳过向量化
            new_rows.append((mysql_id, rec))

        result["fetched"][kind] = len(records)
        result["inserted"][kind] = len(new_rows)
        result["samples"][kind] = [_sample(r, kind) for r in records[:5]]

        if not new_rows:
            print(f"[{kind}] 无新增行(全部已存在),跳过向量化")
            continue

        # 批量向量化
        texts = [r[1]["content"] for r in new_rows]
        try:
            vecs = embed(texts)
        except Exception as e:
            print(f"[{kind}] 向量化失败:{e}（数据已入 MySQL,可稍后补向量）")
            continue

        ids, metas, docs = [], [], []
        for (mysql_id, rec), vec in zip(new_rows, vecs):
            ids.append(f"{kind}_{mysql_id}")
            metas.append({
                "uid": int(uid),
                "type": table,
                "mysql_table": table,
                "mysql_id": int(mysql_id),
                "oid": rec.get("oid") or 0,
            })
            docs.append(rec["content"])
        vector_store.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
        print(f"[{kind}] 新增 {len(new_rows)} 条入库(MySQL + Chroma)")
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("uid", type=int)
    ap.add_argument("--kinds", default="comment,danmu")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--no-ingest", action="store_true", help="只抓取落 JSON,不入库")
    args = ap.parse_args()
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    if args.no_ingest:
        scrape_uid(args.uid, kinds=kinds, headless=not args.headful)
    else:
        ingest_uid(args.uid, kinds=kinds, headless=not args.headful)
