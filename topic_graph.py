"""第三层: 话题传播与关键节点(话题图谱)。

思路:
  以一个视频标签(如"足球")作为话题, 把库内所有命中该标签的评论/弹幕归到一起,
  拼成一张四类节点的关系图:
    话题节点 —— 该标签本身
    视频节点 —— 带该标签的视频(热度 = 库内用户参与数)
    用户节点 —— 库内已入库用户(发言数)
    UP主节点 —— 视频作者(发布数); 若作者本身也在库内, 合并进用户节点, 不裂成两个节点

  边有四种:
    话题 → 视频  包含(权重=该视频下的库内记录数)
    用户 → 视频  评论/弹幕(权重=该用户在此视频的发言数)
    视频 → UP主 发布(权重=1, 视频数已体现在节点值上)
    用户 ↔ 用户  互动(无向, 权重=共同视频数 + 跨向评论数)

  最后用"直接互动边"判断这个话题下的人是一个紧密圈子, 还是一群各自发声的散点。

数据边界(重要, 决定了本期不做哪些指标):
  库里没有点赞数(评论表 raw 字段全为 NULL), 也没有回复树(root_id 全空),
  所以本期不做"影响力(获赞最多)"这一维, 只做生产者与参与者。
  video_owner_uid 只覆盖约 58% 的记录, 覆盖率会随 meta.owner_coverage 返回给前端展示。

数据来源: 直接扫库内每张 per-uid 表(评论表 + 弹幕表), 不走 Chroma。
"""

import json
import time
from collections import defaultdict

import db

# 话题索引缓存时间(标签分布会随视频信息补全而变化)
CACHE_TTL = 300

# 候选话题至少要被多少个用户涉及(单人话题画不出关系网)
MIN_SHARED_USERS = 2

# 角色榜单长度
TOP_ROLES = 5

# 图里最多保留多少个视频节点(按热度取前 N, 超出部分在 note 里说明)
MAX_VIDEOS = 40

# 圈子判定: 直接互动边占"可能的用户两两组合"的比例
CIRCLE_RATIO = 0.5    # >= 该比例判紧密圈子
LOOSE_RATIO = 0.0     # > 0 判松散连接, 等于 0 判散点用户

# 每个视频保留的样例记录数(详情面板用)
SAMPLES_PER_VIDEO = 3

# 样例正文截断长度
SAMPLE_CONTENT_LIMIT = 80

_state = {
    "key": None,      # 入库用户集合签名
    "ts": 0.0,        # 缓存生成时间
    "uids": [],       # 入库用户 uid 列表
    "topics": {},     # 标签 -> 话题索引
}


# ---- 数据扫描 ----

def _ingested_uids():
    """库中已有数据的用户 uid 列表。"""
    try:
        return [u["uid"] for u in db.list_ingested_users()]
    except Exception as e:
        print(f"[topic_graph] 读取入库用户列表失败: {e}")
        return []


def _iter_rows(uids):
    """遍历所有入库用户的评论与弹幕, 产出 (uid, kind, row)。"""
    import pymysql

    conn = db.get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for uid in uids:
                for kind in ("comment", "danmu"):
                    tbl = db.table_for(kind, uid)
                    # 弹幕表没有 rpid 列, 只有评论表有(同楼判定要用)
                    cols = (
                        "id, oid, bvid, rpid, root_id, video_owner_uid, tname, tags, "
                        "content, ctime, url"
                        if kind == "comment"
                        else
                        "id, oid, bvid, video_owner_uid, tname, tags, "
                        "content, ctime, url"
                    )
                    try:
                        cur.execute(f"SELECT {cols} FROM `{tbl}`")
                        rows = cur.fetchall()
                    except Exception:
                        continue  # 表不存在或结构不同, 跳过
                    for row in rows:
                        yield uid, kind, row
    finally:
        conn.close()


def _row_tags(row):
    """取出该记录携带的标签列表(库里的 tags 是 JSON 数组)。"""
    tags = row.get("tags")
    if not tags:
        return []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            return []
    if not isinstance(tags, list):
        return []
    return [t.strip() for t in tags if isinstance(t, str) and t.strip()]


def _fmt_time(value):
    """datetime -> "YYYY-MM-DD HH:MM"(避免 jsonify 输出 RFC1123 长串)。"""
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except AttributeError:
        return str(value)


def _load_state(force=False):
    """建立"标签 -> 话题索引", 带 TTL 缓存。

    索引结构:
      topics[tag] = {
        "records": int,          # 命中该标签的库内记录数
        "users": set,            # 命中该标签的库内用户
        "videos": {oid: {
            "count": int,        # 该视频下的库内记录数
            "users": set,        # 在该视频下发言的库内用户
            "by_user": {uid: n}, # 每个用户的发言数
            "kinds": {uid: {"comment": n, "danmu": n}},  # 发言类型细分
            "bvid": str, "owner": int|None, "tname": str,
            "samples": [{uid, kind, content, ctime, url}],
        }},
      }
    """
    uids = _ingested_uids()
    key = tuple(sorted(uids))
    now = time.time()
    if (not force and _state["key"] == key and _state["ts"]
            and now - _state["ts"] < CACHE_TTL):
        return _state

    topics = {}
    # 互动索引(与 interactions.py 同口径):
    #   on_video   —— A 在 B 的视频下留言(video_owner_uid = B)
    #   same_thread—— 两人出现在同一评论楼(评论楼 id 相同)
    #   co_video   —— 两人在同一视频下都发言过
    on_video = defaultdict(int)
    same_thread = defaultdict(int)
    co_video = defaultdict(int)
    rpid_users = defaultdict(set)
    oid_users = defaultdict(set)

    def _topic(tag):
        return topics.setdefault(tag, {"records": 0, "users": set(), "videos": {}})

    tracked = set(uids)
    for uid, kind, row in _iter_rows(uids):
        owner = row.get("video_owner_uid")
        owner = int(owner) if owner else None
        if owner and owner in tracked and owner != uid:
            key = (uid, owner) if uid < owner else (owner, uid)
            on_video[key] += 1
        oid = row.get("oid")
        if oid:
            oid_users[oid].add(uid)
        rpid = row.get("root_id") or row.get("rpid")
        if rpid:
            rpid_users[int(rpid)].add(uid)
        tags = _row_tags(row)
        if not tags:
            continue
        bvid = row.get("bvid")
        tname = row.get("tname")
        content = (row.get("content") or "")[:SAMPLE_CONTENT_LIMIT]
        ctime = _fmt_time(row.get("ctime"))
        url = row.get("url")

        for tag in tags:
            t = _topic(tag)
            t["records"] += 1
            t["users"].add(uid)
            if not oid:
                continue
            v = t["videos"].get(oid)
            if v is None:
                v = {"count": 0, "users": set(), "by_user": {}, "kinds": {},
                     "bvid": bvid, "owner": owner, "tname": tname, "samples": []}
                t["videos"][oid] = v
            v["count"] += 1
            v["users"].add(uid)
            v["by_user"][uid] = v["by_user"].get(uid, 0) + 1
            k = v["kinds"].setdefault(uid, {"comment": 0, "danmu": 0})
            k["comment" if kind == "comment" else "danmu"] += 1
            if owner and not v.get("owner"):
                v["owner"] = owner
            if len(v["samples"]) < SAMPLES_PER_VIDEO:
                v["samples"].append({
                    "uid": uid, "kind": kind, "content": content,
                    "ctime": ctime, "url": url,
                })

    # 同一视频共现(全库口径)
    for users_of_oid in oid_users.values():
        members = sorted(users_of_oid)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                co_video[(a, b)] += 1

    # 同一评论楼共现
    for users_of_rpid in rpid_users.values():
        members = sorted(users_of_rpid)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                same_thread[(a, b)] += 1

    _state.update({
        "key": key, "ts": now, "uids": uids, "topics": topics,
        "interactions": {
            "on_video": dict(on_video),
            "same_thread": dict(same_thread),
            "co_video": dict(co_video),
        },
    })
    return _state


# ---- 话题候选 ----

def topic_candidates(keyword=None, limit=100):
    """可选择的话题列表(默认只列跨 >=2 个用户的标签)。

    返回 list[dict]: {tag, records, users, videos, shared}
    """
    state = _load_state()
    items = []
    for tag, t in state["topics"].items():
        shared = len(t["users"])
        if shared < MIN_SHARED_USERS:
            continue
        if keyword and keyword not in tag:
            continue
        items.append({
            "tag": tag,
            "records": t["records"],
            "users": shared,
            "videos": len(t["videos"]),
        })
    # 先按参与用户数, 再按记录数排序(用户多的话题关系网更丰富)
    items.sort(key=lambda x: (-x["users"], -x["records"], x["tag"]))
    return items[:limit]


# ---- 图谱构建 ----

def _interaction_pairs(kept, tracked, interactions, key_nodes):
    """关键节点之间的直接互动边。

    三种证据(与 interactions.py 同口径):
      共现     —— 两个节点在本话题的同一条视频下都发言过(话题内)
      on_video —— A 在 B 的视频下留言(video_owner_uid = B, 全库口径)
      同楼     —— 两人出现在同一评论楼(评论楼 id 相同, 全库口径)

    返回 {(a, b): {"shared_videos": n, "on_video": n, "same_thread": n}}, a < b
    """
    pairs = {}

    def _bump(a, b, field, amount=1):
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        item = pairs.setdefault(key, {"shared_videos": 0, "on_video": 0, "same_thread": 0})
        item[field] += amount

    on_video = interactions.get("on_video") or {}
    same_thread = interactions.get("same_thread") or {}

    # 话题内共现
    for oid, v in kept.items():
        users = sorted(v["users"])
        for i, a in enumerate(users):
            for b in users[i + 1:]:
                _bump(a, b, "shared_videos")

    # 全库口径的互动(现有互动分析)
    for (a, b), cnt in on_video.items():
        if a in key_nodes and b in key_nodes:
            _bump(a, b, "on_video", cnt)
    for (a, b), cnt in same_thread.items():
        if a in key_nodes and b in key_nodes:
            _bump(a, b, "same_thread", cnt)

    # 只保留真有互动的对
    return {k: v for k, v in pairs.items() if v["shared_videos"] or v["on_video"] or v["same_thread"]}


def _verdict(participants, pairs):
    """判断这个话题下的人是紧密圈子还是散点。

    ratio = 有直接互动的用户对数 / 可能的用户对数
    """
    n = len(participants)
    possible = n * (n - 1) // 2
    interactive = len(pairs)
    ratio = (interactive / possible) if possible else 0.0
    if n < 2:
        kind, text = "scattered", "该话题下只有 1 个库内用户, 谈不上圈子"
    elif ratio >= CIRCLE_RATIO:
        kind, text = "circle", "参与者之间存在广泛直接互动, 是一个紧密圈子"
    elif interactive > 0:
        kind, text = "loose", "参与者之间只有零星互动, 更像松散连接"
    else:
        kind, text = "scattered", "参与者之间没有任何直接互动, 是一群各自发声的散点用户"
    return {
        "kind": kind,
        "text": text,
        "participants": n,
        "interactive_pairs": interactive,
        "possible_pairs": possible,
        "ratio": round(ratio, 3),
    }


def build_graph(tag, max_videos=MAX_VIDEOS):
    """构建某个话题的关系图。

    返回 dict:
      {
        "tag": str,
        "nodes": [...],   # ECharts graph 节点
        "links": [...],   # ECharts graph 连线
        "roles": {"producers": [...], "participants": [...]},
        "verdict": {...},
        "meta": {...},
        "note": str,
      }
    """
    tag = (tag or "").strip()
    state = _load_state()
    tracked = set(state["uids"])

    empty = {
        "tag": tag, "nodes": [], "links": [],
        "roles": {"producers": [], "participants": []},
        "verdict": {"kind": "scattered", "text": "该话题下没有库内用户的记录",
                    "participants": 0, "interactive_pairs": 0,
                    "possible_pairs": 0, "ratio": 0.0},
        "meta": {"videos": 0, "records": 0, "users": 0, "authors": 0,
                 "owner_coverage": 0.0, "truncated": False, "dropped_videos": 0},
        "note": "",
    }
    if not tag:
        empty["note"] = "请指定话题标签"
        return empty

    tinfo = state["topics"].get(tag)
    if not tinfo:
        empty["note"] = f"库内没有命中「{tag}」的评论或弹幕(话题来自视频标签, 需先补全视频信息)"
        return empty

    # 按热度取前 N 个视频, 后面所有统计都基于保留下来的视频, 保证图自洽
    ranked = sorted(tinfo["videos"].items(),
                    key=lambda kv: (-kv[1]["count"], -len(kv[1]["users"]), kv[0]))
    kept = dict(ranked[:max_videos])
    dropped = len(ranked) - len(kept)

    # ---- 统计 ----
    user_records = defaultdict(int)
    user_comments = defaultdict(int)
    user_danmu = defaultdict(int)
    user_videos = defaultdict(set)
    author_videos = defaultdict(int)
    author_records = defaultdict(int)
    known_owner = 0

    for oid, v in kept.items():
        owner = v.get("owner")
        if owner:
            known_owner += 1
            author_videos[owner] += 1
            author_records[owner] += v["count"]
        for uid, cnt in v["by_user"].items():
            user_records[uid] += cnt
            user_videos[uid].add(oid)
            kinds = v["kinds"].get(uid) or {}
            user_comments[uid] += kinds.get("comment", 0)
            user_danmu[uid] += kinds.get("danmu", 0)

    participants = sorted(user_records.keys(), key=lambda u: -user_records[u])
    # 关键节点 = 在话题下发言的库内用户 + 在话题下发布视频且本身也在库内的UP主
    key_nodes = set(participants)
    for owner in author_videos:
        if owner in tracked:
            key_nodes.add(owner)
    pairs = _interaction_pairs(kept, tracked, state.get("interactions") or {}, key_nodes)

    # ---- 节点 ----
    nodes = []
    links = []
    total_records = sum(user_records.values())

    nodes.append({
        "id": f"t:{tag}",
        "name": tag,
        "value": total_records,
        "category": "话题",
        "kind": "topic",
        "symbolSize": 46,
        "detail": {
            "records": total_records,
            "users": len(participants),
            "videos": len(kept),
        },
    })

    for oid, v in kept.items():
        name = v.get("bvid") or f"av{oid}"
        nodes.append({
            "id": f"v:{oid}",
            "name": name,
            "value": v["count"],
            "category": "视频",
            "kind": "video",
            "symbolSize": 18 + min(v["count"], 12) * 2,
            "detail": {
                "oid": oid,
                "bvid": v.get("bvid"),
                "tname": v.get("tname"),
                "owner": v.get("owner"),
                "records": v["count"],
                "users": sorted(v["users"]),
                "url": f"https://www.bilibili.com/video/av{oid}",
                "samples": v.get("samples") or [],
            },
        })
        links.append({
            "source": f"t:{tag}",
            "target": f"v:{oid}",
            "value": v["count"],
            "kind": "包含",
            "label": {"show": False, "formatter": "包含"},
        })

    for uid in sorted(key_nodes, key=lambda u: (-user_records.get(u, 0), u)):
        is_author = uid in author_videos
        nodes.append({
            "id": f"u:{uid}",
            "name": f"UID {uid}",
            "value": user_records[uid],
            "category": "用户",
            "kind": "user",
            "symbolSize": 20 + min(user_records[uid], 20) * 2,
            "detail": {
                "uid": uid,
                "records": user_records[uid],
                "comments": user_comments.get(uid, 0),
                "danmu": user_danmu.get(uid, 0),
                "videos": len(user_videos[uid]),
                "is_author": is_author,
                "author_videos": author_videos.get(uid, 0),
            },
        })
        for oid, v in kept.items():
            cnt = v["by_user"].get(uid)
            if not cnt:
                continue
            kinds = v["kinds"].get(uid) or {}
            edge_kind = "评论" if kinds.get("comment", 0) else "弹幕"
            links.append({
                "source": f"u:{uid}",
                "target": f"v:{oid}",
                "value": cnt,
                "kind": edge_kind,
                "label": {"show": False, "formatter": edge_kind},
            })

    # UP主节点(作者也在库内的合并进用户节点)
    for owner, vids in author_videos.items():
        if owner in tracked:
            continue
        nodes.append({
            "id": f"a:{owner}",
            "name": f"UP {owner}",
            "value": vids,
            "category": "UP主",
            "kind": "author",
            "symbolSize": 18 + min(vids, 10) * 3,
            "detail": {
                "uid": owner,
                "videos": vids,
                "records": author_records.get(owner, 0),
                "is_tracked": False,
            },
        })
        for oid, v in kept.items():
            if v.get("owner") == owner:
                links.append({
                    "source": f"v:{oid}",
                    "target": f"a:{owner}",
                    "value": 1,
                    "kind": "发布",
                    "label": {"show": False, "formatter": "发布"},
                })

    # 作者也在库内时: 视频指向用户节点
    for oid, v in kept.items():
        owner = v.get("owner")
        if owner and owner in tracked:
            links.append({
                "source": f"v:{oid}",
                "target": f"u:{owner}",
                "value": 1,
                "kind": "发布",
                "label": {"show": False, "formatter": "发布"},
            })

    # 关键节点之间的直接互动
    for (a, b), info in pairs.items():
        total = info["shared_videos"] + info["on_video"] + info["same_thread"]
        if info["on_video"] >= info["shared_videos"] and info["on_video"] >= info["same_thread"]:
            edge_kind = "互评"
        elif info["same_thread"]:
            edge_kind = "同楼"
        else:
            edge_kind = "共现"
        links.append({
            "source": f"u:{a}",
            "target": f"u:{b}",
            "value": total,
            "kind": edge_kind,
            "label": {"show": True, "formatter": edge_kind},
            "detail": info,
        })

    # ---- 角色榜单 ----
    producers = []
    for owner, vids in author_videos.items():
        producers.append({
            "uid": owner,
            "videos": vids,
            "records": author_records.get(owner, 0),
            "is_tracked": owner in tracked,
            "comments": user_comments.get(owner, 0),
            "danmu": user_danmu.get(owner, 0),
        })
    producers.sort(key=lambda x: (-x["videos"], -x["records"], x["uid"]))

    participant_roles = [{
        "uid": uid,
        "records": user_records.get(uid, 0),
        "comments": user_comments.get(uid, 0),
        "danmu": user_danmu.get(uid, 0),
        "videos": len(user_videos.get(uid, ())),
        "is_author": uid in author_videos,
    } for uid in sorted(key_nodes, key=lambda u: (-user_records.get(u, 0), u))]
    participant_roles.sort(key=lambda x: (-x["records"], x["uid"]))

    verdict = _verdict(sorted(key_nodes), pairs)
    verdict["pairs"] = [
        {"a": a, "b": b, "shared_videos": info["shared_videos"],
         "on_video": info["on_video"], "same_thread": info["same_thread"],
         "total": info["shared_videos"] + info["on_video"] + info["same_thread"]}
        for (a, b), info in sorted(
            pairs.items(),
            key=lambda kv: -(kv[1]["shared_videos"] + kv[1]["on_video"] + kv[1]["same_thread"]))
    ]

    notes = []
    if dropped:
        notes.append(f"视频过多, 只保留热度最高的 {len(kept)} 个(共 {len(ranked)} 个)")
    if kept and known_owner < len(kept):
        pct = round(known_owner / len(kept) * 100, 1)
        notes.append(f"UP主信息只覆盖 {known_owner}/{len(kept)} 个视频({pct}%), 生产者榜单不完整")
    if not participants:
        notes.append("该话题下没有库内用户的发言记录")
    if len(key_nodes) == 1:
        notes.append("只有 1 个库内用户参与, 建议换一个跨用户的话题")

    return {
        "tag": tag,
        "nodes": nodes,
        "links": links,
        "roles": {
            "producers": producers[:TOP_ROLES],
            "participants": participant_roles[:TOP_ROLES],
        },
        "verdict": verdict,
        "meta": {
            "videos": len(kept),
            "total_videos": len(ranked),
            "records": total_records,
            "users": len(participants),
            "key_nodes": len(key_nodes),
            "authors": len(author_videos),
            "owner_coverage": round(known_owner / len(kept) * 100, 1) if kept else 0.0,
            "truncated": dropped > 0,
            "dropped_videos": dropped,
        },
        "note": "；".join(notes),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        tag = sys.argv[1]
    else:
        cands = topic_candidates(limit=10)
        print("可用话题(参与用户数最多的前 10 个):")
        for c in cands:
            print("  %-14s 用户 %2d 人 | 记录 %4d 条 | 视频 %3d 个"
                  % (c["tag"], c["users"], c["records"], c["videos"]))
        print("\n用法: python topic_graph.py <标签>")
        sys.exit(0)

    g = build_graph(tag)
    print(f"\n=== 话题图谱: {tag} ===")
    print("节点 %d | 连线 %d" % (len(g["nodes"]), len(g["links"])))
    print("元信息:", g["meta"])
    print("\n生产者:")
    for p in g["roles"]["producers"]:
        print("  UID %-16s 发布 %d 个视频 | 相关记录 %d 条%s"
              % (p["uid"], p["videos"], p["records"], "(同时在库)" if p["is_tracked"] else ""))
    print("\n参与者:")
    for p in g["roles"]["participants"]:
        print("  UID %-16s 发言 %d 条(评论 %d / 弹幕 %d) | 覆盖 %d 个视频%s"
              % (p["uid"], p["records"], p["comments"], p["danmu"], p["videos"],
                 "(同时是UP主)" if p["is_author"] else ""))
    v = g["verdict"]
    print("\n判定: %s  (互动用户对 %d/%d, 占比 %.0f%%)"
          % (v["text"], v["interactive_pairs"], v["possible_pairs"], v["ratio"] * 100))
    for p in v["pairs"]:
        print("  %s <-> %s  共现 %d 个视频 | 互评 %d 次 | 同楼 %d 次"
              % (p["a"], p["b"], p["shared_videos"], p["on_video"], p["same_thread"]))
    if g["note"]:
        print("\n提示:", g["note"])
