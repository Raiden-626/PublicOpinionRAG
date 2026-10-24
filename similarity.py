"""用户兴趣相似度分析（第二层：群体发现与对比）。

核心口径:
  把每个用户表示为一个标签向量, 每个标签的权重 = 该标签下的评论数 / 该用户总记录数,
  然后用余弦相似度衡量两个用户的关注领域重合程度。

分区的重叠情况(tname)作为辅助信息单独返回, 不参与打分。

本模块只做计算, 数据来源统一走 db.user_profile()。
"""

import math

import db

# 返回结果中各列表最多保留的条目数
SHARED_TOP = 20
DISTINCT_TOP = 15
TNAME_TOP = 10

# 相似度等级划分阈值
LEVELS = [
    (0.60, "高度相似"),
    (0.35, "较为相似"),
    (0.15, "关注接近"),
    (0.00, "差异明显"),
]

# 有效记录数低于此值时提示样本过小
MIN_SAMPLE = 5


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


def _profile(uid):
    """取用户画像, 失败时返回空画像而不是抛错。"""
    try:
        profile = db.user_profile(uid)
    except Exception as e:
        print(f"[similarity] 获取 uid={uid} 画像失败: {e}")
        return {"tname_dist": [], "tag_dist": [], "total": 0}
    return profile or {"tname_dist": [], "tag_dist": [], "total": 0}


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
    prof_a = _profile(uid_a)
    prof_b = _profile(uid_b)

    total_a = prof_a.get("total", 0)
    total_b = prof_b.get("total", 0)

    tag_a = build_vector(prof_a.get("tag_dist", []), total_a, "tag")
    tag_b = build_vector(prof_b.get("tag_dist", []), total_b, "tag")
    part_a = build_vector(prof_a.get("tname_dist", []), total_a, "tname")
    part_b = build_vector(prof_b.get("tname_dist", []), total_b, "tname")

    score = cosine(tag_a, tag_b)
    level = _level_of(score)

    notes = []
    if total_a == 0:
        notes.append(f"UID {uid_a} 暂无可用的分区/标签数据(需先抓取入库并补全视频信息)")
    if total_b == 0:
        notes.append(f"UID {uid_b} 暂无可用的分区/标签数据(需先抓取入库并补全视频信息)")
    if score is not None:
        if total_a < MIN_SAMPLE:
            notes.append(f"UID {uid_a} 有效样本仅 {total_a} 条, 结果仅供参考")
        if total_b < MIN_SAMPLE:
            notes.append(f"UID {uid_b} 有效样本仅 {total_b} 条, 结果仅供参考")

    # 共同关注点: 按两边权重之和降序
    shared = []
    for tag in set(tag_a) & set(tag_b):
        shared.append({
            "tag": tag,
            "count_a": int(round(tag_a[tag] * total_a)),
            "count_b": int(round(tag_b[tag] * total_b)),
            "pct_a": round(tag_a[tag] * 100, 1),
            "pct_b": round(tag_b[tag] * 100, 1),
            "weight_sum": tag_a[tag] + tag_b[tag],
        })
    shared.sort(key=lambda x: (-x["weight_sum"], x["tag"]))
    for item in shared:
        item.pop("weight_sum", None)
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

    distinct_a = _distinct(tag_a, tag_b, total_a)
    distinct_b = _distinct(tag_b, tag_a, total_b)

    part_shared = [p for p in part_a if p in part_b]
    part_only_a = [p for p in part_a if p not in part_b]
    part_only_b = [p for p in part_b if p not in part_a]

    return {
        "score": round(score, 4) if score is not None else None,
        "level": level,
        "sample_a": total_a,
        "sample_b": total_b,
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
    print(f"\n=== UID {ua} -> UID {ub} 兴趣相似度 ===")
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
        print("\n仅 A 关注:")
        for item in r["distinct_a"]:
            print(f"  {item['tag']}: {item['count']}条 ({item['pct']}%)")
    if r["distinct_b"]:
        print("\n仅 B 关注:")
        for item in r["distinct_b"]:
            print(f"  {item['tag']}: {item['count']}条 ({item['pct']}%)")
    if r["tname_shared"]:
        print(f"\n共同活跃分区: {', '.join(r['tname_shared'])}")
    if r["note"]:
        print(f"\n提示: {r['note']}")
