"""用户互动分析: 查询两个B站用户之间的评论互动关系。

互动类型:
  1. on_video  — A 在 B 的视频下发评论 (video_owner_uid = B)
  2. same_thread — A 与 B 在同一评论楼中出现 (评论楼 id 相同)

数据来源: MySQL 中已入库的评论数据 + B站API实时补充视频UP主信息。
"""

import pymysql

import config
import db
import similarity
from ingest import get_video_info, dead_oids


def _query_interactions_from_table(table, commenter_uid, target_uid):
    """从指定评论表中查找 commenter_uid 在 target_uid 视频下发评论的记录。

    返回 list[dict],每条包含:
      - type: "on_video"
      - content, oid, bvid, ctime, url, mysql_id
    """
    conn = db.get_conn()
    results = []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT id, content, oid, bvid, ctime, url, tid, tname, tags "
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
                    "tid": row.get("tid"),
                    "tname": row.get("tname"),
                    "tags": row.get("tags"),
                    "mysql_id": row["id"],
                })
    except pymysql.err.ProgrammingError:
        pass  # 表不存在或列不存在
    finally:
        conn.close()
    return results


def _query_same_thread(table_a, table_b, uid_a, uid_b):
    """查找两个用户在同一评论楼中的记录。

    "评论楼 id" = COALESCE(root_id, rpid):
      - 修复后 rpid 是评论自身 id, 楼由 root_id 标识(根评论的 root_id = 自身 rpid)
      - 未修复的旧数据 root_id 为空, 此时旧 rpid 存的正是楼 id, COALESCE 仍然正确

    返回 (a_records, b_records)。
    """
    a_records, b_records = [], []
    conn = db.get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 取 A 表中所有非空评论楼 id
            cur.execute(
                f"SELECT DISTINCT COALESCE(root_id, rpid) AS thread_id "
                f"FROM `{table_a}` WHERE COALESCE(root_id, rpid) IS NOT NULL"
            )
            a_rpids = {row["thread_id"] for row in cur.fetchall()}
            if not a_rpids:
                return a_records, b_records

            # 取 B 表中所有非空评论楼 id
            cur.execute(
                f"SELECT DISTINCT COALESCE(root_id, rpid) AS thread_id "
                f"FROM `{table_b}` WHERE COALESCE(root_id, rpid) IS NOT NULL"
            )
            b_rpids = {row["thread_id"] for row in cur.fetchall()}

            # 交集: 两人共同出现的评论楼
            shared_rpids = a_rpids & b_rpids
            if not shared_rpids:
                return a_records, b_records

            placeholders = ",".join(["%s"] * len(shared_rpids))
            args = list(shared_rpids)

            # 从 A 的表取记录
            cur.execute(
                f"SELECT id, uid, content, oid, bvid, ctime, url, rpid, root_id, tid, tname, tags "
                f"FROM `{table_a}` WHERE COALESCE(root_id, rpid) IN ({placeholders}) "
                f"ORDER BY ctime DESC",
                args,
            )
            for row in cur.fetchall():
                a_records.append({
                    "type": "same_thread",
                    "commenter_uid": uid_a,
                    "target_uid": uid_b,
                    "content": row["content"],
                    "oid": row["oid"],
                    "bvid": row["bvid"],
                    "ctime": row["ctime"].strftime("%Y-%m-%d %H:%M") if row.get("ctime") else None,
                    "url": row["url"],
                    "rpid": row["rpid"],
                    "root_id": row.get("root_id"),
                    "tid": row.get("tid"),
                    "tname": row.get("tname"),
                    "tags": row.get("tags"),
                    "mysql_id": row["id"],
                })

            # 从 B 的表取记录
            cur.execute(
                f"SELECT id, uid, content, oid, bvid, ctime, url, rpid, root_id, tid, tname, tags "
                f"FROM `{table_b}` WHERE COALESCE(root_id, rpid) IN ({placeholders}) "
                f"ORDER BY ctime DESC",
                args,
            )
            for row in cur.fetchall():
                b_records.append({
                    "type": "same_thread",
                    "commenter_uid": uid_b,
                    "target_uid": uid_a,
                    "content": row["content"],
                    "oid": row["oid"],
                    "bvid": row["bvid"],
                    "ctime": row["ctime"].strftime("%Y-%m-%d %H:%M") if row.get("ctime") else None,
                    "url": row["url"],
                    "rpid": row["rpid"],
                    "root_id": row.get("root_id"),
                    "tid": row.get("tid"),
                    "tname": row.get("tname"),
                    "tags": row.get("tags"),
                    "mysql_id": row["id"],
                })
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()
    return a_records, b_records


def _try_resolve_missing_owners(uid):
    """尝试为该 uid 评论/弹幕表中缺失视频信息的记录补充 UP主/分区/标签。
    返回新解析的数量。

    已确认失效的视频(稿件不可见/已删除)会被跳过, 不再请求 B站 API。
    """
    import json

    total_resolved = 0

    # 已确认失效的视频: 开关开启时才会跳过
    dead = dead_oids() if config.SKIP_DEAD_VIDEOS else set()

    # 同时处理评论表和弹幕表
    tables = [db.comment_table(uid), db.danmu_table(uid)]

    for tbl in tables:
        kind = "danmu" if tbl == db.danmu_table(uid) else "comment"
        conn = db.get_conn()
        resolved = 0
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                if tbl == db.danmu_table(uid):
                    # 弹幕表的 video_owner_uid 不参与互动匹配, 只补 tid/tname/tags
                    cur.execute(
                        f"SELECT DISTINCT id, oid FROM `{tbl}` "
                        f"WHERE tid IS NULL AND oid IS NOT NULL LIMIT 50"
                    )
                else:
                    # 取 oid 不重复且 video_owner_uid 或 tid 为 NULL 的记录
                    cur.execute(
                        f"SELECT DISTINCT id, oid FROM `{tbl}` "
                        f"WHERE (video_owner_uid IS NULL OR tid IS NULL) AND oid IS NOT NULL LIMIT 50"
                    )
                rows = cur.fetchall()
            # 失效视频直接排除, 不再发请求也不回写
            if dead:
                rows = [r for r in rows if r["oid"] not in dead]
            if not rows:
                continue

            # 批量解析(利用缓存)
            oid_info_map = {}
            for row in rows:
                oid = row["oid"]
                if oid not in oid_info_map:
                    oid_info_map[oid] = get_video_info(oid, uid=uid, kind=kind)

            # 回写数据库
            with conn.cursor() as cur:
                for row in rows:
                    info = oid_info_map.get(row["oid"], {})
                    owner = info.get("owner_uid")
                    tid = info.get("tid")
                    tname = info.get("tname")
                    tags = info.get("tags")
                    
                    # 只要有任何一个字段需要更新就执行
                    # 用 COALESCE 兜底: 新值为 NULL 时保留库里已有值, 防止把已填好的字段覆盖成 NULL
                    if owner is not None or tid is not None or tname is not None or tags is not None:
                        cur.execute(
                            f"UPDATE `{tbl}` SET "
                            f"video_owner_uid=COALESCE(%s, video_owner_uid), "
                            f"tid=COALESCE(%s, tid), "
                            f"tname=COALESCE(%s, tname), "
                            f"tags=COALESCE(%s, tags) "
                            f"WHERE id=%s",
                            (owner, tid, tname,
                             json.dumps(tags, ensure_ascii=False) if tags else None,
                             row["id"]),
                        )
                        if cur.rowcount:
                            resolved += 1
            conn.commit()
        except pymysql.err.ProgrammingError:
            pass
        finally:
            conn.close()
        
        if resolved > 0:
            print(f"[resolve] {tbl} 补全了 {resolved} 条记录的视频信息")
        total_resolved += resolved
    
    return total_resolved


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
        "similarity": {...}, # 兴趣相似度与关注点对比
      }
    """
    uid_a, uid_b = int(uid_a), int(uid_b)
    resolved = 0

    # 可选: 先补充缺失的视频UP主(实时调用B站API)
    if resolve_owners:
        for uid in (uid_a, uid_b):
            tbl = db.comment_table(uid)
            conn = db.get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM `{tbl}` LIMIT 1")
            except pymysql.err.ProgrammingError:
                conn.close()
                continue
            conn.close()
            resolved += _try_resolve_missing_owners(uid)

    # 查询互动记录(单连接完成全部查询)
    a_to_b = []
    b_to_a = []
    a_table = db.comment_table(uid_a)
    b_table = db.comment_table(uid_b)

    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            # 检查两张表是否存在
            a_exists, b_exists = False, False
            try:
                cur.execute(f"SELECT 1 FROM `{a_table}` LIMIT 1")
                a_exists = True
            except pymysql.err.ProgrammingError:
                pass
            try:
                cur.execute(f"SELECT 1 FROM `{b_table}` LIMIT 1")
                b_exists = True
            except pymysql.err.ProgrammingError:
                pass

        # A 的表: 查 A→B 和 B→A 的 on_video
        if a_exists:
            a_to_b.extend(_query_interactions_from_table(a_table, uid_a, uid_b))
            b_to_a.extend(_query_interactions_from_table(a_table, uid_b, uid_a))
        # B 的表: 查 B→A 和 A→B 的 on_video
        if b_exists:
            b_to_a.extend(_query_interactions_from_table(b_table, uid_b, uid_a))
            a_to_b.extend(_query_interactions_from_table(b_table, uid_a, uid_b))

        # 同楼互动(rpid 相同)
        if a_exists and b_exists:
            thread_a, thread_b = _query_same_thread(a_table, b_table, uid_a, uid_b)
            # 去重: 排除已通过 on_video 匹配到的记录
            seen_a = {r["mysql_id"] for r in a_to_b}
            seen_b = {r["mysql_id"] for r in b_to_a}
            for r in thread_a:
                if r["mysql_id"] not in seen_a:
                    a_to_b.append(r)
                    seen_a.add(r["mysql_id"])
            for r in thread_b:
                if r["mysql_id"] not in seen_b:
                    b_to_a.append(r)
                    seen_b.add(r["mysql_id"])
    finally:
        conn.close()

    # 统计摘要
    a_on_video = [r for r in a_to_b if r["type"] == "on_video"]
    a_thread = [r for r in a_to_b if r["type"] == "same_thread"]
    b_on_video = [r for r in b_to_a if r["type"] == "on_video"]
    b_thread = [r for r in b_to_a if r["type"] == "same_thread"]

    summary = {
        "a_to_b_total": len(a_to_b),
        "a_on_b_video": len(a_on_video),
        "a_same_thread": len(a_thread),
        "b_to_a_total": len(b_to_a),
        "b_on_a_video": len(b_on_video),
        "b_same_thread": len(b_thread),
        "total": len(a_to_b) + len(b_to_a),
    }

    # 兴趣相似度与关注点对比(基于标签向量余弦相似度)
    sim = similarity.compare(uid_a, uid_b)

    return {
        "uid_a": uid_a,
        "uid_b": uid_b,
        "a_to_b": a_to_b,
        "b_to_a": b_to_a,
        "summary": summary,
        "resolved_owners": resolved,
        "similarity": sim,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python interactions.py <uid_a> <uid_b>")
        sys.exit(1)
    ua, ub = int(sys.argv[1]), int(sys.argv[2])
    result = find_interactions(ua, ub)
    s = result["summary"]
    print(f"\n=== UID {ua} -> UID {ub} 互动分析 ===")
    print(f"A→B: {s['a_to_b_total']} 条 (在视频下评论: {s['a_on_b_video']}, 同楼互动: {s['a_same_thread']})")
    print(f"B→A: {s['b_to_a_total']} 条 (在视频下评论: {s['b_on_a_video']}, 同楼互动: {s['b_same_thread']})")
    print(f"总计: {s['total']} 条互动")
    if result["resolved_owners"]:
        print(f"(新解析了 {result['resolved_owners']} 条视频的UP主)")

    sim = result.get("similarity") or {}
    if sim.get("score") is not None:
        print(f"兴趣相似度: {sim['score'] * 100:.1f}% ({sim.get('level')}) "
              f"[有效样本 A={sim.get('sample_a')} B={sim.get('sample_b')}]")
        if sim.get("shared_tags"):
            shared = "、".join(
                f"{t['tag']}({t['count_a']}/{t['count_b']})" for t in sim["shared_tags"][:5]
            )
            print(f"共同关注点: {shared}")
    else:
        print("兴趣相似度: 无法计算")
    if sim.get("note"):
        print(f"提示: {sim['note']}")

    _TYPE_LABEL = {"on_video": "在视频下评论", "same_thread": "同楼互动"}
    for label, records in [("A→B", result["a_to_b"]), ("B→A", result["b_to_a"])]:
        if records:
            print(f"\n--- {label} ---")
            for r in records[:20]:
                tp = _TYPE_LABEL.get(r["type"], r["type"])
                print(f"  [{r['ctime'] or '未知'}] {tp}: {r['content'][:50]}")
