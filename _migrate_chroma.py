"""迁移脚本: 清理 Chroma 中的旧格式记录。

旧格式特征:
- ID 格式: `{kind}_{mysql_id}` (如 `comment_1`, `danmu_42`)
- mysql_table: `bilibili_comment` / `bilibili_danmu` (无 uid 后缀)
- 这些记录指向的 MySQL 表已不存在,无法回查原文

新格式:
- ID: `{kind}_{uid}_{mysql_id}` (如 `comment_49869761_96`)
- mysql_table: `bilibili_comment_{uid}` / `bilibili_danmu_{uid}`

运行: py _migrate_chroma.py
"""
import chromadb
from config import CHROMA_PATH, COLLECTION

def _is_old_format(id_str, meta):
    """判断一条记录是否为旧格式。"""
    # 旧 ID 格式: 只有 1 个下划线 (如 comment_123)
    # 新 ID 格式: 至少 2 个下划线 (如 comment_49869761_123)
    if id_str.count("_") < 2:
        return True
    # 旧 mysql_table 无 uid 后缀
    table = meta.get("mysql_table", "")
    if table in ("bilibili_comment", "bilibili_danmu"):
        return True
    return False


def diagnose():
    """诊断 Chroma 中的数据格式。"""
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_collection(COLLECTION)
    
    all_data = collection.get()
    ids = all_data["ids"]
    metas = all_data["metadatas"]
    
    old_records = []
    new_records = []
    
    for id_str, meta in zip(ids, metas):
        if _is_old_format(id_str, meta):
            old_records.append((id_str, meta))
        else:
            new_records.append((id_str, meta))
    
    print(f"=== Chroma 数据诊断 ===")
    print(f"总记录数: {len(ids)}")
    print(f"旧格式记录: {len(old_records)}")
    print(f"新格式记录: {len(new_records)}")
    
    if old_records:
        print(f"\n--- 旧格式记录详情 ---")
        # 按 uid 分组统计
        uid_counts = {}
        table_counts = {}
        for id_str, meta in old_records:
            uid = meta.get("uid")
            table = meta.get("mysql_table", "unknown")
            uid_counts[uid] = uid_counts.get(uid, 0) + 1
            table_counts[table] = table_counts.get(table, 0) + 1
        
        print(f"按 uid 统计: {uid_counts}")
        print(f"按 mysql_table 统计: {table_counts}")
        print(f"\n示例旧 ID: {[r[0] for r in old_records[:5]]}")
    
    # 统计新格式中各 uid 的分布
    if new_records:
        print(f"\n--- 新格式记录分布 ---")
        uid_counts = {}
        for id_str, meta in new_records:
            uid = meta.get("uid")
            uid_counts[uid] = uid_counts.get(uid, 0) + 1
        print(f"按 uid 统计: {uid_counts}")
    
    return old_records, new_records


def migrate(dry_run=True):
    """执行迁移(默认 dry_run=True 只诊断不删除)。"""
    old_records, new_records = diagnose()
    
    if not old_records:
        print("\n没有旧格式记录,无需迁移。")
        return
    
    old_ids = [r[0] for r in old_records]
    
    if dry_run:
        print(f"\n[DRY RUN] 将删除 {len(old_ids)} 条旧格式记录。")
        print("确认删除请运行: py _migrate_chroma.py --execute")
        return
    
    # 执行删除
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_collection(COLLECTION)
    
    print(f"\n正在删除 {len(old_ids)} 条旧格式记录...")
    # Chroma delete 接口: 分批删除(每批 100 条)
    batch_size = 100
    for i in range(0, len(old_ids), batch_size):
        batch = old_ids[i:i+batch_size]
        collection.delete(ids=batch)
        print(f"  已删除 {min(i+batch_size, len(old_ids))}/{len(old_ids)}")
    
    # 验证
    final_count = collection.count()
    print(f"\n迁移完成! 最终记录数: {final_count}")
    print(f"删除了 {len(old_ids)} 条旧格式记录。")
    
    # 再次诊断
    print("\n=== 迁移后诊断 ===")
    diagnose()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="清理 Chroma 中的旧格式记录")
    parser.add_argument("--execute", action="store_true", help="实际执行删除(默认只诊断)")
    args = parser.parse_args()
    
    migrate(dry_run=not args.execute)
