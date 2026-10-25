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
  若组内存在相似度低于阈值的一对(链式合并), 标记为松散群体(loose)。

数据来源统一走 similarity.load_state()(画像/向量/IDF 都来自 db.user_profile)。
"""

import db
import similarity

# 默认相似度阈值(与 similarity.LEVELS 的「较为相似」档对齐)
DEFAULT_THRESHOLD = 0.12

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
        if (prof.get("total") or 0) < MIN_SAMPLE:
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

    generic_limit = max(1, int(n_users * GENERIC_DF_RATIO))
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


def _build_group(members, scores, vectors, df, n_users, user_stats):
    """组装一个群体的输出结构。"""
    pairs = []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            key = (a, b) if a < b else (b, a)
            s = scores.get(key)
            if s is None:
                continue
            pairs.append({"a": a, "b": b, "score": round(s, 4),
                          "level": similarity._level_of(s)})
    pairs.sort(key=lambda x: -x["score"])

    values = [p["score"] for p in pairs]
    cohesion = sum(values) / len(values) if values else 0.0
    min_pair = min(values) if values else 0.0
    max_pair = max(values) if values else 0.0

    tags, tag_detail = _group_name(members, vectors, df, n_users)
    kind = "group" if len(members) >= GROUP_MIN_SIZE else "pair"

    notes = []
    if min_pair < DEFAULT_THRESHOLD:
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


def discover(threshold=DEFAULT_THRESHOLD, uid=None):
    """发现兴趣群体。

    参数:
      threshold: 相似度阈值, 达到该值的两个用户归为同组
      uid:       指定用户时, 只返回包含该用户的群体, 并附带其 Top 邻居

    返回 dict:
      {
        "groups": [...],       # 群体列表
        "threshold": float,
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
    find, union = _union_find(uids)
    for (a, b), s in scores.items():
        if s >= threshold:
            union(a, b)

    buckets = {}
    for member_uid in uids:
        buckets.setdefault(find(member_uid), []).append(member_uid)

    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        groups.append(_build_group(sorted(members), scores, vectors, state["df"],
                                   len(uids), stats))
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
                               "level": similarity._level_of(s)})
        neighbors.sort(key=lambda x: -x["score"])
        neighbors = neighbors[:5]

    notes = []
    if len(uids) < similarity.MIN_IDF_USERS:
        notes.append("入库用户不足 %d 个, IDF 加权与聚类结果仅供参考"
                     % similarity.MIN_IDF_USERS)
    if len(groups) == 0:
        notes.append("当前阈值 %.0f%% 下没有发现相似群体" % (threshold * 100))

    return {
        "groups": groups,
        "threshold": threshold,
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
