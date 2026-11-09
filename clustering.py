"""第二层: 群体发现(阈值连通分量聚类)。

思路:
  把每个已入库用户看成标签向量空间里的一个点, 两两算余弦相似度(IDF 加权),
  相似度 >= 阈值的两个用户连一条边, 连通分量就是一个兴趣群体。
  群体命名用成员共现最多、且不是泛标签的几个标签, 例如「足球/梅西/C罗」。

为什么用连通分量而不是层次聚类:
  实测在当前数据上两者切出的群体完全一致, 而连通分量实现更简单、
  可解释性更强(群体成员之间存在直接的相似关系), 且不引入 scipy/sklearn 依赖。

群体分级:
  2 人 -> 兴趣对(pair), 3 人及以上 -> 群体(group);
  若组内最弱一对低于弱关联档(链式合并), 标记为松散群体(loose)。

阈值分档:
  默认按库内全体用户对的分数分布动态划档(分位数 + 绝对下限取 max, 从严),
  pair 太少或分布退化时回退固定阈值。余弦分数是"库内相对值", 库变化时
  动态阈值会跟着走, 比手工常数可靠。

数据来源统一走 similarity.load_state()(画像/向量/IDF 都来自 db.user_profile)。
"""

import math

import db
import similarity

# 兜底固定阈值(pair 太少或分布退化时使用, 与原手工分档一致)
FALLBACK_THRESHOLDS = {"high": 0.30, "mid": 0.12, "weak": 0.05}

# 动态阈值的绝对下限(从严): 无论分布如何, 各档不得低于此值
ABS_FLOOR = {"high": 0.08, "mid": 0.05, "weak": 0.02}

# 分布至少需要这么多 pair 才可靠(对应约 5 个用户)
MIN_PAIRS_FOR_DYNAMIC = 10

# 分布方差低于此值视为退化(所有 pair 分数几乎一样, 分位数失去意义)
MIN_VAR = 1e-6

# 3 人及以上算群体, 2 人算兴趣对
GROUP_MIN_SIZE = 3

# 有效记录数低于此值的用户不参与聚类(样本太少, 向量不可靠)
MIN_SAMPLE = 5

# 群体命名使用的标签数量
TOP_TAGS = 3

# df 超过 用户数×该比例 的标签视为泛标签, 不用于命名
GENERIC_DF_RATIO = 0.5


def _eligible_uids(state):
    """可以参与聚类的用户: 有向量且有效样本足够。"""
    result = []
    for uid, prof in state["profiles"].items():
        if not state["vectors"].get(uid):
            continue
        if similarity._sample(prof) < MIN_SAMPLE:
            continue
        result.append(uid)
    return sorted(result)


def _pair_scores(vectors, uids):
    """两两相似度, 返回 {(a, b): score}, a < b。"""
    scores = {}
    for i, a in enumerate(uids):
        for b in uids[i + 1:]:
            s = similarity.cosine(vectors[a], vectors[b])
            if s is None:
                continue
            scores[(a, b)] = s
    return scores


def _percentile(sorted_vals, q):
    """升序列表的 q 分位(0~1), 线性插值。"""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(n - 1, lo + 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _thresholds_from_distribution(scores):
    """按全库 pair 分数分布动态划档(从严: 分位数与绝对下限取 max)。

    分布不可靠(pair 太少/方差退化)时回退固定阈值。
    返回 {high, mid, weak, source, pairs, p50, p75, p90, p95}。
    """
    vals = sorted(scores.values())
    n = len(vals)

    def fixed():
        out = dict(FALLBACK_THRESHOLDS)
        out.update({"source": "fixed", "pairs": n})
        return out

    if n < MIN_PAIRS_FOR_DYNAMIC:
        return fixed()
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    if var < MIN_VAR:
        return fixed()

    p50 = _percentile(vals, 0.50)
    p75 = _percentile(vals, 0.75)
    p90 = _percentile(vals, 0.90)
    p95 = _percentile(vals, 0.95)
    return {
        "high": max(p95, ABS_FLOOR["high"]),
        "mid": max(p90, ABS_FLOOR["mid"]),
        "weak": max(p75, ABS_FLOOR["weak"]),
        "source": "data",
        "pairs": n,
        "p50": p50, "p75": p75, "p90": p90, "p95": p95,
    }


def _level_of_dynamic(score, thr):
    """动态档位: 高度相似 / 较相似 / 有交集 / 弱关联。"""
    if score >= thr["high"]:
        return "高度相似"
    if score >= thr["mid"]:
        return "较相似"
    if score >= thr["weak"]:
        return "有交集"
    return "弱关联"


def _union_find(uids):
    parent = {u: u for u in uids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    return find, union


def _group_name(members, vectors, df, n_users):
    """用成员共现最多的非泛标签给群体命名。"""
    weight = {}
    for uid in members:
        for tag, w in vectors[uid].items():
            weight[tag] = weight.get(tag, 0.0) + w

    generic_limit = max(1, n_users * GENERIC_DF_RATIO)
    ranked = sorted(
        ((tag, w) for tag, w in weight.items() if df.get(tag, 0) < generic_limit),
        # 先按覆盖成员数, 再按权重: 避免组内单个用户的强偏好标签压过全员共有的标签
        key=lambda x: (-sum(1 for uid in members if x[0] in vectors[uid]), -x[1], x[0]),
    )
    tags = [tag for tag, _ in ranked[:TOP_TAGS]]
    detail = []
    for tag, w in ranked[:TOP_TAGS]:
        detail.append({
            "tag": tag,
            "members": sum(1 for uid in members if tag in vectors[uid]),
            "weight": round(w, 4),
        })
    return tags, detail


# 兼容旧调用: 默认连边阈值(app.py 在请求未显式指定时也不再传它)
DEFAULT_THRESHOLD = FALLBACK_THRESHOLDS["mid"]


def _build_group(members, scores, vectors, df, n_users, user_stats, thr):
    """组装一个群体的输出结构。thr 为动态阈值 dict {high, mid, weak, ...}。"""
    pairs = []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            key = (a, b) if a < b else (b, a)
            s = scores.get(key)
            if s is None:
                continue
            pairs.append({"a": a, "b": b, "score": round(s, 4),
                          "level": _level_of_dynamic(s, thr)})
    pairs.sort(key=lambda x: -x["score"])

    values = [p["score"] for p in pairs]
    cohesion = sum(values) / len(values) if values else 0.0
    min_pair = min(values) if values else 0.0
    max_pair = max(values) if values else 0.0

    tags, tag_detail = _group_name(members, vectors, df, n_users)
    kind = "group" if len(members) >= GROUP_MIN_SIZE else "pair"

    notes = []
    if min_pair < thr["weak"]:
        notes.append("该组由相似关系链合并, 组内最弱的一对仅 %.1f%%" % (min_pair * 100))

    return {
        "kind": kind,
        "label": " / ".join(tags) if tags else "未命名群体",
        "size": len(members),
        "members": members,
        "member_details": [
            {
                "uid": uid,
                "comment_count": user_stats.get(uid, {}).get("comment_count", 0),
                "danmu_count": user_stats.get(uid, {}).get("danmu_count", 0),
                "last_ctime": _fmt_time(user_stats.get(uid, {}).get("last_ctime")),
            }
            for uid in members
        ],
        "cohesion": round(cohesion, 4),
        "min_pair": round(min_pair, 4),
        "max_pair": round(max_pair, 4),
        "top_tags": tag_detail,
        "pairs": pairs,
        "note": "；".join(notes),
    }


def _fmt_time(value):
    """datetime -> "YYYY-MM-DD HH:MM"（避免 jsonify 输出 RFC1123 长串）。"""
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except AttributeError:
        return str(value)


def _user_stats():
    """uid -> {comment_count, danmu_count, last_ctime}"""
    try:
        return {u["uid"]: u for u in db.list_ingested_users()}
    except Exception as e:
        print(f"[clustering] 读取入库用户列表失败: {e}")
        return {}


def discover(threshold=None, uid=None):
    """发现兴趣群体。

    参数:
      threshold: 显式指定的连边阈值; 不传时按库内分布动态划档(推荐)。
      uid:       指定用户时, 只返回包含该用户的群体, 并附带其 Top 邻居

    返回 dict:
      {
        "groups": [...],       # 群体列表(pairs 的 level 用动态档位)
        "threshold": float,    # 实际使用的连边阈值(动态时为 mid 档)
        "thresholds": {...},   # 动态阈值与分布摘要 {high, mid, weak, source, ...}
        "user_count": int,     # 参与聚类的用户数
        "total_users": int,    # 库中用户总数
        "neighbors": [...],    # 指定 uid 时的 Top 相似邻居
        "note": str,
      }
    """
    state = similarity.load_state()
    vectors = state["vectors"]
    uids = _eligible_uids(state)
    stats = _user_stats()

    scores = _pair_scores(vectors, uids)
    thr = _thresholds_from_distribution(scores)
    if threshold is not None:
        # 显式指定阈值时仍用固定分档措辞, 以该值为 mid
        thr = dict(thr)
        thr["mid"] = threshold
        thr["source"] = "fixed"

    find, union = _union_find(uids)
    for (a, b), s in scores.items():
        if s >= thr["mid"]:
            union(a, b)

    buckets = {}
    for member_uid in uids:
        buckets.setdefault(find(member_uid), []).append(member_uid)

    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        groups.append(_build_group(sorted(members), scores, vectors, state["df"],
                                   len(uids), stats, thr))
    groups.sort(key=lambda g: (-g["size"], -g["cohesion"]))

    neighbors = []
    if uid is not None:
        uid = int(uid)
        groups = [g for g in groups if uid in g["members"]]
        for (a, b), s in scores.items():
            other = None
            if a == uid:
                other = b
            elif b == uid:
                other = a
            if other is None:
                continue
            neighbors.append({"uid": other, "score": round(s, 4),
                               "level": _level_of_dynamic(s, thr)})
        neighbors.sort(key=lambda x: -x["score"])
        neighbors = neighbors[:5]

    notes = []
    if len(uids) < similarity.MIN_IDF_USERS:
        notes.append("入库用户不足 %d 个, IDF 加权与聚类结果仅供参考"
                     % similarity.MIN_IDF_USERS)
    if thr["source"] == "fixed":
        notes.append("库内用户对样本不足, 使用默认阈值分档")
    if len(groups) == 0:
        notes.append("当前阈值 %.0f%% 下没有发现相似群体" % (thr["mid"] * 100))

    return {
        "groups": groups,
        "threshold": thr["mid"],
        "thresholds": {
            "high": round(thr["high"], 4),
            "mid": round(thr["mid"], 4),
            "weak": round(thr["weak"], 4),
            "source": thr["source"],
            "pairs": thr["pairs"],
        },
        "user_count": len(uids),
        "total_users": len(stats),
        "neighbors": neighbors,
        "note": "；".join(notes),
    }


if __name__ == "__main__":
    import sys

    thr = DEFAULT_THRESHOLD
    if len(sys.argv) > 1:
        try:
            thr = float(sys.argv[1])
        except ValueError:
            pass

    result = discover(threshold=thr)
    print(f"\n=== 兴趣群体发现 (阈值 {thr * 100:.0f}%, "
          f"参与聚类 {result['user_count']}/{result['total_users']} 人) ===")
    if not result["groups"]:
        print("未发现群体")
    for g in result["groups"]:
        kind = "群体" if g["kind"] == "group" else "兴趣对"
        print(f"\n[{kind}] {g['label']}  {g['size']} 人  内聚度 {g['cohesion'] * 100:.1f}%")
        print("  成员:", ", ".join(str(m) for m in g["members"]))
        for p in g["pairs"]:
            print(f"    {p['a']} <-> {p['b']}: {p['score'] * 100:.1f}% ({p['level']})")
        if g["note"]:
            print("  提示:", g["note"])
    if result["note"]:
        print("\n提示:", result["note"])
