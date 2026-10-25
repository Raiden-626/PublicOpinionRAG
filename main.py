"""CLI 入口: initdb / ingest / ask / report / count / resolve。

用法:
    python main.py initdb
    python main.py ingest 2 --kinds comment,danmu --pages 0
    python main.py ask 2 "这个用户最近在吐槽什么?"
    python main.py report 2 --sample 200
    python main.py count
    python main.py resolve 2          # 补全 uid=2 的评论/弹幕表中缺失的视频信息
    python main.py resolve --all      # 补全所有 uid 的缺失信息
    python main.py repair --all       # 用本地映射表离线修复 tname(零 API 请求)
    python main.py mark-dead --all    # 确认并登记失效视频(稿件不可见/已删除)
    python main.py clear-dead         # 清除失效视频登记
    python main.py repair-rpid --all  # 离线修复评论表 rpid(默认预览, --write 才写库)
    python main.py repair-dead --write # 给 dead_video 表补 uid/kind
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

    p_md = sub.add_parser("mark-dead", help="确认并登记失效视频(稿件不可见/已删除)")
    p_md.add_argument("uid", type=int, nargs="?", help="要扫描的用户uid")
    p_md.add_argument("--all", action="store_true", help="扫描所有用户")
    p_md.add_argument("--limit", type=int, default=None,
                      help="每个用户最多确认多少个视频(默认不限制)")
    p_md.add_argument("--write", action="store_true",
                      help="把解析成功的 tid/tname/tags 写回数据库(顺带补全, 不额外发请求)")

    p_cd = sub.add_parser("clear-dead", help="清除失效视频登记")
    p_cd.add_argument("uid", type=int, nargs="?",
                      help="只清除该 uid 登记的; 省略则清除全部")

    p_rr = sub.add_parser("repair-rpid",
                          help="离线修复评论表 rpid(归位成评论自身id, 旧值迁到 root_id)")
    p_rr.add_argument("uid", type=int, nargs="?", help="只修某个 uid")
    p_rr.add_argument("--all", action="store_true", help="修所有用户")
    p_rr.add_argument("--write", action="store_true", help="真正写库(默认只预览)")

    p_rd = sub.add_parser("repair-dead", help="给 dead_video 表补 uid/kind")
    p_rd.add_argument("--write", action="store_true", help="真正写库(默认只预览)")

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

    elif args.cmd == "mark-dead":
        import ingest

        if args.all:
            uids = ingest.discover_uids()
        elif args.uid:
            uids = [args.uid]
        else:
            print("请指定 uid 或使用 --all 参数")
            p_md.print_help()
            return

        print(f"[mark-dead] 待扫描用户 {len(uids)} 个")
        totals = {"scanned": 0, "dead": 0, "alive": 0, "unknown": 0, "already": 0, "written": 0}
        for uid in uids:
            stats = ingest.mark_dead_videos_for_uid(
                uid, limit=args.limit, write_back=args.write
            )
            for k in totals:
                totals[k] += stats[k]
            print(f"[mark-dead] uid={uid}: 扫描 {stats['scanned']}, 失效 {stats['dead']}, "
                  f"存活 {stats['alive']}, 未知 {stats['unknown']}, 已登记 {stats['already']}, "
                  f"回写 {stats['written']}")

        print(f"\n确认完成: 扫描 {totals['scanned']}, 失效 {totals['dead']}, "
              f"存活 {totals['alive']}, 未知 {totals['unknown']}, "
              f"已登记 {totals['already']}, 回写 {totals['written']}")
        import db
        print(f"失效视频登记表现有 {len(db.get_dead_oids())} 条")

    elif args.cmd == "clear-dead":
        import db

        deleted = db.clear_dead_videos(args.uid)
        scope = "全部" if args.uid is None else f"uid={args.uid}"
        print(f"已清除{scope}的失效视频登记 {deleted} 条")

    elif args.cmd == "repair-rpid":
        import repair

        if args.uid:
            uids = [args.uid]
        elif args.all:
            uids = None
        else:
            print("请指定 uid 或使用 --all 参数")
            p_rr.print_help()
            return

        st = repair.repair_rpid(uids=uids, write=args.write)
        print(f"\nrpid 归位: 扫描 {st['scanned']}, 已更新 {st['updated']}, "
              f"待更新 {st['would_update']}, 无需改 {st['skipped']}, "
              f"无自身id {st['no_own']}, 冲突 {st['conflicts']}")
        if not args.write:
            print("(预览模式, 加 --write 才会写库)")

    elif args.cmd == "repair-dead":
        import repair

        st = repair.repair_dead_meta(write=args.write)
        print(f"\ndead_video 补全: 扫描 {st['scanned']}, 已更新 {st['updated']}, "
              f"待更新 {st['would_update']}, 无需改 {st['skipped']}, "
              f"找不到来源 {st['unresolved']}")
        if not args.write:
            print("(预览模式, 加 --write 才会写库)")


if __name__ == "__main__":
    main()
