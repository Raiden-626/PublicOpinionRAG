"""生成: RAG 问答 + 最近关注分析 + 舆情分析报告。

所有生成函数均为生成器,yield str token,供 SSE 流式推送。
- ask_stream():       通用 RAG 问答(向量召回 + 回查原文)
- recent_focus_stream(): 功能2 — 该用户最近在关注的内容
- report_stream():    功能3 — 舆情分析报告
"""
import db
from retrieve import retrieve, build_context
from clients import chat_stream


SYSTEM_QA = (
    "你是B站用户舆情分析助手。仅基于给定评论/弹幕片段回答,不要编造。"
    "引用片段时用 [序号]。若片段不足以回答,直接说明。"
)
SYSTEM_ANALYST = "你是专业的B站用户行为与舆情分析师。严格基于给定数据客观分析,不编造未提供的内容。"


def _fmt_rows(rows, label):
    """把 db 行列表格式化为 [标签 日期] 正文 的行。"""
    out = []
    for r in rows:
        d = r["ctime"].strftime("%Y-%m-%d") if r.get("ctime") else "未知时间"
        out.append(f"[{label} {d}] {r['content']}")
    return out


def _dedup_rows(lines, threshold=0.55):
    """去除近似重复的行(基于字符 bigram Jaccard 相似度)。
    保留首次出现的版本,维持原始顺序。
    """
    def bigrams(s):
        s = s.strip()
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) > 1 else {s}

    kept = []
    kept_bg = []
    for line in lines:
        bg = bigrams(line)
        is_dup = False
        for kbg in kept_bg:
            inter = len(bg & kbg)
            union = len(bg | kbg)
            if union and inter / union > threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(line)
            kept_bg.append(bg)
    return kept


def _truncate_recent(lines, max_chars):
    """从最新内容开始填充,直到达到字符上限。
    lines 已按时间倒序排列;优先保留最新的条目。
    """
    parts, total = [], 0
    for line in lines:
        block_len = len(line) + 1  # +1 for \n
        if total + block_len > max_chars:
            break
        parts.append(line)
        total += block_len
    return "\n".join(parts)


# ---- 流式生成函数 ----

def ask_stream(question, uid, top_k=None):
    """对该 uid 的语料做 RAG 问答。生成器 yield str token。
    调用方可在生成结束后读取 .sources 属性获取来源。
    """
    results = retrieve(question, uid, top_k=top_k or 8)
    ctx = build_context(results)
    if not ctx:
        yield "该用户暂无可检索数据,请先抓取入库。"
        return

    user = (
        f"用户问题:{question}\n\n"
        f"参考片段(按相关度排序):\n{ctx}\n\n"
        "请基于以上片段回答。"
    )
    for token in chat_stream([
        {"role": "system", "content": SYSTEM_QA},
        {"role": "user", "content": user},
    ]):
        yield token

    # 将 sources 附在函数对象上(调用方在生成结束后读取)
    ask_stream.sources = [
        {"content": r["content"], "ctime": r["ctime"], "url": r["url"]}
        for r in results
    ]


def recent_focus_stream(uid, sample_n=200):
    """功能2: 最近在关注的内容。生成器 yield str token。

    取该 uid 最近 sample_n 条评论 + 弹幕(按时间倒序),
    去重后截断至 6000 字符,流式调用 DeepSeek 归纳。
    """
    comments = db.fetch_sample("comment", uid, sample_n)
    danmu = db.fetch_sample("danmu", uid, sample_n)
    if not comments and not danmu:
        yield "该用户暂无数据,请先抓取入库(uid 输入框 → 功能1)。"
        return

    lines = _fmt_rows(comments, "评论") + _fmt_rows(danmu, "弹幕")
    lines = _dedup_rows(lines)
    corpus = _truncate_recent(lines, 4000)

    user = (
        f"以下是B站用户 uid={uid} 最近的 {len(comments)} 条评论与 {len(danmu)} 条弹幕"
        f"(按时间倒序,已去重并截断至最近内容):\n\n{corpus}\n\n"
        "请归纳该用户【最近在关注的内容】,输出:\n"
        "1) 关注的主要话题/领域(3-6 个,每个带关键词 + 一句简要说明)\n"
        "2) 近期兴趣重心(最突出的 1-2 个方向,说明依据)\n"
        "3) 关注方式特征(偏看弹幕还是爱评论?参与是吐槽/认同/玩梗?若可判断)\n"
        "用中文,结构化,基于给定数据,不编造。"
    )
    recent_focus_stream.used = len(comments) + len(danmu)

    # 累积文本并在生成完成后保存历史记录
    full_text = []
    for token in chat_stream([
        {"role": "system", "content": SYSTEM_ANALYST},
        {"role": "user", "content": user},
    ], temperature=0.4, max_tokens=1200):
        full_text.append(token)
        yield token

    # 生成完成,保存到历史表
    try:
        content = "".join(full_text)
        db.save_history(uid, "focus", content, title="最近在关注的内容")
    except Exception:
        pass  # 保存失败不影响主流程


def report_stream(uid, sample_n=400):
    """功能3: 舆情分析报告。生成器 yield str token。

    取该 uid 最近 sample_n 条评论 + 弹幕,去重后截断至 6500 字符,
    DeepSeek 出领域画像与行为预测。
    """
    comments = db.fetch_sample("comment", uid, sample_n)
    danmu = db.fetch_sample("danmu", uid, sample_n)
    if not comments and not danmu:
        yield "该用户暂无数据,请先抓取入库(uid 输入框 → 功能1)。"
        return

    lines = _fmt_rows(comments, "评论") + _fmt_rows(danmu, "弹幕")
    lines = _dedup_rows(lines)
    corpus = _truncate_recent(lines, 4500)

    user = (
        f"以下是B站用户 uid={uid} 的 {len(comments)} 条评论与 {len(danmu)} 条弹幕"
        f"(按时间倒序,已去重):\n\n{corpus}\n\n"
        "请输出该用户的【舆情分析报告】,必须包含以下四部分:\n"
        "## 一、涉猎的方面\n"
        "该用户参与/关注了哪些领域(如 ACG、科技数码、游戏、影视、时政、生活情感、知识科普等),"
        "每个领域给出:关键词、参与程度(高/中/低)、情感倾向。\n"
        "## 二、立场与情感画像\n"
        "整体情感倾向(正/负/中立及大致比例)、典型立场、表达风格(吐槽/玩梗/严肃/情绪化)。\n"
        "## 三、将来会做出什么(行为预测)\n"
        "基于其内容模式,预测未来可能:继续关注/转向什么话题、可能发表什么类型的言论、"
        "行为倾向(如更活跃/趋于沉默/可能引发争议等)。给出 2-4 条预测,每条标注推断依据。\n"
        "## 四、典型依据摘录\n"
        "摘录 3-5 条有代表性的原始内容(带日期),支撑上述判断。\n\n"
        "用中文,Markdown 结构化输出,严格基于给定数据,不编造。"
    )
    report_stream.used = len(comments) + len(danmu)

    # 累积文本并在生成完成后保存历史记录
    full_text = []
    for token in chat_stream([
        {"role": "system", "content": SYSTEM_ANALYST},
        {"role": "user", "content": user},
    ], temperature=0.4, max_tokens=1400):
        full_text.append(token)
        yield token

    # 生成完成,保存到历史表
    try:
        content = "".join(full_text)
        db.save_history(uid, "report", content, title="舆情分析报告")
    except Exception:
        pass  # 保存失败不影响主流程


# ---- RAG 问答 ----

def ask_stream(question, uid, top_k=None):
    """对该 uid 的语料做 RAG 问答。生成器 yield str token。
    调用方可在生成结束后读取 .sources 属性获取来源。
    """
    results = retrieve(question, uid, top_k=top_k or 8)
    ctx = build_context(results)
    if not ctx:
        yield "该用户暂无可检索数据,请先抓取入库。"
        return

    user = (
        f"用户问题:{question}\n\n"
        f"参考片段(按相关度排序):\n{ctx}\n\n"
        "请基于以上片段回答。"
    )

    # 累积文本并在生成完成后保存历史记录
    full_text = []
    for token in chat_stream([
        {"role": "system", "content": SYSTEM_QA},
        {"role": "user", "content": user},
    ]):
        full_text.append(token)
        yield token

    # 将 sources 附在函数对象上(调用方在生成结束后读取)
    ask_stream.sources = [
        {"content": r["content"], "ctime": r["ctime"], "url": r["url"]}
        for r in results
    ]

    # 生成完成,保存到历史表
    try:
        content = "".join(full_text)
        db.save_history(uid, "ask", content, title=question[:50] if question else None)
    except Exception:
        pass  # 保存失败不影响主流程
