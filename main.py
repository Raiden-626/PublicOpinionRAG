"""CLI 入口: initdb / ingest / ask / report / count / resolve。

用法:
    python main.py initdb
    python main.py ingest 2 --kinds comment,danmu --pages 0
    python main.py ask 2 "这个用户最近在吐槽什么?"
    python main.py report 2 --sample 200
    python main.py count
    python main.py resolve 2          # 补全 uid=2 的评论/弹幕表中缺失的视频信息
    python main.py resolve --all      # 补全所有 uid 的缺失信息
"""
import argparse


def main():
    ap = argparse.ArgumentParser(description="B站用户舆情 RAG")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb", help="建库建表")

    p_ing = sub.add_parser("ingest", help="抓取某 uid 的评论/弹幕并入库")
    p_ing.add_argument("uid", type=int)
    p_ing.add_argument("--kinds", default="comment,danmu",
                       help="抓取类型,逗号分隔: comment,danmu")
    p_ing.add_argument("--pages", type=int, default=0, help="最大翻页数,0=全部")
    p_ing.add_argument("--headful", action="store_true", help="有头浏览器(无头被拦时用)")
    p_ing.add_argument("--overwrite", action="store_true", help="覆盖模式:清空旧数据后重新入库")

    p_ask = sub.add_parser("ask", help="对该 uid 问答")
    p_ask.add_argument("uid", type=int)
    p_ask.add_argument("question")

    p_rep = sub.add_parser("report", help="生成舆情报告")
    p_rep.add_argument("uid", type=int)
    p_rep.add_argument("--sample", type=int, default=200)

    sub.add_parser("count", help="向量库条数")

    p_res = sub.add_parser("resolve", help="补全缺失的视频信息(tid/tname/tags)")
    p_res.add_argument("uid", type=int, nargs="?", help="要补全的用户uid")
    p_res.add_argument("--all", action="store_true", help="补全所有用户")

    p_rep = sub.add_parser("repair", help="用本地映射表离线修复 tname(不发API请求)")
    p_rep.add_argument("uid", type=int, nargs="?", help="要修复的用户uid")
    p_rep.add_argument("--all", action="store_true", help="修复所有用户")

    args = ap.parse_args()

    if args.cmd == "initdb":
        import db
        db.init_db()
        print("数据库与表已就绪")

    elif args.cmd == "ingest":
        import ingest
        kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
        ingest.ingest_uid(
            args.uid, kinds=kinds,
            max_pages=args.pages or None,
            headless=not args.headful,
            overwrite=args.overwrite,
        )

    elif args.cmd == "ask":
        import generate
        # 消费流式生成器,逐 token 打印
        for token in generate.ask_stream(args.question, args.uid):
            print(token, end="", flush=True)
        print()  # 换行
        srcs = getattr(generate.ask_stream, "sources", [])
        if srcs:
            print("\n-- 参考 --")
            for s in srcs[:8]:
                d = s["ctime"].strftime("%Y-%m-%d") if s.get("ctime") else ""
                print(f"[{d}] {s['content'][:60]}")

    elif args.cmd == "report":
        import generate
        for token in generate.report_stream(args.uid, sample_n=args.sample):
            print(token, end="", flush=True)
        print()

    elif args.cmd == "count":
        import vector_store
        print("向量库条数:", vector_store.count())

    elif args.cmd == "resolve":
        import ingest
        
        if args.all:
            uids = None  # backfill_video_info will auto-discover all uids
        elif args.uid:
            uids = [args.uid]
        else:
            print("请指定 uid 或使用 --all 参数")
            p_res.print_help()
            return
        
        stats = ingest.backfill_video_info(uids=uids)
        print(f"\n补全完成: 扫描 {stats['scanned']} 条, 更新 {stats['updated']} 条, "
              f"跳过 {stats['skipped']} 条, 失败 {stats['failed']} 条")

    elif args.cmd == "repair":
        import ingest

        if args.all:
            uids = None
        elif args.uid:
            uids = [args.uid]
        else:
            print("请指定 uid 或使用 --all 参数")
            p_rep.print_help()
            return

        stats = ingest.repair_tnames_offline(uids=uids)
        print(f"\n修复完成: 扫描 {stats['scanned']} 条, 更新 {stats['updated']} 条, "
              f"跳过 {stats['skipped']} 条, 未知分区 {stats['unknown_tids']}")


if __name__ == "__main__":
    main()
