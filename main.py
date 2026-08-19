"""CLI 入口: initdb / ingest / ask / report / count。

用法:
    python main.py initdb
    python main.py ingest 2 --kinds comment,danmu --pages 0
    python main.py ask 2 "这个用户最近在吐槽什么?"
    python main.py report 2 --sample 200
    python main.py count
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

    p_ask = sub.add_parser("ask", help="对该 uid 问答")
    p_ask.add_argument("uid", type=int)
    p_ask.add_argument("question")

    p_rep = sub.add_parser("report", help="生成舆情报告")
    p_rep.add_argument("uid", type=int)
    p_rep.add_argument("--sample", type=int, default=200)

    sub.add_parser("count", help="向量库条数")

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


if __name__ == "__main__":
    main()
