"""SiliconFlow API 客户端: 文本向量化(bge-large-zh) + LLM 对话(DeepSeek) + 重排。"""
import json

import requests
from config import API_KEY, API_BASE, LLM_MODEL, EMBED_MODEL, RERANK_MODEL

_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def embed(texts):
    """批量向量化。
    texts: str 或 list[str] -> list[list[float]](与输入顺序一致)。
    """
    single = isinstance(texts, str)
    if single:
        texts = [texts]
    resp = requests.post(
        f"{API_BASE}/embeddings",
        headers=_HEADERS,
        json={"model": EMBED_MODEL, "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    data.sort(key=lambda x: x["index"])  # 按 index 排序保证与输入顺序一致
    vecs = [d["embedding"] for d in data]
    return vecs[0] if single else vecs


def rerank(query, documents, top_n=None, timeout=30):
    """重排: 用 bge-reranker 对 query 与候选文档打相关性分。
    返回 [{"index": i, "relevance_score": s}, ...](按分数降序, index 为 documents 下标)。
    失败时返回 None, 由调用方回退到原始排序。
    """
    if not documents:
        return None
    try:
        resp = requests.post(
            f"{API_BASE}/rerank",
            headers=_HEADERS,
            json={
                "model": RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n or len(documents),
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["results"]
    except Exception as e:
        print(f"[rerank] 重排失败, 回退到向量距离排序: {e}")
        return None


def chat_stream(messages, temperature=0.3, max_tokens=2048, timeout=300):
    """LLM 流式对话。生成器,逐 token yield str。

    timeout 是两次分片间的间隔上限(非总超时):只要模型持续输出,
    read 计时器就不会到期,从而支持长报告而不被整段超时打断。
    """
    resp = requests.post(
        f"{API_BASE}/chat/completions",
        headers=_HEADERS,
        json={
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        },
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=False):
        if not line:
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not text.startswith("data:"):
            continue
        data = text[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
        if delta:
            yield delta
