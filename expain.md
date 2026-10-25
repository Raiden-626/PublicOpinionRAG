项目里 16 个 Python 文件，按角色分四类：

## 入口层

| 文件      | 作用                                                         |
| --------- | ------------------------------------------------------------ |
| `main.py` | CLI 入口。子命令：`initdb` 建库、`ingest` 抓取入库、`ask`/`report` 问答与报告（终端版）、`count` 查向量数、`resolve`/`repair`/`mark-dead`/`clear-dead`/`repair-rpid`/`repair-dead` 各类补全与修复 |
| `app.py`  | Flask Web入口。19 个 API 端点 + 后台任务状态机（`_tasks` 字典 + 锁 + 单飞 + force 重连），SSE 式流式输出的轮询端点 `/api/task` |

## 数据层

| 文件        | 作用                                                         |
| ----------- | ------------------------------------------------------------ |
| `db.py`     | MySQL 全部读写。建表/幂等迁移、per-uid 分表的动态表名与注入防护、`user_profile` 画像即时聚合、`list_ingested_users` 扫表名反推 uid、history 的 upsert、`_fallback_key` 备选去重键 |
| `ingest.py` | 抓取与入库全流程。Playwright 渲染 aicu.cc 解析评论/弹幕卡片、去重过滤、watermark 增量水位线、视频信息补全与分区名映射、失效视频登记、写 MySQL + 写 Chroma |
| `repair.py` | 存量数据离线修复工具（零 API 请求）。`repair_rpid` 把 rpid 归位成评论自身 id 并保住楼 id 不变量，`repair_dead_meta` 给 dead_video 补 uid/kind。默认只预览，`--write` 才写库 |

## 算法层

| 文件              | 作用                                                         |
| ----------------- | ------------------------------------------------------------ |
| `similarity.py`   | 用户兴趣相似度。标签向量 + IDF 加权 + 稀疏余弦，等级分档与共同关注点/差异点拆解 |
| `clustering.py`   | 兴趣群体发现。阈值图 + 并查集连通分量，群体命名、内聚度、松散群体提示 |
| `interactions.py` | 两用户互动分析。`on_video`（在对方视频下评论）与 `same_thread`（同一评论楼）两种口径，附兴趣相似度对比 |
| `topic_graph.py`  | 第三层话题传播图谱。标签→话题索引 + 全库互动索引，四类节点建图，用互动对数占比判定紧密圈子/松散/散点 |

## 向量与配置层

| 文件                 | 作用                                                         |
| -------------------- | ------------------------------------------------------------ |
| `vector_store.py`    | Chroma 封装。批量写入（用 count 前后差算真实写入数）、按 uid 检索（原生 where 失败时回退 Python 侧过滤）、按 metadata 反查已向量化的 mysql_id、按 uid 清空 |
| `retrieve.py`        | 检索链路。问题向量化 → Chroma 召回 → 按表分组回查 MySQL 原文 → 拼成带序号的上下文字符串 |
| `generate.py`        | 三个 LLM 流的 prompt 组装层。`ask_stream`（RAG 问答）、`recent_focus_stream`（最近关注）、`report_stream`（舆情报告）。另外含非 LLM 的 bigram 去重与字符预算截断 |
| `clients.py`         | SiliconFlow HTTP 客户端。`embed` 批量向量化（bge-large-zh，1024 维）、`chat_stream` SSE 流式对话 |
| `zones.py`           | B 站 tid → 分区名本地映射表（64 条），零请求离线查表；`--probe` 可联网扩展 |
| `config.py`          | 配置加载。从 `.env` 读 SiliconFlow / MySQL / Chroma /检索参数 / `SKIP_DEAD_VIDEOS` 开关 |
| `_migrate_chroma.py` | 一次性迁移脚本。把旧格式向量记录（id 缺 uid 段、mysql_table 无后缀）升级为新格式，因旧记录无法回查 MySQL 原文 |

依赖方向很干净：`main.py` / `app.py` 是仅有的两个上层入口，往下依次是 `generate.py` → `retrieve.py` → `vector_store.py`，以及 `similarity.py` → `clustering.py` / `interactions.py` / `topic_graph.py`，全部叶子最终落到 `db.py`。