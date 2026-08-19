"""B站用户舆情分析 — 可视化页面后端(Flask)。

功能:
  1. POST /api/ingest  {uid}        抓取评论+弹幕,入 MySQL+Chroma
  2. GET  /api/status  ?uid=        查该 uid 现有评论/弹幕条数
  3. POST /api/focus   {uid}        最近在关注的内容(后台任务+轮询)
  4. POST /api/report  {uid}        舆情分析报告(后台任务+轮询)
  5. POST /api/ask     {uid,question} RAG 问答(后台任务+轮询)
  6. GET  /api/task    ?task_id=    轮询任务状态/累积文本
  7. POST /api/re_embed {uid}       补全缺失向量(MySQL→Chroma)

启动:
  .venv\\Scripts\\python.exe app.py
  浏览器打开 http://127.0.0.1:5000/
"""
import json
import os
import threading
import time

# Playwright 浏览器在项目内,导入 ingest 前设好
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pw_browsers"),
)

from flask import Flask, render_template, request, jsonify

import db
import generate
import ingest

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


def _run_task(task_key, gen_func, *args, extra_capture=None, **kwargs):
    """后台线程: 运行生成器,累积文本到 _tasks[task_key]。
    extra_capture: 生成结束后要从函数对象上读取的属性名列表(如 ['sources'])。
    """
    entry = _tasks[task_key]
    try:
        for token in gen_func(*args, **kwargs):
            with _tasks_lock:
                if _tasks.get(task_key, {}).get("cancelled"):
                    return
                _tasks[task_key]["text"] += token
                _tasks[task_key]["updated"] = time.time()
        # 捕获额外数据(如 ask_stream.sources)
        if extra_capture:
            extra = {}
            for attr in extra_capture:
                val = getattr(gen_func, attr, None)
                if val is not None:
                    extra[attr] = val
            entry["extra"] = extra
        entry["status"] = "done"
    except Exception as e:
        entry["status"] = "failed"
        entry["error"] = str(e)


@app.route("/api/task")
def api_task():
    """轮询任务状态。返回 {status, text, error?, extra?}。"""
    task_key = request.args.get("task_id", "").strip()
    if not task_key:
        return _json({"error": "缺少 task_id"}, 400)
    with _tasks_lock:
        entry = _tasks.get(task_key)
    if not entry:
        return _json({"error": "任务不存在"}, 404)
    resp = {
        "status": entry["status"],
        "text": entry["text"],
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
    
    # 添加 Chroma 向量数量(调试用)
    import vector_store
    chroma_counts = {
        "comment": vector_store.count_by_uid(uid, kind="comment"),
        "danmu": vector_store.count_by_uid(uid, kind="danmu"),
    }
    
    return _json({"uid": uid, "counts": counts, "chroma_vectors": chroma_counts})


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    try:
        res = ingest.ingest_uid(uid, headless=False)
    except Exception as e:
        return _json({"error": f"抓取失败: {e}"}, 500)
    res["uid"] = uid
    res["counts"] = _counts(uid)
    return _json(res)


# ---- 后台任务端点(刷新不丢失) ----

def _start_or_reconnect(task_key, gen_func, args, counts, extra_capture=None):
    """查找已有任务或创建新任务。返回 (task_key, entry)。"""
    with _tasks_lock:
        existing = _tasks.get(task_key)
        if existing and existing["status"] in ("running", "done"):
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
    t = threading.Thread(
        target=_run_task,
        args=(task_key, gen_func, *args),
        kwargs={"extra_capture": extra_capture} if extra_capture else {},
        daemon=True,
    )
    t.start()
    return task_key, entry


@app.route("/api/focus", methods=["POST"])
def api_focus():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    counts = _counts(uid)
    task_key = f"focus_{uid}"
    _, entry = _start_or_reconnect(task_key, generate.recent_focus_stream, (uid,), counts)
    return _json({"task_id": task_key, "counts": counts,
                    "status": entry["status"], "text": entry["text"]})


@app.route("/api/report", methods=["POST"])
def api_report():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    counts = _counts(uid)
    task_key = f"report_{uid}"
    _, entry = _start_or_reconnect(task_key, generate.report_stream, (uid,), counts)
    return _json({"task_id": task_key, "counts": counts,
                    "status": entry["status"], "text": entry["text"]})


@app.route("/api/ask", methods=["POST"])
def api_ask():
    uid, err = _parse_uid(request.json)
    if err:
        return err
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return _json({"error": "请输入问题"}, 400)
    counts = _counts(uid)
    task_key = f"ask_{uid}_{question}"
    _, entry = _start_or_reconnect(
        task_key, generate.ask_stream, (question, uid), counts,
        extra_capture=["sources"],
    )
    resp = {"task_id": task_key, "counts": counts,
            "status": entry["status"], "text": entry["text"]}
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

    import vector_store
    from clients import embed

    results = {}
    for kind in ("comment", "danmu"):
        table = db.table_for(kind, uid)
        # 用 Chroma get() 按 metadata 查已有的 mysql_id,无需浪费 embedding 调用
        existing_ids = vector_store.get_mysql_ids(where={"uid": uid, "mysql_table": table})

        # 找 MySQL 中缺失的行
        missing_ids = db.fetch_ids_without_vector(kind, uid, existing_ids)
        if not missing_ids:
            results[kind] = {"missing": 0, "embedded": 0}
            continue

        rows = db.fetch_rows_by_ids(kind, uid, missing_ids[:200])  # 每次最多补 200 条
        texts = [r["content"] for r in rows]
        try:
            vecs = embed(texts)
        except Exception as e:
            results[kind] = {"missing": len(missing_ids), "embedded": 0, "error": str(e)}
            continue

        ids, metas, docs = [], [], []
        for row, vec in zip(rows, vecs):
            ids.append(f"{kind}_{row['id']}")
            metas.append({
                "uid": uid,
                "type": table,
                "mysql_table": table,
                "mysql_id": row["id"],
                "oid": row.get("oid") or 0,
            })
            docs.append(row["content"])
        vector_store.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
        results[kind] = {"missing": len(missing_ids), "embedded": len(rows)}

    results["uid"] = uid
    results["counts"] = _counts(uid)
    return _json(results)


# ---- 历史记录管理 ----

@app.route("/api/history")
def api_history_list():
    """列出某 uid 的历史记录。参数: uid, kind(可选)。"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return _json({"error": "uid 必须为数字"}, 400)
    uid = int(uid)
    kind = request.args.get("kind", "").strip() or None
    limit = int(request.args.get("limit", "50"))
    try:
        records = db.list_history(uid, kind=kind, limit=limit)
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
    # threaded=True: 抓取耗时1-2分钟,长请求不阻塞其他请求
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
