"""MySQL 数据层: 建表 + CRUD。
每个 uid 独立表: bilibili_comment_{uid} / bilibili_danmu_{uid}。
Chroma 的 metadata 里存 (mysql_table, mysql_id),检索命中后用 mysql_id 回查此处拿原文与完整上下文。
"""
import hashlib
import json
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
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid          BIGINT       NOT NULL COMMENT '被查询的B站用户uid',
  oid          BIGINT       DEFAULT NULL COMMENT '视频oid',
  bvid         VARCHAR(20)  DEFAULT NULL COMMENT '视频BV号(从链接解析)',
  rpid         BIGINT       DEFAULT NULL COMMENT '评论rpid(去重键)',
  root_id      BIGINT       DEFAULT NULL COMMENT '根评论id',
  content      TEXT         NOT NULL COMMENT '评论正文',
  ctime        DATETIME     DEFAULT NULL COMMENT '评论时间',
  like_count   INT          DEFAULT NULL COMMENT '点赞数',
  category     VARCHAR(50)  DEFAULT NULL COMMENT '分区/分类',
  source       VARCHAR(50)  DEFAULT NULL COMMENT '数据来源,如 aicu.cc',
  url          VARCHAR(500) DEFAULT NULL COMMENT '直达链接',
  raw          JSON         DEFAULT NULL COMMENT '原始字段备份',
  ingested_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_rpid (rpid),
  KEY idx_ctime (ctime),
  KEY idx_oid (oid)
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
  ingested_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_dmid (dmid),
  KEY idx_ctime (ctime),
  KEY idx_oid (oid)
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
    """保存生成记录到历史表。kind: 'focus'/'report'/'ask'。返回新记录 id。"""
    tbl = history_table(uid)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{tbl}` (uid, kind, title, content) VALUES (%s, %s, %s, %s)",
                (uid, kind, title, content),
            )
            new_id = cur.lastrowid
        conn.commit()
        return new_id
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
