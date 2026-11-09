"""B站用户舆情分析 — 可视化页面后端(Flask)。

功能:
  1. POST /api/ingest  {uid}        抓取评论+弹幕,入 MySQL+Chroma
  2. GET  /api/status  ?uid=        查该 uid 现有评论/弹幕条数
  3. POST /api/focus   {uid}        最近在关注的内容(后台任务+轮询)
  4. POST /api/report  {uid}        舆情分析报告(后台任务+轮询)
  5. POST /api/ask     {uid,question} RAG 问答(后台任务+轮询)
  6. GET  /api/task    ?task_id=    轮询任务状态/累积文本
  7. POST /api/re_embed {uid}       补全缺失向量(MySQL→Chroma)
  8. POST /api/backfill {uids}      补全视频信息(tid/tname/tags)
  9. GET  /api/records/users        列出已入库用户(用户记录视图)
 10. GET  /api/records ?uid=        某用户的评论/弹幕记录(各前 N 条)

启动:
  .venv\\Scripts\\python.exe app.py
  浏览器打开 http://127.0.0.1:5000/
"""

import os
import json
import hashlib
import threading
import time
from datetime import datetime
from pathlib import Path

# 加载 .env 文件(显式指定路径,避免工作目录问题)
from dotenv import load_dotenv
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"[config] 已加载 .env: {env_path}")
else:
    print(f"[config] 警告: .env 文件不存在: {env_path}")

# Playwright 浏览器在项目内,导入 ingest 前设好
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pw_browsers"),
)

from flask import Flask, render_template, request, jsonify

import db
import generate
import ingest
from zones import zone_name

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


def _counts(uid):
    """查该 uid 在两表中的条数。"""
    return db.count_rows(uid)


def _json(obj, status=None):
    """jsonify + 显式 charset=utf-8,防止中文 Windows 浏览器回退 GBK 导致乱码。"""
    resp = jsonify(obj)
    resp.mimetype = "application/json; charset=utf-8"
    if status:
        resp.status_code = status
    return resp


# ---- 后台任务(刷新不丢失) ----
_tasks = {}
_tasks_lock = threading.Lock()

# 已结束任务在内存中保留的时长(秒),超时清理防止 _tasks 无限增长
_TASK_TTL = 3600
# 任务字典容量上限,超过时优先清理最早结束的已结束任务
_TASK_MAX = 100


def _prune_tasks():
    """清理过期任务。必须在持有 _tasks_lock 时调用。"""
    now = time.time()
    # 过期清理
    stale = [
        k
        for k, v in _tasks.items()
        if v["status"] in ("done", "failed")
        and now - v.get("finished", v.get("updated", 0)) > _TASK_TTL
    ]
    for k in stale:
        del _tasks[k]
    # 容量兜底: 超限时删掉最早结束的已结束任务(运行中的不动)
    if len(_tasks) > _TASK_MAX:
        finished = sorted(
            (
                (v.get("finished", v.get("updated", 0)), k)
                for k, v in _tasks.items()
                if v["status"] in ("done", "failed")
            ),
        )
        for _, k in finished[: len(_tasks) - _TASK_MAX]:
            del _tasks[k]


def _run_task(task_key, gen_func, *args, **kwargs):
    """后台线程: 运行生成器,累积文本到 _tasks[task_key]。
    生成器的运行产物(如 ask 的 sources、report 的 used)写入 result dict,
    结束后并入任务 entry 的 extra 字段,供 /api/task 返回。
    """
    with _tasks_lock:
        entry = _tasks.get(task_key)
    if entry is None:
        return
    result = {}
    try:
        for token in gen_func(*args, result=result, **kwargs):
            with _tasks_lock:
                cur = _tasks.get(task_key)
                # 任务已被新一次生成替换(force), 放弃本次输出
                if cur is not entry:
                    return
                if cur.get("cancelled"):
                    return
                cur["text"] += token
                cur["updated"] = time.time()
        with _tasks_lock:
            cur = _tasks.get(task_key)
            if cur is not entry:
                return
            if result:
                cur["extra"] = result
            cur["status"] = "done"
            cur["finished"] = time.time()
            cur["updated"] = time.time()
    except Exception as e:
        with _tasks_lock:
            cur = _tasks.get(task_key)
            if cur is entry:
                cur["status"] = "failed"
                cur["error"] = str(e)
                cur["finished"] = time.time()
                cur["updated"] = time.time()


@app.route("/api/task")
def api_task():
    """轮询任务状态。返回 {status, text, offset, counts, error?, extra?}。
    支持 offset 增量拉取: 只返回 text[offset:], offset 为当前全量文本长度。
    不传 offset 时返回全量 text(向后兼容)。
    """
    task_key = request.args.get("task_id", "").strip()
    if not task_key:
        return _json({"error": "缺少 task_id"}, 400)
    with _tasks_lock:
        entry = _tasks.get(task_key)
    if not entry:
        return _json({"error": "任务不存在"}, 404)
    full_text = entry["text"]
    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        offset = 0
    offset = max(0, min(offset, len(full_text)))
    resp = {
        "status": entry["status"],
        "text": full_text[offset:],
        "offset": len(full_text),
        "counts": entry.get("counts") or {},
    }
    if entry.get("error"):
        resp["error"] = entry["error"]
    if entry.get("extra"):
        resp.update(entry["extra"])
    return _json(resp)


def _parse_uid(body):
    """从 request body 解析并校验 uid,返回 (int_uid, error_response)。
    若校验通过 error_response 为 None。
    """
    uid = (body or {}).get("uid", "")
    uid = str(uid).strip()
    if not uid.isdigit():
        return None, _json({"error": "uid 必须为数字"}, 400)
    return int(uid), None


# ---- 非流式端点 ----


@app.route("/api/status")
def api_status():
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    counts = _counts(uid)

    # Chroma 向量数量
    import vector_store

    chroma_counts = {
        "comment": vector_store.count_by_uid(uid, kind="comment"),
        "danmu": vector_store.count_by_uid(uid, kind="danmu"),
    }

    return _json({"uid": uid, "counts": counts, "chroma_vectors": chroma_counts})


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    data = request.json or {}
    uid, err = _parse_uid(data)
    if err:
        return err
    overwrite = data.get("overwrite", False)
    try:
        res = ingest.ingest_uid(uid, headless=False, overwrite=overwrite)
    except Exception as e:
        return _json({"error": f"抓取失败: {e}"}, 500)
    res["uid"] = uid
    res["counts"] = _counts(uid)
    return _json(res)


# ---- 后台任务端点(刷新不丢失) ----


def _start_or_reconnect(task_key, gen_func, args, counts, force=False):
    """查找已有任务或创建新任务。返回 (task_key, entry)。

    force=True: 用户主动再次点击按钮, 即使上次已完成也重新生成一次。
    force=False(默认): 页面刷新恢复时复用已有任务, 不重复消耗 LLM 调用。
    运行中的任务无论如何都只重连, 不并发启动第二个。
    """
    with _tasks_lock:
        _prune_tasks()
        existing = _tasks.get(task_key)
        if existing and existing["status"] == "running":
            return task_key, existing
        if existing and existing["status"] == "done" and not force:
            return task_key, existing
        entry = {
            "status": "running",
            "text": "",
            "error": None,
            "extra": None,
            "counts": counts,
            "created": time.time(),
            "updated": time.time(),
        }
        _tasks[task_key] = entry
    t = threading.Thread(target=_run_task, args=(task_key, gen_func, *args), daemon=True)
    t.start()
    return task_key, entry


@app.route("/api/focus", methods=["POST"])
def api_focus():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    force = bool((request.json or {}).get("force"))
    counts = _counts(uid)
    task_key = f"focus_{uid}"
    _, entry = _start_or_reconnect(
        task_key, generate.recent_focus_stream, (uid,), counts, force=force
    )
    return _json(
        {
            "task_id": task_key,
            "counts": counts,
            "status": entry["status"],
            "text": entry["text"],
        }
    )


@app.route("/api/report", methods=["POST"])
def api_report():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    force = bool((request.json or {}).get("force"))
    counts = _counts(uid)
    task_key = f"report_{uid}"
    _, entry = _start_or_reconnect(
        task_key, generate.report_stream, (uid,), counts, force=force
    )
    return _json(
        {
            "task_id": task_key,
            "counts": counts,
            "status": entry["status"],
            "text": entry["text"],
        }
    )


@app.route("/api/ask", methods=["POST"])
def api_ask():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return _json({"error": "请输入问题"}, 400)
    counts = _counts(uid)

    # 检查 Chroma 中是否有该 UID 的向量,若无则自动补全
    import vector_store
    from clients import embed as embed_fn

    chroma_count = vector_store.count_by_uid(uid)
    auto_fill_error = None
    if chroma_count == 0:
        # 尝试自动补全向量
        try:
            auto_fill_result = []
            for kind in ("comment", "danmu"):
                table = db.table_for(kind, uid)
                existing_ids = vector_store.get_mysql_ids(
                    where={"$and": [{"uid": uid}, {"mysql_table": table}]}
                )
                missing_ids = db.fetch_ids_without_vector(kind, uid, existing_ids)
                if not missing_ids:
                    continue
                rows = db.fetch_rows_by_ids(kind, uid, missing_ids[:200])
                texts = [r["content"] for r in rows]
                # 向量化带重试(最多 3 次)
                vecs = None
                for attempt in range(3):
                    try:
                        vecs = embed_fn(texts)
                        break
                    except Exception as e:
                        if attempt < 2:
                            print(f"[auto-fill] {kind} 向量化失败,重试中: {e}")
                            time.sleep(3)
                        else:
                            raise
                if vecs is None:
                    continue
                ids, metas, docs = [], [], []
                for row, vec in zip(rows, vecs):
                    ids.append(f"{kind}_{uid}_{row['id']}")
                    metas.append(
                        {
                            "uid": uid,
                            "type": table,
                            "mysql_table": table,
                            "mysql_id": row["id"],
                            "oid": row.get("oid") or 0,
                            "ctime_ts": row["ctime"].timestamp() if row.get("ctime") else 0,
                        }
                    )
                    # 嵌入文本包含日期,帮助时间相关查询匹配
                    ctime_str = row["ctime"].strftime("%Y-%m-%d") if row.get("ctime") else "未知时间"
                    docs.append(f"[{ctime_str}] {row['content']}")
                vector_store.add(
                    ids=ids, embeddings=vecs, documents=docs, metadatas=metas
                )
                auto_fill_result.append(f"{kind}: {len(rows)}条")
            if auto_fill_result:
                print(f"[auto-fill] uid={uid} 补全完成: {'; '.join(auto_fill_result)}")
            # 重新检查
            chroma_count = vector_store.count_by_uid(uid)
        except Exception as e:
            auto_fill_error = str(e)
            print(f"[auto-fill] uid={uid} 失败: {e}")

    if chroma_count == 0:
        err_msg = f"Chroma 向量库中没有 UID={uid} 的数据。MySQL 中有评论 {counts.get('comment', 0)} 条、弹幕 {counts.get('danmu', 0)} 条。"
        if auto_fill_error:
            err_msg += f" 自动补全失败: {auto_fill_error}"
        else:
            err_msg += " 自动补全未能写入向量,请检查 embedding API 配置。"
        return _json(
            {
                "error": err_msg,
                "mysql_counts": counts,
                "chroma_vectors": chroma_count,
            },
            400,
        )

    # task_key 用问题的哈希而非原文: 避免中文/特殊字符问题串进 key,
    # 也能保证同一问题刷新后重连到同一任务
    q_hash = hashlib.md5(question.encode("utf-8")).hexdigest()[:12]
    task_key = f"ask_{uid}_{q_hash}"
    force = bool((request.json or {}).get("force"))
    _, entry = _start_or_reconnect(
        task_key,
        generate.ask_stream,
        (question, uid),
        counts,
        force=force,
    )
    resp = {
        "task_id": task_key,
        "counts": counts,
        "status": entry["status"],
        "text": entry["text"],
    }
    if entry.get("extra"):
        resp.update(entry["extra"])
    return _json(resp)


# ---- 向量补全 ----


@app.route("/api/re_embed", methods=["POST"])
def api_re_embed():
    """扫描 MySQL 中有但 Chroma 中没有向量的行,补全向量化。"""
    uid, err = _parse_uid(request.json)
    if err:
        return err

    try:
        import vector_store
        from clients import embed

        results = {}
        debug_info = []

        for kind in ("comment", "danmu"):
            table = db.table_for(kind, uid)
            # 用 Chroma get() 按 metadata 查已有的 mysql_id,无需浪费 embedding 调用
            existing_ids = vector_store.get_mysql_ids(
                where={"$and": [{"uid": uid}, {"mysql_table": table}]}
            )
            debug_info.append(f"{kind}: Chroma已有{len(existing_ids)}条")

            # 找 MySQL 中缺失的行
            missing_ids = db.fetch_ids_without_vector(kind, uid, existing_ids)
            debug_info.append(f"{kind}: MySQL中需补{len(missing_ids)}条")

            if not missing_ids:
                results[kind] = {"missing": 0, "embedded": 0}
                continue

            rows = db.fetch_rows_by_ids(
                kind, uid, missing_ids[:200]
            )  # 每次最多补 200 条
            texts = [r["content"] for r in rows]
            try:
                vecs = embed(texts)
                debug_info.append(f"{kind}: 向量化成功{len(vecs)}条")
            except Exception as e:
                results[kind] = {
                    "missing": len(missing_ids),
                    "embedded": 0,
                    "error": str(e),
                }
                debug_info.append(f"{kind}: 向量化失败-{e}")
                continue

            ids, metas, docs = [], [], []
            for row, vec in zip(rows, vecs):
                # ID 必须与入库时一致: kind_uid_mysqlid
                ids.append(f"{kind}_{uid}_{row['id']}")
                metas.append(
                    {
                        "uid": uid,
                        "type": table,
                        "mysql_table": table,
                        "mysql_id": row["id"],
                        "oid": row.get("oid") or 0,
                        "ctime_ts": row["ctime"].timestamp() if row.get("ctime") else 0,
                    }
                )
                # 嵌入文本包含日期, 与入库时保持一致
                ctime_str = row["ctime"].strftime("%Y-%m-%d") if row.get("ctime") else "未知时间"
                docs.append(f"[{ctime_str}] {row['content']}")
            written = vector_store.add(
                ids=ids, embeddings=vecs, documents=docs, metadatas=metas
            )
            results[kind] = {"missing": len(missing_ids), "embedded": written}
            debug_info.append(f"{kind}: 已写入Chroma{written}条(请求{len(rows)}条)")

        results["uid"] = uid
        results["counts"] = _counts(uid)
        results["debug"] = "; ".join(debug_info)
        return _json(results)
    except Exception as e:
        return _json({"error": f"补全向量失败: {str(e)}"}, 500)


# ---- 视频信息补全 ----


@app.route("/api/backfill", methods=["POST"])
def api_backfill():
    """补全缺失的视频信息(tid/tname/tags)。
    请求体: {"uids": "123,456"} 或 {"uids": ""} 表示全部
    """
    data = request.json or {}
    uids_str = str(data.get("uids", "")).strip()

    if uids_str:
        # 解析指定的 uid 列表
        uid_list = []
        for u in uids_str.split(","):
            u = u.strip()
            if u.isdigit():
                uid_list.append(int(u))
            elif u:
                return _json({"error": f"无效的 uid: {u}"}, 400)
        uids = uid_list if uid_list else None
    else:
        uids = None  # 补全所有用户

    try:
        stats = ingest.backfill_video_info(uids=uids)
        return _json({
            "success": True,
            "scanned": stats["scanned"],
            "updated": stats["updated"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
        })
    except Exception as e:
        return _json({"error": f"补全失败: {str(e)}"}, 500)


# ---- 批量抓取(扩库): 候选发现 + 池调度 + 后台运行 ----
# 同时只允许一个批量任务(发现/扩库共用同一个工作线程槽位),
# 因为都依赖 aicu/B站 限流, 并发只会互相放大等待。

_expand_lock = threading.Lock()
_expand_job = {
    "running": False,
    "kind": None,       # owners/reply/manual/expand
    "log": [],          # 逐行日志(环形保留 _EXPAND_LOG_MAX 行)
    "result": None,     # 结束产物 dict
    "error": None,
    "cancel": False,    # 停止信号: expand 在下个用户开始前/reply 在下个视频前生效
    "started": None,
    "finished": None,
}
_EXPAND_LOG_MAX = 3000


def _expand_cancelled():
    """should_stop 回调: 读取取消标志。"""
    with _expand_lock:
        return _expand_job.get("cancel", False)


class _JobLog:
    """redirect_stdout 接收端: 把 print 输出按行累积进 _expand_job["log"]。"""

    _BUF = []

    def write(self, s):
        if not s:
            return
        cls = type(self)
        with _expand_lock:
            for chunk in s.splitlines(True):
                if chunk.endswith("\n"):
                    chunk = chunk[:-1]
                    cls._BUF.append(chunk)
                    _expand_job["log"].append("".join(cls._BUF))
                    cls._BUF.clear()
                else:
                    cls._BUF.append(chunk)
            del _expand_job["log"][:-_EXPAND_LOG_MAX]

    def flush(self):
        pass


def _start_expand_job(kind, func, args=(), kwargs=None):
    """启动批量后台任务。已有任务在跑时返回错误 dict, 否则返回 None。"""
    kwargs = kwargs or {}
    with _expand_lock:
        if _expand_job["running"]:
            return {"error": f"已有任务在运行({_expand_job['kind']}),请等待其结束"}
        _expand_job.update(
            running=True, kind=kind, log=[], result=None, error=None,
            cancel=False,
            started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), finished=None,
        )
        _JobLog._BUF.clear()

    def worker():
        import contextlib

        try:
            with contextlib.redirect_stdout(_JobLog()):
                res = func(*args, **kwargs)
            with _expand_lock:
                _expand_job["result"] = res
        except Exception as e:
            with _expand_lock:
                _expand_job["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _expand_lock:
                _expand_job["running"] = False
                _expand_job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=worker, daemon=True).start()
    return None


@app.route("/api/expand/preview")
def api_expand_preview():
    """三个候选来源的选取逻辑说明 + 当前实际候选数据(只读,不写池)。"""
    import uid_discovery

    owners = uid_discovery.owners_ranking(limit=20, min_refs=2)
    videos = uid_discovery._top_oids(10)
    manual_exists, manual_uids = uid_discovery.read_manual_file()

    return _json({
        "priority_order": ["manual(20)", "owner(50)", "reply_api(100)"],
        "owners": {
            "desc": "库内评论表的 video_owner_uid, 按被库内评论引用次数降序, "
                    "引用≥2 才入选(过滤冷门UP主)",
            "candidates": [{"uid": u, "refs": c} for u, c in owners],
        },
        "reply": {
            "desc": "库内跨用户被评论最多的视频(排除失效), 拉其评论区提取评论者 uid; "
                    "每个视频最多取 50 个, 按热度排序截断",
            "videos": [{"oid": oid, "refs": c} for oid, c in videos],
        },
        "manual": {
            "desc": "手动清单 uid_pool.txt, 每行一个 uid, # 为注释; "
                    "优先级最高, expand 时最先抓取",
            "file_exists": manual_exists,
            "uids": manual_uids[:100],
        },
        "pool_stats": db.pool_stats(),
    })


@app.route("/api/expand/discover", methods=["POST"])
def api_expand_discover():
    """运行候选发现(后台): source=owners/reply/manual。
    manual 支持直接传 uids 列表(页面输入框), 不依赖清单文件。
    """
    data = request.json or {}
    source = data.get("source", "")
    import uid_discovery

    if source == "owners":
        err = _start_expand_job(
            "owners", uid_discovery.discover_from_owners,
            (int(data.get("limit", 200)), int(data.get("min_refs", 2))),
        )
    elif source == "reply":
        err = _start_expand_job(
            "reply", uid_discovery.discover_from_reply_api,
            (int(data.get("top_videos", 20)), int(data.get("pages", 2)),
             int(data.get("cap", 50))),
            kwargs={"should_stop": _expand_cancelled},
        )
    elif source == "manual":
        uids = data.get("uids") or []
        if isinstance(uids, str):
            uids = [u.strip() for u in uids.replace(",", "\n").split("\n")]
        uids = [int(u) for u in uids if str(u).strip().isdigit()]
        if not uids:
            return _json({"error": "请输入至少一个数字 uid"}, 400)
        err = _start_expand_job(
            "manual",
            lambda: None if db.pool_add_uids(uids, "manual", 20) else None,
        )
        # manual 无 print 产物, 直接记录结果
        if not err:
            with _expand_lock:
                _expand_job["log"].append(
                    f"[manual] 输入 {len(uids)} 个 uid, 完成后看池状态"
                )
    else:
        return _json({"error": "source 必须是 owners/reply/manual"}, 400)

    if err:
        return _json(err, 409)
    return _json({"success": True, "kind": source})


@app.route("/api/expand/start", methods=["POST"])
def api_expand_start():
    """启动批量入库(后台): 从池中按优先级抓取候选。"""
    data = request.json or {}
    limit = max(1, int(data.get("limit", 50)))
    max_attempts = max(1, int(data.get("max_attempts", 3)))
    err = _start_expand_job(
        "expand", ingest.expand_ingest, (limit, max_attempts, True),
        kwargs={"should_stop": _expand_cancelled},
    )
    if err:
        return _json(err, 409)
    return _json({"success": True, "limit": limit, "max_attempts": max_attempts})


@app.route("/api/expand/cancel", methods=["POST"])
def api_expand_cancel():
    """优雅停止当前任务: expand 在当前用户处理完后停止, reply 在当前视频后停止。
    任务结束时状态已落库, 池中剩余候选下次续跑。
    """
    with _expand_lock:
        if not _expand_job["running"]:
            return _json({"error": "当前没有运行中的任务"}, 400)
        if _expand_job["kind"] in ("owners", "manual"):
            return _json({"error": "该发现任务通常几秒内完成, 无需停止"}, 400)
        _expand_job["cancel"] = True
        kind = _expand_job["kind"]
    print(f"[expand-api] 收到停止请求({kind})")
    return _json({"success": True,
                  "msg": "停止信号已发出, 当前用户/视频处理完后停止"})


@app.route("/api/expand/status")
def api_expand_status():
    """轮询批量任务状态与日志(支持 offset 增量拉取)。"""
    offset = request.args.get("offset", "0")
    try:
        offset = max(0, int(offset))
    except ValueError:
        offset = 0
    with _expand_lock:
        log_text = "\n".join(_expand_job["log"])
        resp = {
            "running": _expand_job["running"],
            "kind": _expand_job["kind"],
            "cancel": _expand_job.get("cancel", False),
            "started": _expand_job["started"],
            "finished": _expand_job["finished"],
            "log": log_text[offset:],
            "offset": len(log_text),
            "result": _expand_job["result"],
            "error": _expand_job["error"],
        }
    resp["pool_stats"] = db.pool_stats()
    return _json(resp)


@app.route("/api/expand/pool")
def api_expand_pool():
    """候选池明细(带来源/状态/尝试次数/错误)。"""
    rows = db.pool_list(limit=200)
    return _json({"stats": db.pool_stats(), "rows": rows})


# ---- 用户画像 ----


@app.route("/api/user_profile")
def api_user_profile():
    """获取用户画像: 按分区(tname)和标签(tags)聚合的兴趣分布。"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    try:
        profile = db.user_profile(uid)
        profile["uid"] = uid
        return _json(profile)
    except Exception as e:
        return _json({"error": f"查询失败: {str(e)}"}, 500)


# ---- 用户互动分析 ----


@app.route("/api/interactions", methods=["POST"])
def api_interactions():
    """查询两个用户之间的互动记录。
    请求体: {"uid_a": "123", "uid_b": "456"}
    """
    data = request.json or {}
    uid_a_str = str(data.get("uid_a", "")).strip()
    uid_b_str = str(data.get("uid_b", "")).strip()
    if not uid_a_str.isdigit() or not uid_b_str.isdigit():
        return _json({"error": "uid_a 和 uid_b 必须为数字"}, 400)
    uid_a, uid_b = int(uid_a_str), int(uid_b_str)
    if uid_a == uid_b:
        return _json({"error": "两个 UID 不能相同"}, 400)

    try:
        import interactions
        result = interactions.find_interactions(uid_a, uid_b, resolve_owners=True)
        # datetime 不可直接 json 序列化,已在 interactions 中转为 str
        return _json(result)
    except Exception as e:
        return _json({"error": f"查询失败: {e}"}, 500)


def _fmt_time(row):
    """把记录里的 created_at 由 datetime 格式化为 "YYYY-MM-DD HH:MM"(原地修改)。"""
    ct = row.get("created_at")
    if isinstance(ct, datetime):
        row["created_at"] = ct.strftime("%Y-%m-%d %H:%M")
    return row


# ---- 用户记录 ----


@app.route("/api/records/users")
def api_record_users():
    """列出库中已有数据的用户(用户记录视图的用户卡片列表)。"""
    try:
        users = db.list_ingested_users()
        for u in users:
            ct = u.get("last_ctime")
            u["last_ctime"] = ct.strftime("%Y-%m-%d %H:%M") if ct else None
        return _json({"users": users})
    except Exception as e:
        return _json({"error": f"查询失败: {str(e)}"}, 500)


@app.route("/api/records")
def api_records():
    """某用户最近 limit 条评论与弹幕。参数: uid, limit(默认 20)。"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 200))

    def _fmt(rows):
        out = []
        for row in rows:
            r = dict(row)
            ct = r.get("ctime")
            r["ctime"] = ct.strftime("%Y-%m-%d %H:%M") if ct else None
            # 分区名: 库里 tname 可能为空, 用本地 tid→分区名映射表兜底
            r["zone"] = zone_name(r.get("tid"), r.get("tname"))
            tags = r.get("tags")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            r["tags"] = tags if isinstance(tags, list) else []
            out.append(r)
        return out

    try:
        counts = db.count_rows(uid)
        return _json({
            "uid": uid,
            "comments": _fmt(db.fetch_recent_records("comment", uid, limit)),
            "danmu": _fmt(db.fetch_recent_records("danmu", uid, limit)),
            "comment_total": counts.get("comment", 0),
            "danmu_total": counts.get("danmu", 0),
        })
    except Exception as e:
        return _json({"error": f"查询失败: {str(e)}"}, 500)


# ---- 兴趣群体发现 ----


@app.route("/api/clusters")
def api_clusters():
    """兴趣群体发现(第二层): 基于标签向量余弦相似度的阈值连通分量聚类。

    参数:
      threshold: 可选, 显式指定连边阈值; 不传时按库内分布动态划档
      uid:       指定用户时只返回包含他的群体, 并附带其 Top 相似邻居
    """
    import clustering

    threshold = (request.args.get("threshold") or "").strip()
    if threshold:
        try:
            threshold = float(threshold)
        except ValueError:
            return _json({"error": "threshold 必须为数字"}, 400)
        if not (0 < threshold <= 1):
            return _json({"error": "threshold 必须在 0~1 之间"}, 400)
    else:
        threshold = None  # 不指定时按库内分布动态划档

    uid = (request.args.get("uid") or "").strip()
    if uid and not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)

    try:
        result = clustering.discover(threshold=threshold, uid=int(uid) if uid else None)
        return _json(result)
    except Exception as e:
        return _json({"error": f"查询失败: {str(e)}"}, 500)


# ---- 话题传播与关键节点(第三层) ----


@app.route("/api/topics")
def api_topics():
    """可选话题列表(第三层): 默认只列被 >=2 个库内用户涉及的标签。

    参数:
      q:     关键词过滤(子串匹配)
      limit: 返回条数上限(默认 100)
    """
    import topic_graph

    keyword = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))

    try:
        items = topic_graph.topic_candidates(keyword=keyword or None, limit=limit)
        return _json({"topics": items, "total": len(items), "keyword": keyword})
    except Exception as e:
        return _json({"error": f"查询失败: {str(e)}"}, 500)


@app.route("/api/topic_graph")
def api_topic_graph():
    """话题关系图(第三层): 话题 / 视频 / 库内用户 / UP主 四类节点。

    参数:
      tag:   话题标签(视频标签, 如 "足球")
      limit: 图里最多保留多少个视频节点(默认 40)
    """
    import topic_graph

    tag = (request.args.get("tag") or "").strip()
    if not tag:
        return _json({"error": "tag 不能为空"}, 400)
    try:
        limit = int(request.args.get("limit", str(topic_graph.MAX_VIDEOS)))
    except ValueError:
        limit = topic_graph.MAX_VIDEOS
    limit = max(1, min(limit, 200))

    try:
        result = topic_graph.build_graph(tag, max_videos=limit)
        return _json(result)
    except Exception as e:
        return _json({"error": f"查询失败: {str(e)}"}, 500)


# ---- 历史记录管理 ----


@app.route("/api/history")
def api_history_list():
    """列出某 uid 的历史记录。参数: uid, kind(可选)。"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    kind = request.args.get("kind", "").strip() or None
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    try:
        records = db.list_history(uid, kind=kind, limit=limit)
        for r in records:
            _fmt_time(r)
        return _json({"records": records})
    except Exception as e:
        return _json({"error": f"查询失败: {e}"}, 500)


@app.route("/api/history/<int:record_id>")
def api_history_get(record_id):
    """获取单条历史记录的完整内容。参数: uid。"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    try:
        record = db.get_history(uid, record_id)
        if not record:
            return _json({"error": "记录不存在或不属于该用户"}, 404)
        _fmt_time(record)
        return _json({"record": record})
    except Exception as e:
        return _json({"error": f"查询失败: {e}"}, 500)


@app.route("/api/history/<int:record_id>", methods=["DELETE"])
def api_history_delete(record_id):
    """删除单条历史记录。参数: uid。"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    try:
        ok = db.delete_history(uid, record_id)
        if not ok:
            return _json({"error": "记录不存在或不属于该用户"}, 404)
        return _json({"success": True})
    except Exception as e:
        return _json({"error": f"删除失败: {e}"}, 500)


if __name__ == "__main__":
    import webbrowser

    # 延迟 1.5 秒打开浏览器,等 Flask 启动就绪
    port = int(os.getenv("PORT", "5000"))
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:%d/" % port)).start()
    # threaded=True: 抓取耗时1-2分钟,长请求不阻塞其他请求
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
