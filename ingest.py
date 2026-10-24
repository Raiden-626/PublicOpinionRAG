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

import pymysql
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from zones import zone_name

import config

load_dotenv()

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


# ---- B站API: 获取视频信息(UP主/分区/标签) ----
_video_info_cache = {}    # oid -> 成功解析的结果
_video_info_failed = {}   # oid -> (失败时间, 结果dict, 原因): 失败不入成功缓存, 冷却后可重试
_FAIL_COOLDOWN = 60       # 失败后多久允许重试(秒)
_last_api_call = 0.0

# B站 view 接口拿不到分区信息的业务错误码:
#   62002 稿件不可见 / -404 啥都木有 / -403 无权访问 / 62004 分段播放
#   62012 视频本身还在(标题可见), 但接口不返回分区信息(充电专属/粉丝可见等)
# 这些视频再请求也拿不到 tid/tname/tags, 可以永久跳过。
_GONE_CODES = {62002, -404, -403, 62004, 62012}

# 失效视频集合缓存(避免每次都查库)
_dead_cache = {"oids": set(), "ts": 0.0}
_DEAD_CACHE_TTL = 60      # 失效列表缓存时长(秒)


def _parse_api_error(err):
    """从错误串里解析 B站业务错误码与消息。返回 (code|None, message)。"""
    if not err:
        return None, None
    m = re.search(r"code=(-?\d+)\s*(.*)$", err)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, err


def dead_oids(force_refresh=False):
    """返回已确认失效的视频 oid 集合(带 TTL 缓存, 开关关闭时返回空集合)。"""
    if not config.SKIP_DEAD_VIDEOS:
        return set()
    now = time.time()
    if force_refresh or now - _dead_cache["ts"] > _DEAD_CACHE_TTL:
        try:
            import db
            db.ensure_dead_video_table()
            _dead_cache["oids"] = db.get_dead_oids()
        except Exception as e:
            print(f"[dead] 读取失效视频列表失败: {e}")
        _dead_cache["ts"] = now
    return _dead_cache["oids"]


def mark_dead_video(oid, reason, uid=None, kind=None):
    """把失效视频记入 dead_video 表, 之后不再向 B站 请求它。"""
    try:
        import db
        db.mark_dead_videos([(oid, uid, kind, reason)])
        _dead_cache["oids"].add(oid)
        _dead_cache["ts"] = time.time()
        print(f"[dead] oid={oid} 已标记失效({reason}),后续不再请求")
    except Exception as e:
        print(f"[dead] 标记 oid={oid} 失败: {e}")


def _api_rate_limit():
    """API 限流: 两次请求间至少间隔 1.2s。"""
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 1.2:
        time.sleep(1.2 - elapsed)
    _last_api_call = time.time()


def _api_get(url):
    """带防护的 GET 请求。返回解析后的 dict,失败返回 None。"""
    _api_rate_limit()
    try:
        import os
        import requests

        headers = {"User-Agent": _UA}
        
        # 构建完整 Cookie 头
        sessdata = os.environ.get("BILI_SESSDATA", "").strip()
        bili_jct = os.environ.get("BILI_BILI_JCT", "").strip()
        dedeuserid = os.environ.get("BILI_DEDEUSERID", "").strip()
        
        cookies = []
        if sessdata:
            cookies.append(f"SESSDATA={sessdata}")
        if bili_jct:
            cookies.append(f"bili_jct={bili_jct}")
        if dedeuserid:
            cookies.append(f"DedeUserID={dedeuserid}")
        
        if cookies:
            headers["Cookie"] = "; ".join(cookies)

        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct and "javascript" not in ct:
            return None, f"非JSON响应(Content-Type={ct})"
        try:
            data = resp.json()
        except Exception:
            return None, "JSON解析失败"
        if data.get("code") == 0:
            return data.get("data"), None
        return None, f"业务错误 code={data.get('code')} {data.get('message') or ''}".strip()
    except Exception as e:
        return None, str(e)


def get_video_info(oid, skip_dead=None, mark_dead=None):
    """获取视频完整信息: owner_uid, tid, tname, tags。结果缓存。

    返回 dict: {"owner_uid": int|None, "tid": int|None, "tname": str|None, "tags": list|None}
    失败时所有字段为 None。

    skip_dead: 是否跳过已确认失效的视频(默认跟随 config.SKIP_DEAD_VIDEOS 开关)。
    mark_dead: 是否把确认失效的视频登记进 dead_video 表(默认跟随开关)。
    """
    if oid is None:
        return {"owner_uid": None, "tid": None, "tname": None, "tags": None}

    # 已确认失效的视频(稿件不可见/已删除)不再请求 B站
    if skip_dead is None:
        skip_dead = config.SKIP_DEAD_VIDEOS
    if skip_dead and oid in dead_oids():
        return {"owner_uid": None, "tid": None, "tname": None, "tags": None}

    if mark_dead is None:
        mark_dead = config.SKIP_DEAD_VIDEOS

    if oid in _video_info_cache:
        return _video_info_cache[oid]

    # 失败记录带冷却: 冷却期内不重复请求(避免放大限流), 冷却过后再试一次
    failed = _video_info_failed.get(oid)
    if failed and time.time() - failed[0] < _FAIL_COOLDOWN:
        return failed[1]

    # 1) 从 view API 获取 owner_uid, tid (tname 已停发, 见下)
    data, err = _api_get(f"https://api.bilibili.com/x/web-interface/view?aid={oid}")
    if err:
        print(f"[api] oid={oid} view失败: {err}")
        result = {"owner_uid": None, "tid": None, "tname": None, "tags": None}
        _video_info_failed[oid] = (time.time(), result, err)
        # 稿件不可见/已删除 -> 永久失效, 记入 dead_video 表
        code, message = _parse_api_error(err)
        if mark_dead and code in _GONE_CODES:
            mark_dead_video(oid, f"code={code} {message}".strip())
        return result
    _video_info_failed.pop(oid, None)

    owner_uid = data.get("owner", {}).get("mid")
    tid = data.get("tid")
    # B站 view 接口已停发 tname/tname_v2(恒为空串), 分区名改由本地映射表给出
    tname = zone_name(tid, data.get("tname"))

    # 2) 从 tags API 获取标签列表(尽力而为,失败不阻塞)
    tags_data, err = _api_get(f"https://api.bilibili.com/x/tag/archive/tags?aid={oid}")
    if not err and tags_data:
        tags = [t["tag_name"] for t in tags_data if t.get("tag_name")]
    else:
        tags = None

    result = {"owner_uid": owner_uid, "tid": tid, "tname": tname, "tags": tags}
    _video_info_cache[oid] = result
    return result


def get_video_owner(oid):
    """获取视频UP主uid(向后兼容的包装函数)。"""
    return get_video_info(oid).get("owner_uid")


def resolve_video_info(records):
    """批量解析记录的 video_owner_uid, tid, tname, tags(带缓存,相同 oid 只请求一次)。
    适用于评论和弹幕记录。
    """
    oids = set()
    for rec in records:
        oid = rec.get("oid")
        if oid is not None and oid not in _video_info_cache:
            oids.add(oid)
    if oids:
        print(f"[api] 批量查询 {len(oids)} 个视频的信息(UP主/分区/标签)...")
    for oid in oids:
        get_video_info(oid)
    # 填充到记录中
    for rec in records:
        oid = rec.get("oid")
        if oid is None:
            continue
        info = _video_info_cache.get(oid, {})
        if rec.get("video_owner_uid") is None:
            rec["video_owner_uid"] = info.get("owner_uid")
        if rec.get("tid") is None:
            rec["tid"] = info.get("tid")
        if rec.get("tname") is None:
            rec["tname"] = info.get("tname")
        if rec.get("tags") is None:
            rec["tags"] = info.get("tags")


def backfill_video_info(uids=None, batch_size=50):
    """批量补全已有记录中缺失的 tid/tname/tags/video_owner_uid。

    参数:
      uids: 要处理的 uid 列表,None 则自动扫描所有有数据的 uid
      batch_size: 每批处理的记录数

    返回 dict: {scanned, updated, skipped, failed}
    """
    import json
    import db

    stats = {"scanned": 0, "updated": 0, "skipped": 0, "failed": 0}

    # 自动发现所有有数据的 uid
    if uids is None:
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'bilibili_comment_%%'",
                    (db.MYSQL["database"],),
                )
                uids = []
                for (tbl,) in cur.fetchall():
                    uid_str = tbl.replace("bilibili_comment_", "")
                    if uid_str.isdigit():
                        uids.append(int(uid_str))
        finally:
            conn.close()
        print(f"[backfill] 发现 {len(uids)} 个用户表")

    for uid in uids:
        for kind in ("comment", "danmu"):
            tbl = db.table_for(kind, uid)
            # 已确认失效的视频不再请求(开关关闭时 dead 为空集, 行为不变)
            dead = dead_oids()
            dead_sql = (
                "AND NOT EXISTS (SELECT 1 FROM `dead_video` d WHERE d.oid = c.oid) "
                if config.SKIP_DEAD_VIDEOS
                else ""
            )
            conn = db.get_conn()
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    # 查找缺失视频信息的记录(按 oid 去重)
                    cur.execute(
                        f"SELECT DISTINCT c.id, c.oid FROM `{tbl}` c "
                        f"WHERE c.oid IS NOT NULL AND (c.tid IS NULL OR c.tags IS NULL) "
                        f"{dead_sql}"
                        f"LIMIT %s",
                        (batch_size,),
                    )
                    rows = cur.fetchall()
            except pymysql.err.ProgrammingError:
                rows = []
            finally:
                conn.close()

            if not rows:
                continue

            print(f"[backfill] {tbl}: 发现 {len(rows)} 条待补全记录")
            stats["scanned"] += len(rows)

            # 按 oid 去重,逐个查询 API
            oid_seen = set()
            updates = []
            for row in rows:
                oid = row["oid"]
                if oid in oid_seen:
                    stats["skipped"] += 1
                    continue
                oid_seen.add(oid)
                if oid in dead:
                    stats["skipped"] += 1
                    continue
                info = get_video_info(oid)
                if info.get("tid") is not None or info.get("tags") is not None:
                    updates.append((row["id"], info))
                else:
                    stats["failed"] += 1

            # 批量回写
            if updates:
                conn = db.get_conn()
                try:
                    with conn.cursor() as cur:
                        for record_id, info in updates:
                            tags_json = (
                                json.dumps(info["tags"], ensure_ascii=False)
                                if info.get("tags")
                                else None
                            )
                            cur.execute(
                                f"UPDATE `{tbl}` SET "
                                f"video_owner_uid=COALESCE(video_owner_uid, %s), "
                                f"tid=%s, tname=%s, tags=%s "
                                f"WHERE id=%s",
                                (
                                    info.get("owner_uid"),
                                    info.get("tid"),
                                    info.get("tname"),
                                    tags_json,
                                    record_id,
                                ),
                            )
                            if cur.rowcount:
                                stats["updated"] += 1
                    conn.commit()
                finally:
                    conn.close()

            print(f"[backfill] {tbl}: 更新 {len(updates)} 条")

    print(
        f"[backfill] 完成: 扫描 {stats['scanned']}, 更新 {stats['updated']}, "
        f"跳过 {stats['skipped']}, 失败 {stats['failed']}"
    )
    return stats


def repair_tnames_offline(uids=None):
    """用本地 tid→分区名 映射表离线修复已有记录的 tname(不发任何 B站 API 请求)。

    只处理 tid 已知但 tname 为 NULL/空串的行; tid 未知的行按"原视频已失效"跳过。

    参数:
      uids: 要处理的 uid 列表,None 则自动扫描所有有数据的 uid

    返回 dict: {scanned, updated, skipped, unknown_tids}
    """
    import db
    from zones import TID_NAMES

    stats = {"scanned": 0, "updated": 0, "skipped": 0, "unknown_tids": []}

    if uids is None:
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'bilibili_comment_%%'",
                    (db.MYSQL["database"],),
                )
                uids = []
                for (tbl,) in cur.fetchall():
                    uid_str = tbl.replace("bilibili_comment_", "")
                    if uid_str.isdigit():
                        uids.append(int(uid_str))
        finally:
            conn.close()
        print(f"[repair] 发现 {len(uids)} 个用户表")

    for uid in uids:
        for kind in ("comment", "danmu"):
            tbl = db.table_for(kind, uid)
            conn = db.get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT tid, COUNT(*) FROM `{tbl}` "
                        f"WHERE tid IS NOT NULL AND (tname IS NULL OR tname='') "
                        f"GROUP BY tid"
                    )
                    groups = cur.fetchall()
            except pymysql.err.ProgrammingError:
                groups = []
            finally:
                conn.close()

            if not groups:
                continue

            for tid, cnt in groups:
                stats["scanned"] += cnt
                name = TID_NAMES.get(int(tid))
                if not name:
                    # 映射表里没有这个分区, 保留空值(可用 zones.py --probe 补)
                    if int(tid) not in stats["unknown_tids"]:
                        stats["unknown_tids"].append(int(tid))
                    stats["skipped"] += cnt
                    continue
                conn = db.get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE `{tbl}` SET tname=%s "
                            f"WHERE tid=%s AND (tname IS NULL OR tname='')",
                            (name, tid),
                        )
                        stats["updated"] += cur.rowcount
                    conn.commit()
                finally:
                    conn.close()
                print(f"[repair] {tbl}: tid={tid} -> {name} ({cnt} 条)")

    print(
        f"[repair] 完成: 扫描 {stats['scanned']}, 更新 {stats['updated']}, "
        f"跳过 {stats['skipped']}, 未知分区 {stats['unknown_tids']}"
    )
    return stats


def discover_uids():
    """自动发现库中已有评论数据的 uid 列表。"""
    import db

    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'bilibili_comment_%%'",
                (db.MYSQL["database"],),
            )
            uids = []
            for (tbl,) in cur.fetchall():
                uid_str = tbl.replace("bilibili_comment_", "")
                if uid_str.isdigit():
                    uids.append(int(uid_str))
        return uids
    finally:
        conn.close()


def mark_dead_videos_for_uid(uid, limit=None, write_back=False):
    """扫描该 uid 表中 tid 仍为空的视频, 逐个向 B站 确认是否已失效。

    确认失效(稿件不可见/已删除/接口不返回分区信息)的会登记进 dead_video 表, 之后不再请求;
    视频仍可访问的说明只是之前没解析成功, 不登记, 后续仍可补全。

    write_back=True 时, 把解析成功的 tid/tname/tags 写回数据库(顺带补全, 不额外发请求)。

    返回 dict: {scanned, dead, alive, unknown, already, written}
    """
    import db

    stats = {"scanned": 0, "dead": 0, "alive": 0, "unknown": 0, "already": 0, "written": 0}

    already_dead = db.get_dead_oids()

    # 收集待确认的 oid
    oids = []
    seen = set()
    for kind in ("comment", "danmu"):
        tbl = db.table_for(kind, uid)
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT oid FROM `{tbl}` "
                    f"WHERE oid IS NOT NULL AND tid IS NULL"
                )
                for (oid,) in cur.fetchall():
                    if oid in seen:
                        continue
                    seen.add(oid)
                    if oid in already_dead:
                        stats["already"] += 1
                        continue
                    oids.append(oid)
        except pymysql.err.ProgrammingError:
            pass
        finally:
            conn.close()

    if limit:
        oids = oids[:limit]
    stats["scanned"] = len(oids)
    if not oids:
        return stats

    print(f"[mark-dead] uid={uid}: 逐个确认 {len(oids)} 个视频(约需 {len(oids) * 1.2:.0f} 秒)")

    resolved = {}   # oid -> info(用于回写)
    for oid in oids:
        # 强制请求一次并登记: 即使已登记失效也要重新确认
        info = get_video_info(oid, skip_dead=False, mark_dead=True)
        if info.get("tid") is not None:
            stats["alive"] += 1
            resolved[oid] = info
            continue
        failed = _video_info_failed.get(oid)
        code = _parse_api_error(failed[2])[0] if failed else None
        if code in _GONE_CODES:
            stats["dead"] += 1
        else:
            stats["unknown"] += 1

    if write_back and resolved:
        stats["written"] = _write_back_video_info(uid, resolved)

    return stats


def _write_back_video_info(uid, resolved):
    """把已解析的视频信息写回该 uid 的评论/弹幕表。返回更新行数。"""
    import json

    import db

    updated = 0
    for kind in ("comment", "danmu"):
        tbl = db.table_for(kind, uid)
        # 弹幕表没有 video_owner_uid 列
        if kind == "comment":
            set_sql = ("video_owner_uid=COALESCE(video_owner_uid, %s), "
                       "tid=%s, tname=%s, tags=COALESCE(tags, %s)")
        else:
            set_sql = "tid=%s, tname=%s, tags=COALESCE(tags, %s)"

        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                for oid, info in resolved.items():
                    tags_json = (
                        json.dumps(info["tags"], ensure_ascii=False)
                        if info.get("tags")
                        else None
                    )
                    if kind == "comment":
                        params = (info.get("owner_uid"), info.get("tid"),
                                  info.get("tname"), tags_json, oid)
                    else:
                        params = (info.get("tid"), info.get("tname"), tags_json, oid)
                    cur.execute(
                        f"UPDATE `{tbl}` SET {set_sql} "
                        f"WHERE oid=%s AND tid IS NULL",
                        params,
                    )
                    updated += cur.rowcount
            conn.commit()
        except pymysql.err.ProgrammingError:
            pass
        finally:
            conn.close()
    return updated


def parse_comment_card(card, uid):
    """从一张评论卡提取结构化字段。"""
    caps = [
        c.get_text(" ", strip=True) for c in card.select("span.MuiTypography-caption")
    ]
    bodies = [p.get_text(" ", strip=True) for p in card.select("p.MuiTypography-body1")]
    content = bodies[0] if bodies else ""
    # 首个 caption = "时间"; 第二个 = "uid:X 爱来自aicu.cc"
    time_cap = caps[0] if caps else ""
    ctime = _parse_ctime(time_cap)

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
        "category": None,
        "source": "aicu.cc",
        "url": url,
        "video_owner_uid": None,  # 稍后由 resolve_video_info 批量填充
        "tid": None,  # 视频分区ID
        "tname": None,  # 视频分区名
        "tags": None,  # 视频标签列表
    }


def parse_danmu_card(card, uid):
    """从一张弹幕卡提取结构化字段。"""
    caps = [
        c.get_text(" ", strip=True) for c in card.select("span.MuiTypography-caption")
    ]
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
        "video_owner_uid": None,  # 稍后由 resolve_video_info 批量填充
        "tid": None,  # 视频分区ID
        "tname": None,  # 视频分区名
        "tags": None,  # 视频标签列表
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
        page.evaluate(
            "() => {"
            "  let b=null,d=0;"
            "  for(const e of document.querySelectorAll('div')){"
            "    const x=e.scrollHeight-e.clientHeight; if(x>d){d=x;b=e;}}"
            "  if(b){b.scrollTop=b.scrollHeight;}"
            "}"
        )
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

    # 批量解析视频信息(UP主/分区/标签) — 评论和弹幕都需要
    if records:
        resolve_video_info(records)

    return records, total


def scrape_uid(uid, kinds=("comment", "danmu"), headless=True):
    """抓取指定 uid 的评论/弹幕,返回 {kind: [records]}。同时落 JSON 便于核对。

    不依赖 MySQL/Chroma,纯抓取,可单独验证。
    """
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless)
        ctx = b.new_context(
            user_agent=_UA, viewport={"width": 1366, "height": 900}, locale="zh-CN"
        )
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
    }
    if kind == "danmu":
        d["video_offset"] = rec.get("video_offset")
    return d


def ingest_uid(
    uid, kinds=("comment", "danmu"), max_pages=None, headless=True, overwrite=False
):
    """抓取 + 入 MySQL + 入 Chroma。

    增量模式(默认):
      - 读取 ingest_state 表中该 uid+kind 的 watermark(上次最新 ctime)
      - 抓取后只处理 ctime > watermark 的新记录
      - 处理完毕后更新 watermark
      - 首次抓取时 watermark=None,等价于全量

    覆盖模式(overwrite=True):
      - 清空旧数据 + 重置 watermark,然后全量入库

    返回 dict: {fetched, inserted, samples, incremental}
    """
    import db
    import vector_store
    from clients import embed

    db.ensure_tables(uid)

    # 覆盖模式:先清空旧数据 + 重置 watermark
    if overwrite:
        print(f"[overwrite] 清空 uid={uid} 的旧数据...")
        mysql_deleted = db.clear_uid_data(uid, kinds=kinds)
        chroma_deleted = vector_store.clear_by_uid(uid)
        print(
            f"[overwrite] MySQL 删除 {mysql_deleted} 行, Chroma 删除 {chroma_deleted} 条向量"
        )

    # 读取增量 watermark(覆盖模式清除后读到 None → 全量)
    watermarks = {}
    incremental = not overwrite
    for kind in kinds:
        wm = db.get_ingest_state(uid, kind)
        watermarks[kind] = wm
        if wm:
            print(f"[{kind}] 增量模式: watermark={wm.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print(f"[{kind}] 全量模式(首次抓取或无 watermark)")

    data = scrape_uid(uid, kinds=kinds, headless=headless)

    result = {"fetched": {}, "inserted": {}, "samples": {}, "incremental": incremental}
    for kind, records in data.items():
        table = db.table_for(kind, uid)
        insert = db.insert_comment if kind == "comment" else db.insert_danmu
        watermark = watermarks.get(kind)

        # 增量过滤: 只保留 ctime > watermark 的记录
        if watermark:
            new_records = [
                r for r in records if r.get("ctime") and r["ctime"] > watermark
            ]
            skipped = len(records) - len(new_records)
            if skipped:
                print(f"[{kind}] 增量过滤: 跳过 {skipped} 条旧记录(ctime ≤ watermark)")
            records_to_process = new_records
        else:
            records_to_process = records

        # 入库 MySQL
        new_rows = []
        for rec in records_to_process:
            row = _to_mysql_row(rec, kind)
            mysql_id = insert(uid, row)
            if mysql_id is None:
                continue  # 重复,跳过向量化
            new_rows.append((mysql_id, rec))

        result["fetched"][kind] = len(records)
        result["inserted"][kind] = len(new_rows)
        result["samples"][kind] = [_sample(r, kind) for r in records[:5]]

        if not new_rows:
            # 无新增行,检查 Chroma 向量是否完整
            existing_mids = vector_store.get_mysql_ids(
                where={"$and": [{"uid": uid}, {"mysql_table": table}]}
            )
            if existing_mids:
                print(f"[{kind}] 无新增行,Chroma 已有 {len(existing_mids)} 条向量,跳过")
                # 更新 watermark(即使无新行,也推进到本次抓取的最大 ctime)
                _update_watermark(uid, kind, records_to_process, watermark)
                continue
            # Chroma 中没有向量,需要补全(从 MySQL 取全部行做向量化)
            from db import fetch_all_ids, fetch_rows_by_ids

            all_ids = fetch_all_ids(kind, uid)
            if not all_ids:
                print(f"[{kind}] MySQL 中也无数据,跳过")
                _update_watermark(uid, kind, records_to_process, watermark)
                continue
            print(f"[{kind}] Chroma 缺少向量,从 MySQL 补全 {len(all_ids)} 条…")
            rows = fetch_rows_by_ids(kind, uid, all_ids)
            new_rows = [(r["id"], r) for r in rows]

        # 批量向量化(带重试,最多 3 次)
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
            _update_watermark(uid, kind, records_to_process, watermark)
            continue

        ids, metas, docs = [], [], []
        for (mysql_id, rec), vec in zip(new_rows, vecs):
            ids.append(f"{kind}_{uid}_{mysql_id}")
            metas.append(
                {
                    "uid": int(uid),
                    "type": table,
                    "mysql_table": table,
                    "mysql_id": int(mysql_id),
                    "oid": rec.get("oid") or 0,
                }
            )
            # 嵌入文本包含日期,帮助时间相关查询匹配
            ctime_str = (
                rec["ctime"].strftime("%Y-%m-%d") if rec.get("ctime") else "未知时间"
            )
            docs.append(f"[{ctime_str}] {rec['content']}")
        vector_store.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
        print(f"[{kind}] 新增 {len(new_rows)} 条入库(MySQL + Chroma)")

        # 更新 watermark
        _update_watermark(uid, kind, records_to_process, watermark)

    # ---- 失效视频登记 ----
    # 入库完成后收尾: 把"本次仍拿不到分区信息"的视频 oid 过一遍,
    # B站 已确认失效(稿件不可见/已删除)的登记进 dead_video 表,
    # 之后的交互查询与批量补全不再对这些 oid 请求 API。
    if config.SKIP_DEAD_VIDEOS:
        _register_dead_videos(uid, kinds, data)
        print(f"[dead] 失效视频登记表现有 {len(dead_oids())} 条")

    return result


def _register_dead_videos(uid, kinds, scraped):
    """入库收尾: 登记本次确认失效的视频。

    只登记 B站 接口明确返回"稿件不可见/已删除"的视频;
    网络/限流等临时故障不算失效, 后续仍可重试。
    """
    import db

    # 收集本次入库里仍为空的 oid -> kind
    pending = {}
    for kind in kinds:
        for rec in (scraped.get(kind) or []):
            oid = rec.get("oid")
            if oid is not None and rec.get("tid") is None:
                pending.setdefault(oid, kind)
    if not pending:
        return

    items = []
    for oid, kind in pending.items():
        failed = _video_info_failed.get(oid)
        if not failed:
            continue  # 本次没失败过(可能是旧数据/重复行), 不擅自判定失效
        code, message = _parse_api_error(failed[2])
        if code in _GONE_CODES:
            items.append((oid, uid, kind, f"code={code} {message}".strip()))
    if not items:
        return

    db.mark_dead_videos(items)
    dead = _dead_cache["oids"]
    dead.update(item[0] for item in items)
    _dead_cache["ts"] = time.time()
    print(f"[dead] uid={uid} 本次登记失效视频 {len(items)} 个")


def _update_watermark(uid, kind, records, old_watermark):
    """将 watermark 推进到本次处理记录中的最大 ctime。"""
    import db

    ctimes = [r["ctime"] for r in records if r.get("ctime")]
    if not ctimes:
        return
    max_ctime = max(ctimes)
    # 只在 max_ctime 超过旧 watermark 时才推进
    if old_watermark is None or max_ctime > old_watermark:
        db.set_ingest_state(uid, kind, max_ctime)
        print(f"[{kind}] watermark 更新为 {max_ctime.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("uid", type=int, nargs="?", help="B站用户uid(backfill模式下可省略)")
    ap.add_argument("--kinds", default="comment,danmu")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--no-ingest", action="store_true", help="只抓取落 JSON,不入库")
    ap.add_argument(
        "--overwrite", action="store_true", help="覆盖模式:清空旧数据后重新入库"
    )
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="补全已有记录的 tid/tname/tags(从B站API)",
    )
    ap.add_argument(
        "--repair-tname",
        action="store_true",
        help="用本地 tid→分区名映射表离线修复已有记录的 tname(不发API请求)",
    )
    args = ap.parse_args()

    if args.repair_tname:
        # 离线修复模式: 不请求 B站 API
        uids = [args.uid] if args.uid else None
        stats = repair_tnames_offline(uids=uids)
        print(
            f"\n修复完成: 扫描 {stats['scanned']} 条, 更新 {stats['updated']} 条, "
            f"跳过 {stats['skipped']} 条, 未知分区 {stats['unknown_tids']}"
        )
    elif args.backfill:
        # 补全模式: 可指定单个 uid, 或处理全部用户
        uids = [args.uid] if args.uid else None
        stats = backfill_video_info(uids=uids)
        print(
            f"\n补全完成: 扫描 {stats['scanned']} 条, 更新 {stats['updated']} 条, 跳过 {stats['skipped']} 条, 失败 {stats['failed']} 条"
        )
    else:
        if not args.uid:
            ap.error("请提供 uid, 或使用 --backfill 补全已有数据")
        kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
        if args.no_ingest:
            scrape_uid(args.uid, kinds=kinds, headless=not args.headful)
        else:
            ingest_uid(
                args.uid,
                kinds=kinds,
                headless=not args.headful,
                overwrite=args.overwrite,
            )
