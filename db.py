"""MySQL 数据层: 建表 + CRUD。
每个 uid 独立表: bilibili_comment_{uid} / bilibili_danmu_{uid}。
Chroma 的 metadata 里存 (mysql_table, mysql_id),检索命中后用 mysql_id 回查此处拿原文与完整上下文。
"""
import hashlib
from datetime import datetime

import pymysql

from config import MYSQL

# ---- 表名工具 ----
def comment_table(uid):
    """返回该 uid 的评论表名。"""
    return f"bilibili_comment_{uid}"

def danmu_table(uid):
    """返回该 uid 的弹幕表名。"""
    return f"bilibili_danmu_{uid}"

def table_for(kind, uid):
    """kind='comment'/'danmu',返回对应表名。"""
    return comment_table(uid) if kind == "comment" else danmu_table(uid)

def history_table(uid):
    """返回该 uid 的历史记录表名。"""
    return f"bilibili_history_{uid}"


# ---- 建表 SQL 模板 ----
_COMMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS `{table}` (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid             BIGINT       NOT NULL COMMENT '被查询的B站用户uid',
  oid             BIGINT       DEFAULT NULL COMMENT '视频oid',
  bvid            VARCHAR(20)  DEFAULT NULL COMMENT '视频BV号(从链接解析)',
  rpid            BIGINT       DEFAULT NULL COMMENT '评论rpid(去重键)',
  root_id         BIGINT       DEFAULT NULL COMMENT '根评论id',
  content         TEXT         NOT NULL COMMENT '评论正文',
  ctime           DATETIME     DEFAULT NULL COMMENT '评论时间',
  category        VARCHAR(50)  DEFAULT NULL COMMENT '分区/分类',
  source          VARCHAR(50)  DEFAULT NULL COMMENT '数据来源,如 aicu.cc',
  url             VARCHAR(500) DEFAULT NULL COMMENT '直达链接',
  video_owner_uid BIGINT       DEFAULT NULL COMMENT '视频UP主uid(从B站API获取)',
  tid             INT          DEFAULT NULL COMMENT '视频分区ID',
  tname           VARCHAR(50)  DEFAULT NULL COMMENT '视频分区名',
  tags            JSON         DEFAULT NULL COMMENT '视频标签列表',
  raw             JSON         DEFAULT NULL COMMENT '原始字段备份',
  ingested_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_rpid (rpid),
  KEY idx_ctime (ctime),
  KEY idx_oid (oid),
  KEY idx_owner (video_owner_uid),
  KEY idx_tid (tid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_DANMU_SCHEMA = """
CREATE TABLE IF NOT EXISTS `{table}` (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid          BIGINT       NOT NULL COMMENT '被查询的B站用户uid',
  oid          BIGINT       DEFAULT NULL COMMENT '视频oid',
  bvid         VARCHAR(20)  DEFAULT NULL COMMENT '视频BV号',
  dmid         BIGINT       DEFAULT NULL COMMENT '弹幕dmid(去重键)',
  content      TEXT         NOT NULL COMMENT '弹幕正文',
  video_offset INT          DEFAULT NULL COMMENT '视频内时间偏移(秒)',
  ctime        DATETIME     DEFAULT NULL COMMENT '弹幕发送时间',
  mode         INT          DEFAULT NULL COMMENT '弹幕模式(1滚动/4底/5顶/6逆/7特殊)',
  color        INT          DEFAULT NULL COMMENT '弹幕颜色',
  fontsize     INT          DEFAULT NULL COMMENT '字号',
  extra        JSON         DEFAULT NULL COMMENT '彩色弹幕参数等额外字段',
  source       VARCHAR(50)  DEFAULT NULL,
  url          VARCHAR(500) DEFAULT NULL COMMENT '传送门链接',
  tid          INT          DEFAULT NULL COMMENT '视频分区ID',
  tname        VARCHAR(50)  DEFAULT NULL COMMENT '视频分区名',
  tags         JSON         DEFAULT NULL COMMENT '视频标签列表',
  ingested_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_dmid (dmid),
  KEY idx_ctime (ctime),
  KEY idx_oid (oid),
  KEY idx_tid (tid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS `{table}` (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid          BIGINT       NOT NULL COMMENT 'B站用户uid',
  kind         VARCHAR(20)  NOT NULL COMMENT '记录类型: focus/report/ask',
  title        VARCHAR(200) DEFAULT NULL COMMENT '标题/问题摘要',
  content      LONGTEXT     NOT NULL COMMENT '生成内容全文',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
  KEY idx_uid_kind (uid, kind),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_INGEST_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS `ingest_state` (
  uid          BIGINT       NOT NULL,
  kind         VARCHAR(20)  NOT NULL COMMENT 'comment/danmu',
  last_ctime   DATETIME     DEFAULT NULL COMMENT '上次入库的最新记录时间(watermark)',
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (uid, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_DEAD_VIDEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS `dead_video` (
  oid       BIGINT       NOT NULL COMMENT '视频oid',
  uid       BIGINT       DEFAULT NULL COMMENT '来源用户uid(可空)',
  kind      VARCHAR(20)  DEFAULT NULL COMMENT 'comment/danmu(可空)',
  reason    VARCHAR(100) DEFAULT NULL COMMENT 'B站返回的原因',
  marked_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (oid),
  KEY idx_uid (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def get_conn():
    return pymysql.connect(**MYSQL, autocommit=False)


def init_db():
    """建库。库不存在则创建。"""
    cfg = {k: v for k, v in MYSQL.items() if k != "database"}
    conn = pymysql.connect(**cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL['database']}` "
                "DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def ensure_tables(uid):
    """为该 uid 建评论表 + 弹幕表(若不存在)。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_COMMENT_SCHEMA.format(table=comment_table(uid)))
            cur.execute(_DANMU_SCHEMA.format(table=danmu_table(uid)))
            cur.execute(_HISTORY_SCHEMA.format(table=history_table(uid)))
        conn.commit()
    finally:
        conn.close()
    # 迁移: 为已有表补充新列
    migrate_comment_table(uid)
    migrate_danmu_table(uid)
    # 增量入库状态表
    ensure_ingest_state_table()


def _add_column_if_missing(conn, tbl, col_name, col_def, index_def=None):
    """辅助: 为表添加列(若不存在),可选附带索引。"""
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM `{tbl}` LIKE %s", (col_name,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE `{tbl}` ADD COLUMN {col_name} {col_def}")
            if index_def:
                cur.execute(f"ALTER TABLE `{tbl}` ADD {index_def}")
            print(f"[migrate] {tbl}: 已添加 {col_name} 列")


def migrate_comment_table(uid):
    """为已有评论表添加缺失列(video_owner_uid, tid, tname, tags)。"""
    tbl = comment_table(uid)
    conn = get_conn()
    try:
        _add_column_if_missing(conn, tbl, "video_owner_uid",
                               "BIGINT DEFAULT NULL COMMENT '视频UP主uid'",
                               "KEY idx_owner (video_owner_uid)")
        _add_column_if_missing(conn, tbl, "tid",
                               "INT DEFAULT NULL COMMENT '视频分区ID'",
                               "KEY idx_tid (tid)")
        _add_column_if_missing(conn, tbl, "tname",
                               "VARCHAR(50) DEFAULT NULL COMMENT '视频分区名'")
        _add_column_if_missing(conn, tbl, "tags",
                               "JSON DEFAULT NULL COMMENT '视频标签列表'")
        conn.commit()
    except pymysql.err.ProgrammingError:
        pass  # 表不存在,跳过
    finally:
        conn.close()


def migrate_danmu_table(uid):
    """为已有弹幕表添加缺失列(video_owner_uid, tid, tname, tags)。"""
    tbl = danmu_table(uid)
    conn = get_conn()
    try:
        _add_column_if_missing(conn, tbl, "video_owner_uid",
                               "BIGINT DEFAULT NULL COMMENT '视频UP主uid'",
                               "KEY idx_owner (video_owner_uid)")
        _add_column_if_missing(conn, tbl, "tid",
                               "INT DEFAULT NULL COMMENT '视频分区ID'",
                               "KEY idx_tid (tid)")
        _add_column_if_missing(conn, tbl, "tname",
                               "VARCHAR(50) DEFAULT NULL COMMENT '视频分区名'")
        _add_column_if_missing(conn, tbl, "tags",
                               "JSON DEFAULT NULL COMMENT '视频标签列表'")
        conn.commit()
    except pymysql.err.ProgrammingError:
        pass  # 表不存在,跳过
    finally:
        conn.close()


# ---- 增量入库状态 ----

def ensure_ingest_state_table():
    """创建 ingest_state 表(若不存在)。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_INGEST_STATE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_ingest_state(uid, kind):
    """获取增量入库的 watermark(上次最新 ctime)。返回 datetime 或 None。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_ctime FROM `ingest_state` WHERE uid=%s AND kind=%s",
                (uid, kind),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except pymysql.err.ProgrammingError:
        return None
    finally:
        conn.close()


def set_ingest_state(uid, kind, last_ctime):
    """更新增量入库的 watermark。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `ingest_state` (uid, kind, last_ctime) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE last_ctime=%s",
                (uid, kind, last_ctime, last_ctime),
            )
        conn.commit()
    finally:
        conn.close()


def clear_ingest_state(uid, kind=None):
    """清除增量入库状态。kind=None 时清除该 uid 的全部状态。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if kind:
                cur.execute(
                    "DELETE FROM `ingest_state` WHERE uid=%s AND kind=%s",
                    (uid, kind),
                )
            else:
                cur.execute(
                    "DELETE FROM `ingest_state` WHERE uid=%s",
                    (uid,),
                )
        conn.commit()
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()


# ---- 失效视频标记 ----
# B站 view 接口对"稿件不可见/已删除"的视频返回 62002 / -404, 这类视频再请求也拿不到分区信息。
# 标记后写入 dead_video 表, 交互查询与批量补全都会跳过, 省掉无意义的 API 请求。

def ensure_dead_video_table():
    """创建 dead_video 表(若不存在)。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_DEAD_VIDEO_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def mark_dead_videos(items):
    """记录失效视频。items: list[(oid, uid, kind, reason)]。返回新增条数(已存在的不重复计)。"""
    if not items:
        return 0
    ensure_dead_video_table()
    conn = get_conn()
    added = 0
    try:
        with conn.cursor() as cur:
            for oid, uid, kind, reason in items:
                cur.execute(
                    "INSERT IGNORE INTO `dead_video` (oid, uid, kind, reason) "
                    "VALUES (%s, %s, %s, %s)",
                    (oid, uid, kind, reason),
                )
                added += cur.rowcount
        conn.commit()
        return added
    finally:
        conn.close()


def get_dead_oids():
    """返回全部失效视频的 oid 集合。表不存在时返回空集合。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM `dead_video`")
            return {row[0] for row in cur.fetchall()}
    except pymysql.err.ProgrammingError:
        return set()
    finally:
        conn.close()


def clear_dead_videos(uid=None):
    """清除失效标记。uid=None 清除全部,否则只清除该 uid 记录的。返回删除行数。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if uid is None:
                cur.execute("DELETE FROM `dead_video`")
            else:
                cur.execute("DELETE FROM `dead_video` WHERE uid=%s", (uid,))
            deleted = cur.rowcount
        conn.commit()
        return deleted
    except pymysql.err.ProgrammingError:
        return 0
    finally:
        conn.close()


# ---- 写入 ----
def _fallback_key(row, kind):
    """当 rpid/dmid 为 None 时,用 uid+content+ctime 的哈希作为备选去重键。
    返回 int(取哈希前 8 位 hex 转 int),保证可存入 BIGINT 列。
    """
    raw = f"{row.get('uid')}|{row.get('content', '')}|{row.get('ctime', '')}"
    h = hashlib.md5(raw.encode()).hexdigest()[:8]
    return int(h, 16)


def _upsert(table, row, uniq_col):
    """按唯一键去重插入:已存在则跳过(INSERT IGNORE)。返回新分配的 id(已存在返回 None)。

    若 uniq_col 对应值为 None,使用 _fallback_key 生成备选键,避免 NULL 绕过 UNIQUE 约束。
    """
    # rpid/dmid 为 None 时用备选键,防止 UNIQUE(NULL) 多次插入
    if row.get(uniq_col) is None:
        row = dict(row)
        row[uniq_col] = _fallback_key(row, uniq_col)

    cols = list(row.keys())
    placeholders = ",".join(["%s"] * len(cols))
    col_list = ",".join(f"`{c}`" for c in cols)
    sql = (
        f"INSERT IGNORE INTO `{table}` ({col_list}) VALUES ({placeholders})"
    )
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [row[c] for c in cols])
            new_id = cur.lastrowid if cur.rowcount else None
        conn.commit()
        return new_id
    finally:
        conn.close()


def insert_comment(uid, row):
    """row: dict,键见评论表列。按 rpid 去重。返回新 id 或 None。"""
    return _upsert(comment_table(uid), row, "rpid")


def insert_danmu(uid, row):
    """row: dict。按 dmid 去重。返回新 id 或 None。"""
    return _upsert(danmu_table(uid), row, "dmid")


# ---- 计数 ----
def count_rows(uid):
    """返回该 uid 的评论数 + 弹幕数。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{comment_table(uid)}`", ())
            c = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM `{danmu_table(uid)}`", ())
            d = cur.fetchone()[0]
        return {"comment": c, "danmu": d}
    except pymysql.err.ProgrammingError:
        # 表不存在时返回 0
        return {"comment": 0, "danmu": 0}
    finally:
        conn.close()


def list_ingested_users():
    """列出库中已有数据的用户及其数据量(用户记录视图用)。

    数据来源: comment / danmu / history 三类表,取并集,保证只生成了历史记录
    但评论弹幕已清空的用户也能被列出。

    返回 list[dict]:
      uid, comment_count, danmu_count, has_focus, has_report, last_ctime
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=%s AND ("
                "TABLE_NAME LIKE 'bilibili_comment_%%' OR "
                "TABLE_NAME LIKE 'bilibili_danmu_%%' OR "
                "TABLE_NAME LIKE 'bilibili_history_%%')",
                (MYSQL["database"],),
            )
            uids = set()
            for (tbl,) in cur.fetchall():
                for prefix in ("bilibili_comment_", "bilibili_danmu_", "bilibili_history_"):
                    if tbl.startswith(prefix):
                        tail = tbl[len(prefix):]
                        if tail.isdigit():
                            uids.add(int(tail))
                        break

        users = []
        for uid in sorted(uids):
            info = {
                "uid": uid,
                "comment_count": 0,
                "danmu_count": 0,
                "has_focus": False,
                "has_report": False,
                "last_ctime": None,
            }
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        f"SELECT COUNT(*), MAX(ctime) FROM `{comment_table(uid)}`"
                    )
                    cnt, mx = cur.fetchone()
                    info["comment_count"] = int(cnt or 0)
                    if mx:
                        info["last_ctime"] = mx
                except pymysql.err.ProgrammingError:
                    pass
                try:
                    cur.execute(
                        f"SELECT COUNT(*), MAX(ctime) FROM `{danmu_table(uid)}`"
                    )
                    cnt, mx = cur.fetchone()
                    info["danmu_count"] = int(cnt or 0)
                    if mx and not info["last_ctime"]:
                        info["last_ctime"] = mx
                except pymysql.err.ProgrammingError:
                    pass
                try:
                    cur.execute(f"SELECT kind FROM `{history_table(uid)}`")
                    for (kind,) in cur.fetchall():
                        if kind == "focus":
                            info["has_focus"] = True
                        elif kind == "report":
                            info["has_report"] = True
                except pymysql.err.ProgrammingError:
                    pass
            users.append(info)

        # 最近有活动的排前面
        users.sort(
            key=lambda u: (u["last_ctime"] or datetime.min, u["uid"]),
            reverse=True,
        )
        return users
    finally:
        conn.close()


def fetch_recent_records(kind, uid, limit=20):
    """取该 uid 最近 limit 条评论/弹幕(按 ctime 倒序),只取展示需要的列。

    不复用 fetch_sample: 那个是 SELECT *, 会把 comment.raw / danmu.extra
    这些大 JSON 字段一起拖出来, 展示用不着。
    """
    tbl = table_for(kind, uid)
    if kind == "comment":
        cols = ("id, oid, bvid, rpid, content, ctime, url, "
                "tid, tname, tags, video_owner_uid")
    else:
        cols = "id, oid, bvid, dmid, content, ctime, url, tid, tname, tags"

    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {cols} FROM `{tbl}` "
                f"ORDER BY ctime DESC, id DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
    except pymysql.err.ProgrammingError:
        return []
    finally:
        conn.close()


def user_profile(uid):
    """用户画像: 按 tname(分区) 和 tags(标签) 聚合评论+弹幕,得到兴趣分布。

    返回 dict:
      tname_dist: [{"tname": str, "count": int, "pct": float}, ...]  按 count 降序
      tag_dist:   [{"tag": str, "count": int, "pct": float}, ...]    按 count 降序
      total:      int  (评论+弹幕总数)
    """
    import json as _json
    from collections import Counter

    tname_counter = Counter()
    tag_counter = Counter()
    total = 0

    conn = get_conn()
    try:
        for kind in ("comment", "danmu"):
            tbl = table_for(kind, uid)
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                try:
                    cur.execute(
                        f"SELECT tname, tags FROM `{tbl}` "
                        f"WHERE (tname IS NOT NULL AND tname <> '') OR tags IS NOT NULL"
                    )
                except pymysql.err.ProgrammingError:
                    continue
                rows = cur.fetchall()

            for row in rows:
                total += 1
                tname = row.get("tname")
                if tname and tname.strip():
                    tname_counter[tname.strip()] += 1
                tags_raw = row.get("tags")
                if tags_raw:
                    if isinstance(tags_raw, str):
                        try:
                            tags_list = _json.loads(tags_raw)
                        except Exception:
                            tags_list = []
                    elif isinstance(tags_raw, list):
                        tags_list = tags_raw
                    else:
                        tags_list = []
                    for tag in tags_list:
                        if isinstance(tag, str) and tag.strip():
                            tag_counter[tag.strip()] += 1
    finally:
        conn.close()

    # 转为排序列表
    def _to_dist(counter, key_name):
        items = []
        for name, cnt in counter.most_common():
            pct = round(cnt / total * 100, 1) if total else 0
            items.append({key_name: name, "count": cnt, "pct": pct})
        return items

    return {
        "tname_dist": _to_dist(tname_counter, "tname"),
        "tag_dist": _to_dist(tag_counter, "tag"),
        "total": total,
    }


# ---- 取样(报告生成用:按时间倒序取若干条)----
def fetch_sample(kind, uid, limit=200):
    """取该 uid 最近 limit 条(按 ctime 倒序)。返回 list[dict]。"""
    tbl = table_for(kind, uid)
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT * FROM `{tbl}` ORDER BY ctime DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
    except pymysql.err.ProgrammingError:
        return []
    finally:
        conn.close()


# ---- 回查(检索后取原文)----
def fetch_rows(kind, uid, ids):
    """按主键 id 批量回查完整行。返回 list[dict]。"""
    if not ids:
        return []
    tbl = table_for(kind, uid)
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT * FROM `{tbl}` WHERE id IN ({placeholders})",
                list(ids),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---- 向量化一致性辅助 ----
def fetch_all_ids(kind, uid):
    """返回 MySQL 中某 uid 表的全部主键 id 列表。"""
    tbl = table_for(kind, uid)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM `{tbl}`", ())
            return [row[0] for row in cur.fetchall()]
    except pymysql.err.ProgrammingError:
        return []
    finally:
        conn.close()


def fetch_ids_without_vector(kind, uid, vector_ids_set):
    """找出 MySQL 中有但 Chroma 中没有对应向量的行 id 列表。
    vector_ids_set: Chroma 中已有的 mysql_id 集合。
    返回 list[int]: 需要补向量化的 MySQL id。
    """
    tbl = table_for(kind, uid)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM `{tbl}`", ())
            all_ids = {row[0] for row in cur.fetchall()}
        return sorted(all_ids - vector_ids_set)
    except pymysql.err.ProgrammingError:
        return []
    finally:
        conn.close()


def fetch_rows_by_ids(kind, uid, ids):
    """按主键 id 列表取完整行(保证顺序与 ids 一致)。"""
    if not ids:
        return []
    tbl = table_for(kind, uid)
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT * FROM `{tbl}` WHERE id IN ({placeholders})",
                list(ids),
            )
            rows = cur.fetchall()
        row_map = {r["id"]: r for r in rows}
        return [row_map[i] for i in ids if i in row_map]
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("数据库已就绪:", MYSQL["database"])
    print("使用 ensure_tables(uid) 为特定用户建表")


# ---- 历史记录 CRUD ----

def save_history(uid, kind, content, title=None):
    """保存生成记录到历史表。kind: 'focus'/'report'/'ask'。
    每个 uid+kind 只保留一份最新记录,新生成会覆盖旧的。返回记录 id。
    """
    tbl = history_table(uid)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 先查找是否已有该 uid+kind 的记录
            cur.execute(
                f"SELECT id FROM `{tbl}` WHERE uid=%s AND kind=%s",
                (uid, kind),
            )
            existing = cur.fetchone()

            if existing:
                # 已有记录,更新内容
                record_id = existing[0]
                cur.execute(
                    f"UPDATE `{tbl}` SET title=%s, content=%s, created_at=NOW() WHERE id=%s",
                    (title, content, record_id),
                )
            else:
                # 无记录,插入新记录
                cur.execute(
                    f"INSERT INTO `{tbl}` (uid, kind, title, content) VALUES (%s, %s, %s, %s)",
                    (uid, kind, title, content),
                )
                record_id = cur.lastrowid
        conn.commit()
        return record_id
    finally:
        conn.close()


def list_history(uid, kind=None, limit=50):
    """列出该 uid 的历史记录,按时间倒序。可指定 kind 过滤。返回 list[dict]。"""
    tbl = history_table(uid)
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if kind:
                cur.execute(
                    f"SELECT id, kind, title, created_at FROM `{tbl}` WHERE kind=%s ORDER BY created_at DESC LIMIT %s",
                    (kind, limit),
                )
            else:
                cur.execute(
                    f"SELECT id, kind, title, created_at FROM `{tbl}` ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            return cur.fetchall()
    except pymysql.err.ProgrammingError:
        return []
    finally:
        conn.close()


def get_history(uid, record_id):
    """获取单条历史记录的完整内容。返回 dict 或 None。"""
    tbl = history_table(uid)
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT * FROM `{tbl}` WHERE id=%s",
                (record_id,),
            )
            return cur.fetchone()
    except pymysql.err.ProgrammingError:
        return None
    finally:
        conn.close()


def delete_history(uid, record_id):
    """删除单条历史记录。返回是否成功。"""
    tbl = history_table(uid)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{tbl}` WHERE id=%s", (record_id,))
            affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        conn.close()


def clear_uid_data(uid, kinds=("comment", "danmu")):
    """清空指定 uid 的评论和弹幕数据,同时重置增量入库状态。返回删除的行数。
    用于覆盖模式:先清空再重新入库。
    """
    conn = get_conn()
    total_deleted = 0
    try:
        with conn.cursor() as cur:
            for kind in kinds:
                tbl = table_for(kind, uid)
                try:
                    cur.execute(f"SELECT COUNT(*) FROM `{tbl}`", ())
                    count = cur.fetchone()[0]
                    cur.execute(f"DELETE FROM `{tbl}`", ())
                    conn.commit()
                    total_deleted += count
                    print(f"[clear] {tbl}: 删除 {count} 行")
                except pymysql.err.ProgrammingError:
                    pass  # 表不存在,跳过
        return total_deleted
    finally:
        # 重置增量入库状态
        for kind in kinds:
            clear_ingest_state(uid, kind)
        conn.close()
