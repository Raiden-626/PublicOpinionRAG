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


def get_mysql_ids(where):
    """按 metadata 过滤,返回已有的 mysql_id 集合(无需 query vector)。
    用于 re_embed 时检查哪些行已有向量。
    """
    res = _collection.get(where=where)
    ids = set()
    for m in (res.get("metadatas") or []):
        mid = m.get("mysql_id")
        if mid is not None:
            ids.add(mid)
    return ids


def count():
    return _collection.count()


def count_by_uid(uid, kind=None):
    """统计某 uid 在 Chroma 中的向量数量。kind: 'comment'/'danmu'/None(全部)。"""
    where = {"uid": int(uid)}
    if kind:
        # mysql_table 形如 bilibili_comment_123 或 bilibili_danmu_123
        table_prefix = f"bilibili_{kind}_"
        where["mysql_table"] = {"$contains": table_prefix}
    res = _collection.get(where=where)
    return len(res.get("ids") or [])
