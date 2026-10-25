"""存量数据离线修复工具(零 API 请求, 全部走 MySQL 本地数据)。

修复项:
1) repair_rpid(uids, write)
   背景: 早期抓取逻辑把评论卡里"最后一个 #reply/?root 链接的 id"写进了 rpid。
        这个 id 有时是评论自身的 id, 有时是被回复的评论楼 id(多个用户会共享同一个楼 id),
        导致 UNIQUE(rpid) 去重键不可靠(同一用户的不同评论可能撞键, 也可能把楼 id 当成自己的 id)。
   修复: rpid 归位成"评论自身 id"(取自 url 的 #reply<id> 片段, 全库核对过: 689 行全部唯一),
        原来的 rpid 迁到 root_id(评论楼 id)。
   不变量: 修复前后 COALESCE(root_id, rpid) 完全相等 —— 即"评论楼 id"不变,
          互动分析/话题图谱的同楼判定结果不受影响, 只是去重键变正确了。

2) repair_dead_meta(write)
   给 dead_video 表补 uid/kind(回查该 oid 出自哪些用户表)。
   运行期发现视频失效的路径拿不到 uid, 历史上都记成了 NULL, 导致没法按用户清理。

用法:
    python main.py repair-rpid --all --write     # 真正写库
    python main.py repair-rpid --all             # 只预览, 不写库
    python main.py repair-dead --write
"""
import re

# 直达链接里的 #reply<id> —— 评论自身的 id
_OWN_RE = re.compile(r"#reply(\d+)")


def own_id_from_url(url):
    """从评论直达链接里取评论自身 id(#reply<id>)。取不到返回 None。"""
    if not url:
        return None
    m = _OWN_RE.search(url)
    return int(m.group(1)) if m else None


def _comment_tables(uids=None):
    """返回 [(uid, 表名)], 只含评论表。"""
    import db
    import pymysql

    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'bilibili_comment_%%'",
                (db.MYSQL["database"],),
            )
            tables = [r[0] for r in cur.fetchall()]
        out = []
        for tbl in tables:
            uid_str = tbl[len("bilibili_comment_"):]
            if not uid_str.isdigit():
                continue
            uid = int(uid_str)
            if uids is not None and uid not in uids:
                continue
            out.append((uid, tbl))
        return out
    finally:
        conn.close()


def repair_rpid(uids=None, write=False):
    """把评论表的 rpid 归位成评论自身 id, 旧 rpid 迁到 root_id。

    参数:
      uids:  只修指定的 uid 列表; None = 全部
      write: True 才真正写库, False 只统计(预览)

    返回 dict: {scanned, updated, would_update, skipped, no_own, conflicts}
    """
    import db
    import pymysql

    stats = {
        "scanned": 0,
        "updated": 0,
        "would_update": 0,
        "skipped": 0,
        "no_own": 0,
        "conflicts": 0,
    }

    for uid, tbl in _comment_tables(uids):
        conn = db.get_conn()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(f"SELECT id, rpid, root_id, url FROM `{tbl}`")
                rows = cur.fetchall()

            plan = []
            seen_own = {}
            for row in rows:
                stats["scanned"] += 1
                own = own_id_from_url(row.get("url"))
                if own is None:
                    # url 里没有 #reply 片段, 无从判断自身 id, 保持原样
                    stats["no_own"] += 1
                    continue

                old_rpid = row.get("rpid")
                # 评论楼 id: 旧 rpid 若就是自身 id, 说明是顶层评论, 楼即自身;
                # 否则旧 rpid 就是被回复的楼 id, 原样迁到 root_id
                new_root = old_rpid if (old_rpid is not None and old_rpid != own) else own

                if own in seen_own:
                    # 同表内自身 id 重复 → 归位后会撞 UNIQUE(rpid), 跳过并报告
                    stats["conflicts"] += 1
                    print(f"[repair-rpid] {tbl}: id={row['id']} 的自身 id={own} "
                          f"与 id={seen_own[own]} 重复, 跳过")
                    continue
                seen_own[own] = row["id"]

                if old_rpid == own and row.get("root_id") == new_root:
                    stats["skipped"] += 1
                    continue
                plan.append((row["id"], own, new_root))

            if not plan:
                continue

            if write:
                with conn.cursor() as cur:
                    for row_id, new_rpid, new_root in plan:
                        cur.execute(
                            f"UPDATE `{tbl}` SET rpid=%s, root_id=%s WHERE id=%s",
                            (new_rpid, new_root, row_id),
                        )
                conn.commit()
                stats["updated"] += len(plan)
                print(f"[repair-rpid] {tbl}: 已更新 {len(plan)} 行")
            else:
                stats["would_update"] += len(plan)
                print(f"[repair-rpid] {tbl}: 待更新 {len(plan)} 行(未写库)")
        finally:
            conn.close()

    return stats


def repair_dead_meta(write=False):
    """给 dead_video 表里 uid/kind 为空的行补上来源用户。"""
    import db
    import pymysql

    stats = {"scanned": 0, "updated": 0, "would_update": 0, "skipped": 0, "unresolved": 0}

    conn = db.get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT oid, uid, kind FROM `dead_video` WHERE uid IS NULL OR kind IS NULL")
            rows = cur.fetchall()

        for row in rows:
            stats["scanned"] += 1
            sources = db.find_oid_sources(row["oid"])
            if not sources:
                stats["unresolved"] += 1
                print(f"[repair-dead] oid={row['oid']}: 库里已无对应记录, 保持原样")
                continue
            uid, kind = sources[0]
            if row.get("uid") == uid and row.get("kind") == kind:
                stats["skipped"] += 1
                continue
            if write:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE `dead_video` SET uid=%s, kind=%s WHERE oid=%s",
                        (uid, kind, row["oid"]),
                    )
                conn.commit()
                stats["updated"] += 1
            else:
                stats["would_update"] += 1
            print(f"[repair-dead] oid={row['oid']}: uid={uid} kind={kind}"
                  f"{'' if write else '(未写库)'}")

        return stats
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="存量数据离线修复")
    ap.add_argument("what", choices=["rpid", "dead"], help="修复项")
    ap.add_argument("--all", action="store_true", help="修所有用户")
    ap.add_argument("--uid", type=int, help="只修某个 uid")
    ap.add_argument("--write", action="store_true", help="真正写库(默认只预览)")
    args = ap.parse_args()

    if args.what == "rpid":
        target = [args.uid] if args.uid else (None if args.all else None)
        st = repair_rpid(uids=target, write=args.write)
        print(f"\nrpid 归位: 扫描 {st['scanned']}, 已更新 {st['updated']}, "
              f"待更新 {st['would_update']}, 无需改 {st['skipped']}, "
              f"无自身id {st['no_own']}, 冲突 {st['conflicts']}")
    else:
        st = repair_dead_meta(write=args.write)
        print(f"\ndead_video 补全: 扫描 {st['scanned']}, 已更新 {st['updated']}, "
              f"待更新 {st['would_update']}, 无需改 {st['skipped']}, 找不到来源 {st['unresolved']}")
