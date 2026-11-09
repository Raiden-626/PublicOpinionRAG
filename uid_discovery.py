"""候选 uid 发现: 从库内数据与 B站评论区挖掘待入库用户, 写入 uid_pool 表。

三个来源:
  1. owners    — 库内评论表的 video_owner_uid(UP主), 零成本, 优先级最高
  2. reply_api — 挑库内高频 oid, 用 B站评论接口拉评论区, 提取评论者 uid
                 (与库内兴趣分布最贴近, 限流 3s/次)
  3. manual    — 手动清单 uid_pool.txt, 每行一个 uid

用法(经 main.py):
    python main.py discover owners --limit 200
    python main.py discover reply --top-videos 20 --per-video 2
    python main.py discover manual --file uid_pool.txt
    python main.py pool                     # 查看池状态
"""

import os
import re
import time

import pymysql

import db

# 手动清单默认路径(相对项目根)
_MANUAL_FILE = "uid_pool.txt"

# ---- B站评论接口(独立于 ingest 的 1.2s 限流, 评论区挖掘用更保守的 3s) ----
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_last_reply_call = 0.0


def _reply_rate_limit():
    """评论区挖掘限流: 两次请求间至少间隔 3s。"""
    global _last_reply_call
    elapsed = time.time() - _last_reply_call
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)
    _last_reply_call = time.time()


def _reply_get(url):
    """带限流的 GET, 返回 (data|None, err|None)。复用 .env 的 B站 cookie。"""
    _reply_rate_limit()
    try:
        import requests

        headers = {"User-Agent": _UA}
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
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data"), None
        return None, f"code={data.get('code')} {data.get('message') or ''}".strip()
    except Exception as e:
        return None, str(e)


def _existing_uids():
    """库内已有数据的 uid 集合(用于去重)。"""
    return set(_scan_library_uids())


def _scan_library_uids():
    """库内已有评论/弹幕表的 uid 列表。"""
    import ingest
    return ingest.discover_uids()


def owners_ranking(limit=200, min_refs=2):
    """统计库内评论表引用的 UP主(uid -> 被引用次数), 过滤已入库 uid。
    只读不写库, discover_from_owners 与页面预览共用。
    返回 [(uid, refs)], 按引用次数降序, 截断到 limit。
    """
    conn = db.get_conn()
    counter = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'bilibili_comment_%%'",
                (db.MYSQL["database"],),
            )
            tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT video_owner_uid, COUNT(*) FROM `{tbl}` "
                        f"WHERE video_owner_uid IS NOT NULL "
                        f"GROUP BY video_owner_uid"
                    )
                    for owner, cnt in cur.fetchall():
                        counter[owner] = counter.get(owner, 0) + cnt
            except pymysql.err.ProgrammingError:
                continue
    finally:
        conn.close()

    existing = _existing_uids()
    candidates = [
        (owner, cnt) for owner, cnt in counter.items()
        if owner not in existing and cnt >= min_refs
    ]
    candidates.sort(key=lambda x: -x[1])
    return candidates[:limit]


def discover_from_owners(limit=200, min_refs=2):
    """来源1: 从库内评论表提取 video_owner_uid(UP主)。

    min_refs: 至少被多少条库内评论引用才入池(过滤冷门UP主噪音)。
    返回入池条数。
    """
    candidates = owners_ranking(limit=limit, min_refs=min_refs)

    # UP主 类用户通常互动面广, 优先级 50(在 reply_api 之前跑)
    added = db.pool_add_uids(
        [u for u, _ in candidates], source="owner", priority=50
    )
    print(f"[discover-owners] 候选 {len(candidates)} 个(引用≥{min_refs}), "
          f"新入池 {added} 个")
    for u, cnt in candidates[:10]:
        print(f"  uid={u} 被库内评论引用 {cnt} 次")
    if len(candidates) > 10:
        print(f"  ... 其余 {len(candidates) - 10} 个见 uid_pool 表")
    return added


def _top_oids(top_n):
    """挑库内高频 oid(跨用户被评论最多的视频, 排除已确认失效的)。返回 [(oid, refs)]。"""
    conn = db.get_conn()
    counter = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'bilibili_comment_%%'",
                (db.MYSQL["database"],),
            )
            tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT oid, COUNT(*) FROM `{tbl}` "
                        f"WHERE oid IS NOT NULL GROUP BY oid"
                    )
                    for oid, cnt in cur.fetchall():
                        counter[oid] = counter.get(oid, 0) + cnt
            except pymysql.err.ProgrammingError:
                continue
    finally:
        conn.close()

    # 失效视频不用于挖评论区
    dead = set()
    try:
        dead = db.get_dead_oids()
    except pymysql.err.ProgrammingError:
        pass

    ranked = [(oid, cnt) for oid, cnt in counter.items() if oid not in dead]
    ranked.sort(key=lambda x: -x[1])
    return ranked[:top_n]


def discover_from_reply_api(top_videos=20, per_video_pages=2, per_video_cap=50,
                            should_stop=None):
    """来源2: 对库内高频 oid 拉评论区, 提取评论者 uid。

    top_videos:        取被评论最多的前 N 个视频
    per_video_pages:   每个视频拉几页评论(每页约 20 条)
    per_video_cap:     每个视频最多提取多少个 uid(防单视频霸池)
    返回入池条数。
    """
    oids = _top_oids(top_videos)
    if not oids:
        print("[discover-reply] 库内没有可用视频")
        return 0
    print(f"[discover-reply] 选取 {len(oids)} 个高频视频: "
          f"{[oid for oid, _ in oids[:5]]}...")

    existing = _existing_uids()
    counter = {}
    total_req = 0
    for oid, refs in oids:
        if should_stop is not None and should_stop():
            print("[discover-reply] 收到停止信号, 优雅停止(已请求 "
                  f"{total_req} 次)")
            break
        uids_of_video = []
        for pn in range(1, per_video_pages + 1):
            url = (f"https://api.bilibili.com/x/v2/reply"
                   f"?type=1&oid={oid}&pn={pn}&ps=20&sort=2")
            data, err = _reply_get(url)
            total_req += 1
            if err:
                print(f"[discover-reply] oid={oid} pn={pn} 失败: {err}")
                break
            replies = (data or {}).get("replies") or []
            for r in replies:
                mid = ((r.get("member") or {}).get("mid"))
                if mid:
                    uids_of_video.append(int(mid))
                # 楼中楼的前 3 条回复也带 member, 顺手提取
                for sub in (r.get("replies") or []):
                    mid2 = ((sub.get("member") or {}).get("mid"))
                    if mid2:
                        uids_of_video.append(int(mid2))
            if len(replies) < 20:
                break  # 没有下一页了
        # 单视频限流入池量, 保持池子多样性
        seen_video = set()
        for u in uids_of_video:
            if u in seen_video:
                continue
            seen_video.add(u)
            if len(seen_video) > per_video_cap:
                break
            counter[u] = counter.get(u, 0) + 1
        print(f"[discover-reply] oid={oid}(库内引用{refs}次): "
              f"提取 {len(seen_video)} 个评论者")

    candidates = [u for u in counter if u not in existing]
    # 在多个视频下出现的评论者更"活跃通用", 优先级 100(UP主之后跑)
    added = db.pool_add_uids(candidates, source="reply_api", priority=100)
    print(f"[discover-reply] 共请求 {total_req} 次, 候选 {len(candidates)} 个, "
          f"新入池 {added} 个")
    return added


def read_manual_file(path=None):
    """读取手动清单(只读, 页面预览用)。返回 (exists, [uid,...])。"""
    path = path or _MANUAL_FILE
    if not os.path.exists(path):
        return False, []
    uids = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if re.fullmatch(r"\d+", line):
                uids.append(int(line))
    return True, uids


def discover_from_manual(path=None):
    """来源3: 手动清单(或页面直接传来的 uid 列表)。返回入池条数。"""
    path = path or _MANUAL_FILE
    exists, uids = read_manual_file(path)
    if not exists:
        print(f"[discover-manual] 清单不存在: {path}")
        return 0
    # 手动指定的优先级最高(用户明确意图)
    added = db.pool_add_uids(uids, source="manual", priority=20)
    print(f"[discover-manual] 清单 {len(uids)} 个, 新入池 {added} 个")
    return added


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="候选 uid 发现")
    ap.add_argument("source", choices=["owners", "reply", "manual"])
    ap.add_argument("--limit", type=int, default=200, help="owners 最大入池数")
    ap.add_argument("--min-refs", type=int, default=2,
                    help="owners 最少引用次数")
    ap.add_argument("--top-videos", type=int, default=20,
                    help="reply 挖掘的视频数")
    ap.add_argument("--pages", type=int, default=2, help="reply 每视频页数")
    ap.add_argument("--cap", type=int, default=50, help="reply 每视频uid上限")
    ap.add_argument("--file", default=None, help="manual 清单路径")
    args = ap.parse_args()

    if args.source == "owners":
        discover_from_owners(limit=args.limit, min_refs=args.min_refs)
    elif args.source == "reply":
        discover_from_reply_api(
            top_videos=args.top_videos,
            per_video_pages=args.pages,
            per_video_cap=args.cap,
        )
    else:
        discover_from_manual(args.file)
