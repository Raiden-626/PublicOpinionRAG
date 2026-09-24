"""用户互动分析: 查询两个B站用户之间的评论互动关系。

互动类型:
  1. A 在 B 的视频下发评论 (video_owner_uid = B, 评论者 = A)
  2. A 回复 B 的评论 (reply_to_uid = B, 评论者 = A)
  3. B 在 A 的视频下发评论 (反向)
  4. B 回复 A 的评论 (反向)

数据来源: MySQL 中已入库的评论数据 + B站API实时补充视频UP主信息。
"""

import pymysql

import db
from ingest import get_video_owner


def _query_interactions_from_table(table, commenter_uid, target_uid):
    """从指定评论表中查找 commenter_uid 与 target_uid 之间的互动。

    返回 list[dict],每条包含:
      - type: "on_video"(在对方视频下评论) 或 "reply"(回复对方评论)
      - content: 评论内容
      - oid: 视频oid
      - bvid: BV号
      - ctime: 评论时间
      - url: 链接
      - like_count: 点赞数
    """
    conn = db.get_conn()
    results = []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 类型1: commenter 在 target 的视频下发评论
            cur.execute(
                f"SELECT id, content, oid, bvid, ctime, url, like_count "
                f"FROM `{table}` WHERE uid=%s AND video_owner_uid=%s "
                f"ORDER BY ctime DESC",
                (commenter_uid, target_uid),
            )
            for row in cur.fetchall():
                results.append({
                    "type": "on_video",
                    "commenter_uid": commenter_uid,
                    "target_uid": target_uid,
                    "content": row["content"],
                    "oid": row["oid"],
                    "bvid": row["bvid"],
                    "ctime": row["ctime"].strftime("%Y-%m-%d %H:%M") if row.get("ctime") else None,
                    "url": row["url"],
                    "like_count": row["like_count"],
                    "mysql_id": row["id"],
                })

            # 类型2: commenter 回复 target 的评论
            cur.execute(
                f"SELECT id, content, oid, bvid, ctime, url, like_count "
                f"FROM `{table}` WHERE uid=%s AND reply_to_uid=%s "
                f"ORDER BY ctime DESC",
                (commenter_uid, target_uid),
            )
            for row in cur.fetchall():
                results.append({
                    "type": "reply",
                    "commenter_uid": commenter_uid,
                    "target_uid": target_uid,
                    "content": row["content"],
                    "oid": row["oid"],
                    "bvid": row["bvid"],
                    "ctime": row["ctime"].strftime("%Y-%m-%d %H:%M") if row.get("ctime") else None,
                    "url": row["url"],
                    "like_count": row["like_count"],
                    "mysql_id": row["id"],
                })
    except pymysql.err.ProgrammingError:
        pass  # 表不存在或列不存在
    finally:
        conn.close()
    return results


def _try_resolve_missing_owners(uid):
    """尝试为该 uid 评论表中 video_owner_uid 为 NULL 的记录补充UP主信息。
    返回新解析的数量。
    """
    tbl = db.comment_table(uid)
    conn = db.get_conn()
    resolved = 0
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 取 oid 不重复且 video_owner_uid 为 NULL 的记录
            cur.execute(
                f"SELECT DISTINCT id, oid FROM `{tbl}` "
                f"WHERE video_owner_uid IS NULL AND oid IS NOT NULL LIMIT 200"
            )
            rows = cur.fetchall()
        if not rows:
            return 0

        # 批量解析(利用缓存)
        oid_owner_map = {}
        for row in rows:
            oid = row["oid"]
            if oid not in oid_owner_map:
                owner = get_video_owner(oid)
                oid_owner_map[oid] = owner

        # 回写数据库
        with conn.cursor() as cur:
            for row in rows:
                owner = oid_owner_map.get(row["oid"])
                if owner is not None:
                    cur.execute(
                        f"UPDATE `{tbl}` SET video_owner_uid=%s WHERE id=%s",
                        (owner, row["id"]),
                    )
                    if cur.rowcount:
                        resolved += 1
        conn.commit()
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()
    return resolved


def find_interactions(uid_a, uid_b, resolve_owners=True):
    """查询两个用户之间的所有互动记录。

    参数:
      uid_a, uid_b: 两个B站用户uid
      resolve_owners: 是否先尝试补充缺失的video_owner_uid(会调用B站API)

    返回 dict:
      {
        "uid_a": int,
        "uid_b": int,
        "a_to_b": [...],  # A→B 的互动
        "b_to_a": [...],  # B→A 的互动
        "summary": {...}, # 统计摘要
        "resolved_owners": int,  # 新解析的UP主数量
      }
    """
    uid_a, uid_b = int(uid_a), int(uid_b)
    resolved = 0

    # 可选: 先补充缺失的视频UP主(实时调用B站API)
    if resolve_owners:
        for uid in (uid_a, uid_b):
            tbl = db.comment_table(uid)
            # 检查表是否存在
            conn = db.get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM `{tbl}` LIMIT 1")
                resolved += _try_resolve_missing_owners(uid)
            except pymysql.err.ProgrammingError:
                pass
            finally:
                conn.close()

    # 查询互动记录
    a_to_b = []
    b_to_a = []

    # 从 A 的评论表查 A→B 的互动
    a_table = db.comment_table(uid_a)
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM `{a_table}` LIMIT 1")
        a_to_b.extend(_query_interactions_from_table(a_table, uid_a, uid_b))
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()

    # 从 B 的评论表查 B→A 的互动
    b_table = db.comment_table(uid_b)
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM `{b_table}` LIMIT 1")
        b_to_a.extend(_query_interactions_from_table(b_table, uid_b, uid_a))
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()

    # 同时交叉查: 从 A 的表查 B→A, 从 B 的表查 A→B
    # (如果 A 的表中有 B 发的评论, 或 B 的表中有 A 发的评论)
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM `{b_table}` LIMIT 1")
        a_to_b.extend(_query_interactions_from_table(b_table, uid_a, uid_b))
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()

    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM `{a_table}` LIMIT 1")
        b_to_a.extend(_query_interactions_from_table(a_table, uid_b, uid_a))
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()

    # 统计摘要
    a_on_video = [r for r in a_to_b if r["type"] == "on_video"]
    a_reply = [r for r in a_to_b if r["type"] == "reply"]
    b_on_video = [r for r in b_to_a if r["type"] == "on_video"]
    b_reply = [r for r in b_to_a if r["type"] == "reply"]

    summary = {
        "a_to_b_total": len(a_to_b),
        "a_on_b_video": len(a_on_video),
        "a_reply_to_b": len(a_reply),
        "b_to_a_total": len(b_to_a),
        "b_on_a_video": len(b_on_video),
        "b_reply_to_a": len(b_reply),
        "total": len(a_to_b) + len(b_to_a),
    }

    return {
        "uid_a": uid_a,
        "uid_b": uid_b,
        "a_to_b": a_to_b,
        "b_to_a": b_to_a,
        "summary": summary,
        "resolved_owners": resolved,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python interactions.py <uid_a> <uid_b>")
        sys.exit(1)
    ua, ub = int(sys.argv[1]), int(sys.argv[2])
    result = find_interactions(ua, ub)
    s = result["summary"]
    print(f"\n=== UID {ua} ↔ UID {ub} 互动分析 ===")
    print(f"A→B: {s['a_to_b_total']} 条 (在视频下评论: {s['a_on_b_video']}, 回复评论: {s['a_reply_to_b']})")
    print(f"B→A: {s['b_to_a_total']} 条 (在视频下评论: {s['b_on_a_video']}, 回复评论: {s['b_reply_to_a']})")
    print(f"总计: {s['total']} 条互动")
    if result["resolved_owners"]:
        print(f"(新解析了 {result['resolved_owners']} 条视频的UP主)")
    for label, records in [("A→B", result["a_to_b"]), ("B→A", result["b_to_a"])]:
        if records:
            print(f"\n--- {label} ---")
            for r in records[:20]:
                tp = "在视频下评论" if r["type"] == "on_video" else "回复评论"
                print(f"  [{r['ctime'] or '未知'}] {tp}: {r['content'][:50]}")
