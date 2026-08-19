"""检索: query 向量化 → Chroma 召回 top-k → 按 metadata 回查 MySQL 原文。
Chroma metadata 里存了 mysql_table 与 mysql_id,命中后用它回查拿完整行。
"""
from config import TOP_K
from clients import embed
import vector_store
import db


def _kind_from_table(table_name):
    """从 per-uid 表名(如 bilibili_comment_123)推断 kind。"""
    if "comment" in table_name:
        return "comment"
    if "danmu" in table_name:
        return "danmu"
    return None


def retrieve(question, uid, top_k=TOP_K):
    """对指定 uid 的语料做语义检索。返回 list[dict],按相似度升序。"""
    qvec = embed(question)
    hits = vector_store.query(qvec, top_k=top_k, where={"uid": int(uid)})
    
    # 调试: 打印检索结果数量
    print(f"[DEBUG] retrieve: uid={uid}, question='{question[:30]}...', hits={len(hits)}")
    if hits:
        print(f"[DEBUG]   第一条: id={hits[0]['id']}, distance={hits[0]['distance']:.4f}")
        print(f"[DEBUG]   metadata: {hits[0]['metadata']}")

    # 按 mysql_table 分组,批量回查原文
    by_table = {}
    for h in hits:
        m = h["metadata"]
        t = m.get("mysql_table")
        by_table.setdefault(t, []).append((m.get("mysql_id"), h))

    results = []
    for t, items in by_table.items():
        if t is None:
            continue
        kind = _kind_from_table(t)
        if kind is None:
            continue
        ids = [i[0] for i in items if i[0] is not None]
        rows = db.fetch_rows(kind, uid, ids)
        row_map = {r["id"]: r for r in rows}
        for mysql_id, h in items:
            r = row_map.get(mysql_id, {})
            results.append({
                "type": t,
                "content": h["document"],
                "ctime": r.get("ctime"),
                "oid": r.get("oid"),
                "bvid": r.get("bvid"),
                "url": r.get("url"),
                "distance": h["distance"],
            })
    results.sort(key=lambda x: x["distance"])
    return results


def build_context(results, max_chars=6000):
    """拼成给 LLM 的上下文字符串,带序号与时间。"""
    parts, total = [], 0
    for i, r in enumerate(results, 1):
        kind = "评论" if "comment" in (r.get("type") or "") else "弹幕"
        ctime = r["ctime"].strftime("%Y-%m-%d") if r.get("ctime") else "未知时间"
        block = f"[{i}] {kind} · {ctime}\n{r['content']}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)
