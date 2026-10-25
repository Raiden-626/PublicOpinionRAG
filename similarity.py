"""用户兴趣相似度分析（第二层：群体发现与对比）。

核心口径:
  把每个用户表示为一个标签向量, 每个标签的权重 = 该标签下的评论数 / 该用户总记录数,
  再乘以 IDF 权重 log((N+1)/(df+1)) 抑制"搞笑/原创"这类人人都有的泛标签,
  最后用余弦相似度衡量两个用户的关注领域重合程度。

为什么加 IDF:
  不加 IDF 时, 出现在大多数用户里的泛标签会和"梅西"这种专精标签获得同等权重,
  实测会让"搞笑"贡献 84.7% 的相似度, 造成大量假阳性(两个泛娱乐用户被拉成一组)。
  加 IDF 后泛标签(df=N)权重归零, 专精标签的相对贡献被放大,
  相似度分布从"连续递减"变成"断层结构", 阈值判定才稳定。

分区的重叠情况(tname)作为辅助信息单独返回, 不参与打分。

本模块只做计算, 数据来源统一走 db.user_profile()。
"""

import math
import time

import db

# 返回结果中各列表最多保留的条目数
SHARED_TOP = 20
DISTINCT_TOP = 15
TNAME_TOP = 10

# 相似度等级划分阈值(IDF 口径, 与原始 tf 口径的数值不可直接比较)
LEVELS = [
    (0.30, "高度相似"),
    (0.12, "较为相似"),
    (0.05, "关注接近"),
    (0.00, "差异明显"),
]

# 有效记录数低于此值时提示样本过小
MIN_SAMPLE = 5

# 标签分布会随视频信息补全而变化, 缓存 5 分钟即可
CACHE_TTL = 300

# 低于这个用户数时不做 IDF(样本太少, df≈N 会让所有标签权重归零)
MIN_IDF_USERS = 3

_state = {
    "key": None,       # 入库用户集合签名
    "ts": 0.0,         # 缓存生成时间
    "profiles": {},    # uid -> 画像
    "vectors": {},     # uid -> IDF 加权后的标签向量
    "raw_vectors": {}, # uid -> 原始 tf 标签向量(展示用)
    "idf": {},         # 标签 -> IDF 权重
    "df": {},          # 标签 -> 出现在多少个用户里
}


def build_vector(dist, total, name_key="key"):
    """把分布列表转成归一化的稀疏权重字典。

    dist:     [{"tag"/"tname": 名称, "count": 次数, "pct": 百分比}, ...]
    total:    参与统计的总记录数
    name_key: 名称所在的字段名(标签用 "tag", 分区用 "tname")

    返回 {名称: 权重}, 权重 = count / total (各标签权重之和可能大于 1, 因为一条记录带多个标签)
    """
    if not total:
        return {}
    vec = {}
    for item in dist:
        name = item.get(name_key)
        count = item.get("count") or 0
        if not name or count <= 0:
            continue
        vec[name] = count / total
    return vec


def apply_idf(vec, idf):
    """给标签向量乘上 IDF 权重。idf 里没有的标签按 1.0 处理。"""
    if not vec:
        return {}
    if not idf:
        return dict(vec)
    return {tag: w * idf.get(tag, 1.0) for tag, w in vec.items()}


def cosine(v1, v2):
    """稀疏向量的余弦相似度, 返回 0~1 的浮点数。

    返回 None 表示无法计算(任一向量为空)。
    """
    if not v1 or not v2:
        return None
    dot = 0.0
    if len(v1) <= len(v2):
        small, large = v1, v2
    else:
        small, large = v2, v1
    for key, w in small.items():
        w2 = large.get(key)
        if w2:
            dot += w * w2

    n1 = math.sqrt(sum(w * w for w in v1.values()))
    n2 = math.sqrt(sum(w * w for w in v2.values()))
    if n1 == 0 or n2 == 0:
        return None
    if dot == 0:
        return 0.0
    return dot / (n1 * n2)


def _level_of(score):
    if score is None:
        return ""
    for threshold, name in LEVELS:
        if score >= threshold:
            return name
    return LEVELS[-1][1]


def _sample(prof):
    """参与统计的有效记录数(有分区/标签的记录条数)。

    db.user_profile 返回的 total 是评论+弹幕的总条数, 而分布计数只覆盖带分区/标签的记录,
    所以权重分母与样本量判定必须用 annotated, 否则权重被稀释、条数还原也会算错。
    兼容旧数据(没有 annotated 键时退回 total)。
    """
    if not prof:
        return 0
    val = prof.get("annotated")
    if val is None:
        val = prof.get("total", 0)
    return val or 0


def _profile(uid):
    """取用户画像, 失败时返回空画像而不是抛错。"""
    try:
        profile = db.user_profile(uid)
    except Exception as e:
        print(f"[similarity] 获取 uid={uid} 画像失败: {e}")
        return {"tname_dist": [], "tag_dist": [], "total": 0, "annotated": 0}
    return profile or {"tname_dist": [], "tag_dist": [], "total": 0, "annotated": 0}


def _ingested_uids():
    """库中已有数据的用户 uid 列表。"""
    try:
        return [u["uid"] for u in db.list_ingested_users()]
    except Exception as e:
        print(f"[similarity] 读取入库用户列表失败: {e}")
        return []


def _compute_df(profiles):
    """标签出现在多少个用户里(按用户计数, 不按记录计数)。"""
    df = {}
    for prof in profiles.values():
        for item in prof.get("tag_dist", []) or []:
            tag = item.get("tag")
            if tag:
                df[tag] = df.get(tag, 0) + 1
    return df


def _compute_idf(df, n_users):
    """IDF 权重 = log((N+1)/(df+1))。

    df=N(人人都有)的标签权重为 0, 正好被剔除; 用户数太少时不做 IDF。
    """
    if n_users < MIN_IDF_USERS:
        return {tag: 1.0 for tag in df}
    return {tag: math.log((n_users + 1) / (cnt + 1)) for tag, cnt in df.items()}


def load_state(force=False):
    """加载全体入库用户的画像 / 标签向量 / IDF 权重, 带 TTL 缓存。

    返回 dict: {key, ts, profiles, vectors, idf, df}
    """
    uids = _ingested_uids()
    key = tuple(sorted(uids))
    now = time.time()
    if (not force and _state["key"] == key and _state["ts"]
            and now - _state["ts"] < CACHE_TTL):
        return _state

    profiles = {uid: _profile(uid) for uid in uids}
    df = _compute_df(profiles)
    idf = _compute_idf(df, len(profiles))
    raw_vectors = {}
    vectors = {}
    for uid, prof in profiles.items():
        vec = build_vector(prof.get("tag_dist", []) or [], _sample(prof), "tag")
        raw_vectors[uid] = vec
        vectors[uid] = apply_idf(vec, idf)

    _state.update({
        "key": key,
        "ts": now,
        "profiles": profiles,
        "vectors": vectors,
        "raw_vectors": raw_vectors,
        "idf": idf,
        "df": df,
    })
    return _state


def _vector_of(uid, state):
    """取某个用户的 IDF 加权标签向量; 不在入库列表里的用户临时现算。"""
    vec = state["vectors"].get(uid)
    if vec is not None:
        return vec
    prof = state["profiles"].get(uid) or _profile(uid)
    return apply_idf(build_vector(prof.get("tag_dist", []) or [], _sample(prof), "tag"),
                     state.get("idf") or {})


def _raw_vector_of(uid, state):
    """取某个用户的原始(tf)标签向量, 用于展示条数与占比。"""
    raw = state.get("raw_vectors", {}).get(uid)
    if raw is not None:
        return raw
    prof = state["profiles"].get(uid) or _profile(uid)
    return build_vector(prof.get("tag_dist", []) or [], _sample(prof), "tag")


def _profile_of(uid, state):
    prof = state["profiles"].get(uid)
    if prof is None:
        prof = _profile(uid)
    return prof


def compare(uid_a, uid_b):
    """比较两个用户的关注领域相似度。

    返回 dict:
      {
        "score": float|None,      # 相似度 0~1; None 表示数据不足无法计算
        "level": str,             # 等级描述
        "sample_a": int,          # A 参与计算的有效记录数
        "sample_b": int,
        "shared_tags": [...],     # 共同关注点
        "distinct_a": [...],      # A 有而 B 没有的标签
        "distinct_b": [...],      # B 有而 A 没有的标签
        "tname_shared": [...],    # 共同活跃分区
        "tname_only_a": [...],
        "tname_only_b": [...],
        "note": str,              # 数据不足/样本过小时的提示
      }
    """
    state = load_state()
    prof_a = _profile_of(uid_a, state)
    prof_b = _profile_of(uid_b, state)

    sample_a = _sample(prof_a)
    sample_b = _sample(prof_b)

    raw_a = _raw_vector_of(uid_a, state)
    raw_b = _raw_vector_of(uid_b, state)
    tag_a = _vector_of(uid_a, state)   # IDF 加权, 参与余弦
    tag_b = _vector_of(uid_b, state)
    part_a = build_vector(prof_a.get("tname_dist", []) or [], sample_a, "tname")
    part_b = build_vector(prof_b.get("tname_dist", []) or [], sample_b, "tname")

    score = cosine(tag_a, tag_b)
    level = _level_of(score)

    notes = []
    if sample_a == 0:
        notes.append(f"UID {uid_a} 暂无可用的分区/标签数据(需先抓取入库并补全视频信息)")
    if sample_b == 0:
        notes.append(f"UID {uid_b} 暂无可用的分区/标签数据(需先抓取入库并补全视频信息)")
    if score is not None:
        if sample_a < MIN_SAMPLE:
            notes.append(f"UID {uid_a} 有效样本仅 {sample_a} 条, 结果仅供参考")
        if sample_b < MIN_SAMPLE:
            notes.append(f"UID {uid_b} 有效样本仅 {sample_b} 条, 结果仅供参考")

    # 共同关注点: 条数/占比取原始 tf 口径, 排序取 IDF 加权后的贡献度
    shared = []
    for tag in set(tag_a) & set(tag_b):
        shared.append({
            "tag": tag,
            "count_a": int(round(raw_a.get(tag, 0.0) * sample_a)),
            "count_b": int(round(raw_b.get(tag, 0.0) * sample_b)),
            "pct_a": round(raw_a.get(tag, 0.0) * 100, 1),
            "pct_b": round(raw_b.get(tag, 0.0) * 100, 1),
            "contribution": tag_a[tag] * tag_b[tag],
        })
    shared.sort(key=lambda x: (-x["contribution"], x["tag"]))
    for item in shared:
        item.pop("contribution", None)
    shared = shared[:SHARED_TOP]

    def _distinct(src, other, total):
        items = []
        for name, w in src.items():
            if name in other:
                continue
            items.append({
                "tag": name,
                "count": int(round(w * total)),
                "pct": round(w * 100, 1),
            })
        items.sort(key=lambda x: (-x["count"], x["tag"]))
        return items[:DISTINCT_TOP]

    distinct_a = _distinct(raw_a, raw_b, sample_a)
    distinct_b = _distinct(raw_b, raw_a, sample_b)

    part_shared = [p for p in part_a if p in part_b]
    part_only_a = [p for p in part_a if p not in part_b]
    part_only_b = [p for p in part_b if p not in part_a]

    return {
        "score": round(score, 4) if score is not None else None,
        "level": level,
        "sample_a": sample_a,
        "sample_b": sample_b,
        "shared_tags": shared,
        "distinct_a": distinct_a,
        "distinct_b": distinct_b,
        "tname_shared": part_shared[:TNAME_TOP],
        "tname_only_a": part_only_a[:TNAME_TOP],
        "tname_only_b": part_only_b[:TNAME_TOP],
        "note": "；".join(notes),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python similarity.py <uid_a> <uid_b>")
        sys.exit(1)
    ua, ub = int(sys.argv[1]), int(sys.argv[2])
    r = compare(ua, ub)
    print(f"\n=== UID {ua} -> {ub} 兴趣相似度(IDF加权) ===")
    if r["score"] is None:
        print("无法计算相似度")
    else:
        print(f"相似度: {r['score'] * 100:.1f}%  ({r['level']})")
    print(f"有效样本: A={r['sample_a']} 条, B={r['sample_b']} 条")
    if r["shared_tags"]:
        print("\n共同关注点:")
        for item in r["shared_tags"]:
            print(f"  {item['tag']}: A {item['count_a']}条 / B {item['count_b']}条")
    if r["distinct_a"]:
        print(f"\n仅 A 关注:")
        for item in r["distinct_a"]:
            print(f"  {item['tag']}: {item['count']}条 ({item['pct']}%)")
    if r["distinct_b"]:
        print(f"\n仅 B 关注:")
        for item in r["distinct_b"]:
            print(f"  {item['tag']}: {item['count']}条 ({item['pct']}%)")
    if r["tname_shared"]:
        print(f"\n共同活跃分区: {', '.join(r['tname_shared'])}")
    if r["note"]:
        print(f"\n提示: {r['note']}")
