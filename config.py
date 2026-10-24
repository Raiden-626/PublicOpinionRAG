"""配置加载: 从 .env 读取 SiliconFlow / MySQL / Chroma 配置。"""

import os
from dotenv import load_dotenv

# 项目根目录(config.py 所在目录)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 显式指定 .env 路径: 从任意目录启动都能读到配置
load_dotenv(os.path.join(_BASE_DIR, ".env"))

# ---- SiliconFlow API ----
API_KEY = os.getenv("KEY")
API_BASE = os.getenv("BASE", "https://api.siliconflow.cn/v1")
LLM_MODEL = os.getenv("MODEL", "deepseek-ai/DeepSeek-V4-Flash")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")
EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度

# ---- MySQL(全量结构化数据源)----
MYSQL = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DB", "public_opinion"),
    "charset": "utf8mb4",
}

# ---- Chroma(向量检索索引)----
CHROMA_PATH = os.getenv("CHROMA_PATH", os.path.join(_BASE_DIR, "chroma_db"))
COLLECTION = os.getenv("CHROMA_COLLECTION", "bilibili_comments")

# ---- 检索参数 ----
TOP_K = int(os.getenv("TOP_K", "8"))

# ---- 失效视频跳过 ----
# 开启后: B站接口确认"稿件不可见/已删除"(code=62002 / -404)的视频会被记入 dead_video 表;
#         之后的交互查询与批量补全都不再对这些 oid 请求 API。
# 关闭时: 不标记也不跳过, 保持原行为。
SKIP_DEAD_VIDEOS = os.getenv("SKIP_DEAD_VIDEOS", "0").strip().lower() in ("1", "true", "yes", "on")

