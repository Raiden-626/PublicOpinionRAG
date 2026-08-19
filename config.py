"""配置加载: 从 .env 读取 SiliconFlow / MySQL / Chroma 配置。"""
import os
from dotenv import load_dotenv

load_dotenv()

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
CHROMA_PATH = os.getenv("CHROMA_PATH", "./chroma_db")
COLLECTION = os.getenv("CHROMA_COLLECTION", "bilibili_comments")

# ---- 检索参数 ----
TOP_K = int(os.getenv("TOP_K", "8"))
