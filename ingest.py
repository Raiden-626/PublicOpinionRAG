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
# 回复目标: "回复 @用户名 :" 或 "回复 @用户名: "
_REPLY_TO_RE = re.compile(r"回复\s*@([^ :：]+)\s*[:：]")


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


# ---- B站API: 获取视频UP主 ----
_video_owner_cache = {}
_last_api_call = 0.0


def get_video_owner(oid):
    """通过B站API获取视频oid的UP主uid。结果缓存。失败返回None。
    自带限流: 两次请求间至少间隔 0.3s,遇到 -412/超时自动重试最多 2 次。
    """
    global _last_api_call
    if oid is None:
        return None
    if oid in _video_owner_cache:
        return _video_owner_cache[oid]

    import requests, time

    for attempt in range(3):
        # 限流: 保证两次请求间至少有间隔
        elapsed = time.time() - _last_api_call
        if elapsed < 0.3:
            time.sleep(0.3 - elapsed)
        _last_api_call = time.time()

        try:
            resp = requests.get(
                f"https://api.bilibili.com/x/web-interface/view?aid={oid}",
                headers={"User-Agent": _UA},
                timeout=10,
            )
            data = resp.json()
            code = data.get("code")
            if code == 0:
                owner_uid = data["data"]["owner"]["mid"]
                _video_owner_cache[oid] = owner_uid
                return owner_uid
            if code == -412:
                # 风控拦截,等待后重试
                wait = 2 * (attempt + 1)
                print(f"[api] oid={oid} 触发风控, {wait}s 后重试({attempt+1}/3)")
                time.sleep(wait)
                continue
            # 其他错误码(-404 视频不存在 等),不重试
            break
        except Exception as e:
            if attempt < 2:
                wait = 2 * (attempt + 1)
                print(f"[api] oid={oid} 请求异常: {e}, {wait}s 后重试({attempt+1}/3)")
                time.sleep(wait)
            else:
                print(f"[api] 获取视频oid={oid}的UP主最终失败: {e}")

    _video_owner_cache[oid] = None
    return None


def resolve_video_owners(records):
    """批量解析评论记录的video_owner_uid(带缓存,相同oid只请求一次)。"""
    oids = set()
    for rec in records:
        oid = rec.get("oid")
        if oid is not None and oid not in _video_owner_cache:
            oids.add(oid)
    if oids:
        print(f"[api] 批量查询 {len(oids)} 个视频的UP主...")
    for oid in oids:
        get_video_owner(oid)
    # 填充到记录中
    for rec in records:
        if rec.get("video_owner_uid") is None and rec.get("oid") is not None:
            rec["video_owner_uid"] = _video_owner_cache.get(rec["oid"])


def parse_reply_target(card):
    """从评论卡中提取回复目标用户的uid。
    aicu.cc 的回复评论卡中有 data-user-id 属性的链接。
    返回 int 或 None。
    """
    # 方式1: 查找带 data-user-id 属性的 <a> 标签(回复目标)
    for a in card.select("a[data-user-id]"):
        user_id = a.get("data-user-id", "")
        if user_id.isdigit():
            return int(user_id)
    # 方式2: 从 href 中查找 /space.bilibili.com/{uid} 模式(非视频链接)
    for a in card.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"space\.bilibili\.com/(\d+)", href)
        if m:
            # 排除视频链接中的uid,只取回复目标的
            if "/video/" not in href and "bilibili.com/video" not in href:
                return int(m.group(1))
    return None


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

    # 回复目标用户uid(从 data-user-id 属性或 space 链接提取)
    reply_to_uid = parse_reply_target(card)

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
        "video_owner_uid": None,  # 稍后由 resolve_video_owners 批量填充
        "reply_to_uid": reply_to_uid,
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

    # 评论类型: 批量解析视频UP主
    if kind == "comment" and records:
        resolve_video_owners(records)

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


def ingest_uid(uid, kinds=("comment", "danmu"), max_pages=None, headless=True, overwrite=False):
    """抓取 + 入 MySQL + 入 Chroma。

    - MySQL: 按 rpid/dmid 去重(INSERT IGNORE),返回新分配 id
    - Chroma: 仅对新增行做向量化入库(metadata 携 mysql_table/mysql_id/uid)
    - overwrite=True 时,先清空该 UID 的旧数据再入库(完全覆盖)
    返回 dict: {fetched:{comment,danmu}, inserted:{comment,danmu}, samples:{comment,danmu}}
    """
    import db
    import vector_store
    from clients import embed

    db.ensure_tables(uid)

    # 覆盖模式:先清空旧数据
    if overwrite:
        print(f"[overwrite] 清空 uid={uid} 的旧数据...")
        mysql_deleted = db.clear_uid_data(uid, kinds=kinds)
        chroma_deleted = vector_store.clear_by_uid(uid)
        print(f"[overwrite] MySQL 删除 {mysql_deleted} 行, Chroma 删除 {chroma_deleted} 条向量")

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
            print(f"[{kind}] 无新增行(全部已存在),检查 Chroma 向量是否完整…")
            # 检查 Chroma 中是否已有该 uid+kind 的向量
            existing_mids = vector_store.get_mysql_ids(
                where={"$and": [{"uid": uid}, {"mysql_table": table}]}
            )
            if existing_mids:
                print(f"[{kind}] Chroma 已有 {len(existing_mids)} 条向量,跳过")
                continue
            # Chroma 中没有向量,需要补全(从 MySQL 取全部行做向量化)
            from db import fetch_all_ids, fetch_rows_by_ids
            all_ids = fetch_all_ids(kind, uid)
            if not all_ids:
                print(f"[{kind}] MySQL 中也无数据,跳过")
                continue
            print(f"[{kind}] Chroma 缺少向量,从 MySQL 补全 {len(all_ids)} 条…")
            rows = fetch_rows_by_ids(kind, uid, all_ids)
            new_rows = [(r["id"], r) for r in rows]

        # 批量向量化(带重试,最多 2 次)
        texts = [r[1]["content"] for r in new_rows]
        vecs = None
        for attempt in range(3):
            try:
                vecs = embed(texts)
                break
            except Exception as e:
                if attempt < 2:
                    print(f"[{kind}] 向量化第{attempt+1}次失败:{e},3 秒后重试…")
                    time.sleep(3)
                else:
                    print(f"[{kind}] 向量化最终失败:{e}（数据已入 MySQL,可稍后补向量）")
        if vecs is None:
            continue

        ids, metas, docs = [], [], []
        for (mysql_id, rec), vec in zip(new_rows, vecs):
            ids.append(f"{kind}_{uid}_{mysql_id}")
            metas.append({
                "uid": int(uid),
                "type": table,
                "mysql_table": table,
                "mysql_id": int(mysql_id),
                "oid": rec.get("oid") or 0,
            })
            # 嵌入文本包含日期,帮助时间相关查询匹配
            ctime_str = rec["ctime"].strftime("%Y-%m-%d") if rec.get("ctime") else "未知时间"
            docs.append(f"[{ctime_str}] {rec['content']}")
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
    ap.add_argument("--overwrite", action="store_true", help="覆盖模式:清空旧数据后重新入库")
    args = ap.parse_args()
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    if args.no_ingest:
        scrape_uid(args.uid, kinds=kinds, headless=not args.headful)
    else:
        ingest_uid(args.uid, kinds=kinds, headless=not args.headful, overwrite=args.overwrite)
