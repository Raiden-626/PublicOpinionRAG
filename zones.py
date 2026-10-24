"""B站分区映射: tid -> 分区名。

背景: B站 view 接口(x/web-interface/view)已停发 tname / tname_v2(恒为空串),
     只保留 tid(旧分区ID) 与 tid_v2(新分类ID), 因此无法直接拿到分区名。
     分区名改由本地映射表提供: 纯离线查表, 不产生任何额外 API 请求。

映射表来源: B站榜单接口 x/web-interface/ranking/region?rid=<tid> 返回的 typename,
     并与搜索接口 x/web-interface/search/type 的 typename 交叉验证
     (抽检 24 个 tid: 22 个完全一致, 2 个为新体系里更细的名称, 旧名反而更贴合入库口径)。
     少数 rid 没有榜单(如 205), 改用搜索接口对具体视频取到的 typename 补齐。

扩展映射表(仅在遇到表里没有的 tid 时才需要):
    python zones.py --probe 24 155
"""

import time

# ---- tid -> 分区名 ----
TID_NAMES = {
    17: "单机游戏",
    21: "日常",
    22: "鬼畜调教",
    24: "MAD·AMV",
    27: "综合",
    28: "原创音乐",
    29: "音乐现场",
    30: "VOCALOID·UTAU",
    47: "同人·手书",
    59: "演奏",
    65: "网络游戏",
    75: "动物综合",
    85: "小剧场",
    86: "特摄",
    95: "数码",
    124: "社科·法律·心理",
    126: "人力VOCALOID",
    130: "音乐综合",
    137: "明星综合",
    138: "搞笑",
    153: "国产动画",
    158: "穿搭",
    161: "手工",
    162: "绘画",
    164: "健身",
    170: "资讯",
    171: "电子竞技",
    172: "手机游戏",
    173: "桌游棋牌",
    176: "汽车生活",
    180: "社会·美食·旅行",
    182: "影视杂谈",
    183: "影视剪辑",
    184: "预告·资讯",
    195: "动态漫·广播剧",
    199: "明星舞蹈",
    201: "科学科普",
    205: "社会",
    208: "校园学习",
    209: "职业职场",
    212: "美食侦探",
    213: "美食测评",
    215: "美食记录",
    216: "鬼畜剧场",
    218: "喵星人",
    219: "汪星人",
    228: "人文历史",
    230: "软件应用",
    235: "篮球",
    236: "竞技体育",
    237: "运动文化",
    238: "运动综合",
    240: "摩托车",
    242: "娱乐粉丝创作",
    246: "改装玩车",
    249: "足球",
    250: "出行",
    251: "三农",
    253: "动漫杂谈",
    254: "亲子",
    258: "汽车知识科普",
    260: "影视整活",
    263: "颜值安利",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_last_api_call = 0.0


def zone_name(tid, api_tname=None):
    """返回分区名。

    参数:
      tid: 视频的分区ID(B站 view 接口返回的 tid)
      api_tname: 接口自带的分区名(目前恒为空串, 仅作兜底)

    返回: 分区名字符串; 映射表里没有且接口也没给时返回 None。
    """
    if tid is not None:
        try:
            name = TID_NAMES.get(int(tid))
        except (TypeError, ValueError):
            name = None
        if name:
            return name
    if api_tname and api_tname.strip():
        return api_tname.strip()
    return None


def _rate_limit():
    """探测接口限流: 两次请求间至少间隔 1.2s。"""
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 1.2:
        time.sleep(1.2 - elapsed)
    _last_api_call = time.time()


def probe_tname(tid):
    """联网查某个 tid 的分区名(榜单接口)。查不到返回 None。

    仅用于扩展映射表, 平时不需要调用。
    """
    import os

    import requests

    headers = {"User-Agent": _UA}
    cookies = []
    for env_key, cookie_name in (
        ("BILI_SESSDATA", "SESSDATA"),
        ("BILI_BILI_JCT", "bili_jct"),
        ("BILI_DEDEUSERID", "DedeUserID"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            cookies.append(f"{cookie_name}={val}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    _rate_limit()
    try:
        resp = requests.get(
            f"https://api.bilibili.com/x/web-interface/ranking/region?rid={tid}&day=3",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("typename") or None
    except Exception:
        return None
    return None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="B站 tid -> 分区名 映射表")
    ap.add_argument("--probe", nargs="+", type=int, help="联网查询这些 tid 的分区名")
    args = ap.parse_args()

    if args.probe:
        for tid in args.probe:
            name = probe_tname(tid)
            known = TID_NAMES.get(tid)
            if name:
                mark = "(已在表内)" if known else "← 可加入 TID_NAMES"
                print(f"tid={tid}: {name} {mark}")
            else:
                print(f"tid={tid}: 查不到(该 rid 无榜单)")
    else:
        print(f"映射表共 {len(TID_NAMES)} 条, 用 --probe <tid...> 扩展")
