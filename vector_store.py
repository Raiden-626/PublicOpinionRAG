"""Chroma 向量库封装: 入库 + 检索。
metadata 中携带 mysql 表名与主键 id,便于检索后回查 MySQL 拿原文与完整上下文。
"""
import chromadb
from config import CHROMA_PATH, COLLECTION

_client = chromadb.PersistentClient(path=CHROMA_PATH)
_collection = _client.get_or_create_collection(
    name=COLLECTION,
    metadata={"hnsw:space": "cosine"},  # 余弦相似度,适合短文本语义召回
)


def add(ids, embeddings, documents, metadatas):
    """批量写入向量。四个参数均为等长 list。"""
    _collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )


def query(query_embedding, top_k=8, where=None):
    """检索 top_k。where 可按 uid/vid/type 等过滤(值需为原始类型)。
    返回 list[dict]: {id, document, metadata, distance}。
    """
    res = _collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where,
    )
    out = []
    for i in range(len(res["ids"][0])):
        out.append({
            "id": res["ids"][0][i],
            "document": res["documents"][0][i],
            "metadata": res["metadatas"][0][i],
            "distance": res["distances"][0][i],
        })
    return out


def query_by_uid(query_embedding, uid, top_k=8):
    """按 uid 过滤的语义检索。不使用 Chroma where 子句(大整数不可靠),
    改为多取候选后在 Python 侧过滤。
    返回 list[dict]: {id, document, metadata, distance}。
    """
    candidates = top_k * 3  # 多取 3 倍,过滤后仍保留 top_k
    res = _collection.query(
        query_embeddings=[query_embedding],
        n_results=candidates,
    )
    out = []
    uid_int, uid_str = int(uid), str(uid)
    for i in range(len(res["ids"][0])):
        m = res["metadatas"][0][i]
        u = m.get("uid")
        if u != uid_int and u != uid_str:
            continue
        out.append({
            "id": res["ids"][0][i],
            "document": res["documents"][0][i],
            "metadata": m,
            "distance": res["distances"][0][i],
        })
        if len(out) >= top_k:
            break
    return out


def _get_all():
    """拉取集合中全部向量(无 where 过滤)。数据量小,性能无碍。"""
    return _collection.get()


def _filter_by_uid(metadatas, uid, mysql_table=None):
    """在 Python 侧按 uid(及可选 mysql_table)过滤 metadata 列表。
    同时匹配 int 和 str 两种类型,防止 Chroma 存成字符串。
    返回满足条件的 (index, metadata) 对列表。
    """
    uid_int, uid_str = int(uid), str(uid)
    hits = []
    for i, m in enumerate(metadatas):
        u = m.get("uid")
        if u != uid_int and u != uid_str:
            continue
        if mysql_table is not None and m.get("mysql_table") != mysql_table:
            continue
        hits.append(i)
    return hits


def get_mysql_ids(where=None):
    """按 metadata 过滤,返回已有的 mysql_id 集合(无需 query vector)。
    用于 re_embed 时检查哪些行已有向量。
    注意: 不使用 Chroma where 子句(大整数 uid 不可靠),改为 Python 侧过滤。
    """
    all_data = _get_all()
    all_metas = all_data.get("metadatas") or []

    if where is None:
        return {m.get("mysql_id") for m in all_metas if m.get("mysql_id") is not None}

    # 解析 where 条件,在 Python 中匹配
    uid_val = None
    table_val = None
    if "$and" in where:
        for cond in where["$and"]:
            if "uid" in cond:
                uid_val = cond["uid"]
            if "mysql_table" in cond:
                table_val = cond["mysql_table"]
    else:
        uid_val = where.get("uid")
        table_val = where.get("mysql_table")

    indices = _filter_by_uid(all_metas, uid_val, mysql_table=table_val)
    return {all_metas[i].get("mysql_id") for i in indices if all_metas[i].get("mysql_id") is not None}


def count():
    return _collection.count()


def count_by_uid(uid, kind=None):
    """统计某 uid 在 Chroma 中的向量数量。kind: 'comment'/'danmu'/None(全部)。
    不使用 Chroma where 子句(大整数 uid 过滤不可靠),改为 Python 侧过滤。
    """
    all_data = _get_all()
    all_metas = all_data.get("metadatas") or []
    indices = _filter_by_uid(all_metas, uid)

    if kind is None:
        return len(indices)

    table_prefix = f"bilibili_{kind}_"
    return sum(1 for i in indices if all_metas[i].get("mysql_table", "").startswith(table_prefix))
