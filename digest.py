#!/usr/bin/env python3
"""内容雷达日报：每天晚上给乔帮主发一条私信（小K），汇总当天入表的内容。

不管有没有强烈推荐都发。没有新内容时也明说，不用登录飞书去猜是不是漏了。
读的是飞书表本身，不依赖 radar.py 当趟的运行结果。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

from radar import (BASE_URL, CST, DRY_RUN, HOT_FIELDS, HOT_TABLE, LEVELS, SUB_FIELDS, SUB_TABLE, Feishu, log)


def build_digest(now: datetime, subs: list[dict], hots: list[dict], last: int | None, sub_tid: str, hot_tid: str) -> str:
    lines = []
    if not subs and not hots:
        lines.append("今天没有新内容入表。如果连续好几天都这样，去看运行记录。")
    count = {lv: sum(1 for r in subs if r["推荐等级"] == lv) for lv in LEVELS}
    lines.append(f"**订阅日报**：今天新入表 {len(subs)} 条（强烈推荐 {count['强烈推荐']} / 推荐 {count['推荐']} / 一般 {count['一般']}）")
    for level, icon in (("强烈推荐", "🔥"), ("推荐", "✅")):
        lines += [f"- {icon} {r['博主']}｜{r['标题'][:30]}" for r in subs if r["推荐等级"] == level][:8]
    lines.append(f"**AI 热点**：今天新入 {len(hots)} 条待判断")
    lines += [f"- {h['选题'][:40]}" for h in hots[:3]]
    lines += ["", f"👉 [打开订阅日报]({BASE_URL}?table={sub_tid})　[打开 AI 热点]({BASE_URL}?table={hot_tid})"]
    lines += ["", f"最近一次入表：{datetime.fromtimestamp(last / 1000, CST):%-m月%-d日 %H:%M}" if last else "表里还没有任何记录"]
    return "\n".join(lines)


def main() -> int:
    missing = [k for k in ("XIAOK_APP_ID", "XIAOK_APP_SECRET") if not os.environ.get(k)]
    if missing:
        log(f"缺少环境变量：{', '.join(missing)}")
        return 1
    fs = Feishu(os.environ["XIAOK_APP_ID"], os.environ["XIAOK_APP_SECRET"])
    now = datetime.now(CST)
    today = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    sub_tid = fs.ensure_table(SUB_TABLE, SUB_FIELDS)
    hot_tid = fs.ensure_table(HOT_TABLE, HOT_FIELDS)
    sub_all = fs.records(sub_tid, ["标题", "博主", "推荐等级"])
    hot_all = fs.records(hot_tid, ["选题"])
    last = max((r["created"] for r in sub_all + hot_all), default=None)

    text = build_digest(now,
                        [r for r in sub_all if r["created"] >= today],
                        [r for r in hot_all if r["created"] >= today],
                        last, sub_tid, hot_tid)
    title = f"📋 内容雷达 · {now:%-m月%-d日} 日报"
    if DRY_RUN:
        log(f"[试跑] 不发消息。预览标题：{title}")
        print(text)
        return 0
    fs.send(title, text)
    log("日报已发")
    return 0


if __name__ == "__main__":
    sys.exit(main())
