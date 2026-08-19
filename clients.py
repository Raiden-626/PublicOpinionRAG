"""SiliconFlow API 客户端: 文本向量化(bge-large-zh) + LLM 对话(DeepSeek)。"""
import json

import requests
from config import API_KEY, API_BASE, LLM_MODEL, EMBED_MODEL

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


def chat(messages, temperature=0.3, max_tokens=2048, timeout=300):
    """LLM 对话(非流式,兼容旧调用)。messages: [{"role": "user"/"system", "content": "..."}] -> str。"""
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
    out = []
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
            out.append(delta)
    return "".join(out)


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
