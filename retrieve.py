"""检索: 向量召回 + 关键词召回(混合) → 时间过滤 → rerank 重排 → 回查 MySQL 原文。

- 向量召回: 语义相似,适合口语化/换说法的表达
- 关键词召回: 字符级精确匹配,补足向量对专有名词、玩梗、ID 的漏检
- 时间过滤: 从问题解析时间范围(如"最近一个月"),优先在 Chroma 侧用
  metadata 的 ctime_ts 过滤(老数据无该字段时自动回退),并在回查到的
  ctime 上做最终过滤;过滤后为空则放弃过滤,保证有结果返回
- 重排: SiliconFlow rerank(bge-reranker-v2-m3)对混合候选统一打分,
  失败时回退到"向量命中优先(按距离)、关键词命中其次(按匹配数)"
"""
import re
from datetime import datetime, timedelta

from config import TOP_K, RERANK_ENABLED
from clients import embed, rerank as rerank_api
import vector_store
import db


def _kind_from_table(table_name):
    """从 per-uid 表名(如 bilibili_comment_123)推断 kind。"""
    if "comment" in table_name:
        return "comment"
    if "danmu" in table_name:
        return "danmu"
    return None


# ---- 时间解析 ----

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s):
    """把 0-99 范围内的中文/阿拉伯数字串转 int,如 '三'→3、'十二'→12、'7'→7。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(s, 0)


def _parse_time_range(question):
    """从问题解析时间范围,返回 (start_ts, end_ts) 或 None。
    支持: 今天/昨天/最近N天(周/星期/个月/月/年)/上周/上个月/上月/今年/
    具体日期(2025-01-15、2025年1月15日、1月15日)。
    """
    q = question or ""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def out(start, end):
        return (start.timestamp(), end.timestamp())

    if re.search(r"今天|当日", q):
        return out(today, now)
    if re.search(r"昨天", q):
        return out(today - timedelta(days=1), today)

    m = re.search(r"最近\s*([0-9一二两三四五六七八九十]+)\s*(天|日|周|星期|个?月|年)", q)
    if m:
        n = _cn_to_int(m.group(1))
        unit = m.group(2)
        days = n * {"天": 1, "日": 1, "周": 7, "星期": 7, "月": 30, "个月": 30, "年": 365}.get(unit, 1)
        return out(today - timedelta(days=days), now)

    if re.search(r"上一个?周|上周", q):
        week_start = today - timedelta(days=today.weekday() + 7)
        return out(week_start, week_start + timedelta(days=7))
    if re.search(r"上个?月", q):
        month_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_start = (month_end - timedelta(days=1)).replace(day=1)
        return out(month_start, month_end)
    if re.search(r"今年|今年以来", q):
        return out(datetime(now.year, 1, 1), now)

    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", q)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return out(d, d + timedelta(days=1))
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", q)
    if m:
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)))
            if d > now:
                d = d.replace(year=now.year - 1)
            return out(d, d + timedelta(days=1))
        except ValueError:
            pass
    return None


# ---- 关键词召回 ----

def _extract_keywords(question, max_kws=24):
    """从问题提取匹配关键词: 拉丁/数字词整取,中文短段整取、长段取字符 bigram。"""
    q = question or ""
    kws = set(re.findall(r"[A-Za-z0-9_]{2,}", q))
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        if len(seg) <= 3:
            kws.add(seg)
        else:
            kws.update(seg[i : i + 2] for i in range(len(seg) - 1))
    return sorted(kws, key=len, reverse=True)[:max_kws]


def _keyword_recall(question, uid, limit):
    """在该 uid 全量语料上做关键词匹配打分。
    返回 [(kind, mysql_id, score, ctime), ...] 按 score 降序,最多 limit 条。
    """
    kws = _extract_keywords(question)
    if not kws:
        return []
    scored = []
    for kind in ("comment", "danmu"):
        try:
            rows = db.fetch_all_contents(kind, uid)
        except Exception as e:
            print(f"[keyword] 读取 {kind} 语料失败, 跳过: {e}")
            rows = []
        for r in rows:
            content = r.get("content") or ""
            if not content:
                continue
            score = sum(1 for k in kws if k in content)
            if score:
                scored.append((kind, r["id"], score, r.get("ctime")))
    scored.sort(key=lambda x: (-x[2],))
    return scored[:limit]


# ---- 主流程 ----

def retrieve(question, uid, top_k=TOP_K):
    """对指定 uid 的语料做混合检索。返回 list[dict],按相关度降序。"""
    cand_k = max(top_k * 3, 24)
    time_range = _parse_time_range(question)

    # 1) 向量召回: 带时间条件优先,命中为空(老数据无 ctime_ts)则放弃时间条件
    where_extra = None
    if time_range:
        where_extra = [
            {"ctime_ts": {"$gte": time_range[0]}},
            {"ctime_ts": {"$lte": time_range[1]}},
        ]
    qvec = embed(question)
    hits = vector_store.query_by_uid(qvec, uid, top_k=cand_k, where_extra=where_extra)
    if time_range and not hits:
        hits = vector_store.query_by_uid(qvec, uid, top_k=cand_k)

    # 2) 候选池合并: key=(kind, mysql_id),向量命中记 distance,关键词命中记 kw_score
    cands = {}
    for h in hits:
        m = h["metadata"]
        table = m.get("mysql_table")
        kind = _kind_from_table(table) if table else None
        if kind is None or m.get("mysql_id") is None:
            continue
        key = (kind, m["mysql_id"])
        cands[key] = {
            "kind": kind,
            "mysql_id": m["mysql_id"],
            "document": h["document"],
            "distance": h["distance"],
            "kw_score": 0,
            "ctime": None,
            "oid": None,
            "bvid": None,
            "url": None,
        }
    for kind, mysql_id, score, ctime in _keyword_recall(question, uid, cand_k):
        key = (kind, mysql_id)
        if key in cands:
            cands[key]["kw_score"] = score
        else:
            cands[key] = {
                "kind": kind,
                "mysql_id": mysql_id,
                "document": None,
                "distance": None,
                "kw_score": score,
                "ctime": ctime,
                "oid": None,
                "bvid": None,
                "url": None,
            }

    if not cands:
        return []

    # 3) 回查 MySQL 原文,补全 ctime/url 等;关键词独有命中还要拼嵌入文档
    need_rows = {}
    for c in cands.values():
        need_rows.setdefault(c["kind"], []).append(c["mysql_id"])
    for kind, ids in need_rows.items():
        rows = db.fetch_rows(kind, uid, [i for i in ids if i is not None])
        for r in rows:
            c = cands.get((kind, r["id"]))
            if c is None:
                continue
            c["ctime"] = r.get("ctime")
            c["oid"] = r.get("oid")
            c["bvid"] = r.get("bvid")
            c["url"] = r.get("url")
            if c["document"] is None:
                d = r["ctime"].strftime("%Y-%m-%d") if r.get("ctime") else "未知时间"
                c["document"] = f"[{d}] {r['content']}"

    # 4) 时间过滤(基于回查到的 ctime);全被滤掉则放弃过滤
    cand_list = list(cands.values())
    if time_range:
        kept = [
            c
            for c in cand_list
            if c["ctime"] and time_range[0] <= c["ctime"].timestamp() <= time_range[1]
        ]
        if kept:
            cand_list = kept

    # 5) rerank 统一重排;失败回退:向量命中在前(按距离),关键词命中在后(按分数)
    if RERANK_ENABLED and len(cand_list) > 1:
        rr = rerank_api(question, [c["document"] or "" for c in cand_list], top_n=len(cand_list))
        if rr:
            order = [item["index"] for item in rr]
        else:
            order = None
    else:
        order = None
    if order is None:
        def _fallback_key(c):
            dist = c["distance"] if c["distance"] is not None else 99.0
            return (0 if c["distance"] is not None else 1, dist, -c["kw_score"])
        order = sorted(range(len(cand_list)), key=lambda i: _fallback_key(cand_list[i]))

    results = []
    for i in order[:top_k]:
        c = cand_list[i]
        results.append({
            "type": db.table_for(c["kind"], uid),
            "content": c["document"],
            "ctime": c["ctime"],
            "oid": c["oid"],
            "bvid": c["bvid"],
            "url": c["url"],
            "distance": c["distance"],
            "kw_score": c["kw_score"],
        })
    return results


def build_context(results, max_chars=6000):
    """拼成给 LLM 的上下文字符串,带序号。
    文档已包含日期前缀(如 "[2025-01-15] 内容"),直接使用。
    """
    parts, total = [], 0
    for i, r in enumerate(results, 1):
        kind = "评论" if "comment" in (r.get("type") or "") else "弹幕"
        # 文档已包含日期前缀,直接使用
        block = f"[{i}] {kind}: {r['content']}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)
