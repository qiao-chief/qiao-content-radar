#!/usr/bin/env python3
"""内容雷达巡检：不看"有没有报错"，只看"料是不是真的在进来"。

老系统（乔松观澜）失败的教训：健康检查测的是端口通不通，结果数据冻了三周没人发现。
所以这里每一项都是可量化的新鲜度和对账差额，不是"看起来正常"。

用法（本机，钥匙自动从本机配置读）：
    python3 check.py            巡检并打印报告
    python3 check.py --notify   巡检；不合格时让小K 私信乔帮主
    python3 check.py --days 3   对账窗口天数（默认 3，和 radar.py 的 WINDOW_DAYS 一致）
退出码：0 = 正常或仅观察项；1 = 有不合格项。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# getnote 装在 ~/.npm-global/bin、gh 在 /opt/homebrew/bin，这两个目录不一定在调用者的 PATH 里
# （WorkBuddy 的 shell 里就没有，结果误判成「工具没装、系统故障」）。这里自己补上。
TOOL_DIRS = [str(Path.home() / ".npm-global" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
os.environ["PATH"] = os.pathsep.join(TOOL_DIRS + [os.environ.get("PATH", "")])

import radar
from radar import CST, STALE_HOURS, TOPIC_ID, Feishu, getnote, parse_cst


def load_keys() -> dict:
    """云端（GitHub Actions）走环境变量；本机没有环境变量时，回落到 local_env 读本地配置。"""
    need = ("XIAOK_APP_ID", "XIAOK_APP_SECRET")
    if all(os.environ.get(k) for k in need):
        return {k: os.environ[k] for k in need}
    from local_env import load_env
    return load_env()

REPO = "qiao-chief/qiao-content-radar"
ACTIONS_URL = f"https://github.com/{REPO}/actions"
RUN_MAX_AGE_H = 26      # 每天至少该成功跑一趟，超过这个钟点没跑过就是定时任务出问题
ROW_MAX_AGE_H = 72      # 表里超过三天没进过新行，且料源有货，就是写表环节断了
RUNS_PER_DAY = 2        # Cloudflare Worker 每天触发两趟：09:13 和 18:19（见 worker/src/index.js）
RADAR_WORKFLOW = "内容雷达"  # radar.yml 的 name；台账只数它，不数看门狗和日报

OK, WARN, BAD, SKIP = "✅", "⚠️", "❌", "—"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.notes: list[str] = []

    def add(self, mark: str, name: str, measured: str, rule: str) -> None:
        self.rows.append((mark, name, measured, rule))

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def failed(self) -> bool:
        return any(r[0] == BAD for r in self.rows)

    @property
    def verdict(self) -> str:
        if self.failed:
            return "❌ 不合格：下面标 ❌ 的项要马上处理"
        if any(r[0] == WARN for r in self.rows):
            return "⚠️ 基本正常，但有观察项"
        return "✅ 正常"

    def markdown(self) -> str:
        lines = [f"**{self.verdict}**", "", "| | 检查项 | 实测 | 判据 |", "|---|---|---|---|"]
        lines += [f"| {m} | {n} | {v} | {r} |" for m, n, v, r in self.rows]
        if self.notes:
            lines += [""] + [f"- {t}" for t in self.notes]
        return "\n".join(lines)

    def text(self) -> str:
        width = max(len(n) for _, n, _, _ in self.rows)
        lines = [f"{m} {n.ljust(width)}  {v}   〔判据 {r}〕" for m, n, v, r in self.rows]
        return "\n".join(lines + [""] + [f"· {t}" for t in self.notes] + ["", self.verdict])


def hours_since(t: datetime, now: datetime) -> float:
    return (now - t).total_seconds() / 3600


def check_schedule(rep: Report, now: datetime) -> bool | None:
    """返回今天是否已有成功运行；None 表示没测出来（gh 不可用）。"""
    """云端到底跑没跑。抓的是「该跑却没触发」。现在定时由 Cloudflare Worker 发起，记录里是 workflow_dispatch。"""
    try:
        raw = subprocess.run(["gh", "api", f"repos/{REPO}/actions/runs?per_page=60"],
                             capture_output=True, text=True, timeout=60)
        runs = [r for r in json.loads(raw.stdout)["workflow_runs"] if r["name"] == RADAR_WORKFLOW]
    except Exception as e:
        rep.add(SKIP, "云端定时任务", f"未测（gh 命令不可用：{str(e)[:40]}）", "每天≥1次成功")
        return None

    def when(r: dict) -> datetime:
        return datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(CST)

    ok_runs = [r for r in runs if r["conclusion"] == "success"]
    if not ok_runs:
        rep.add(BAD, "云端定时任务", "查不到任何成功运行", f"≤{RUN_MAX_AGE_H}小时内有成功运行")
        return False

    age = hours_since(when(ok_runs[0]), now)
    mark = OK if age <= RUN_MAX_AGE_H else BAD
    rep.add(mark, "云端定时任务", f"最近一次成功 {age:.1f} 小时前", f"≤{RUN_MAX_AGE_H}小时")

    # 近 5 天台账：每天该有 RUNS_PER_DAY 趟，少了就是被 GitHub 丢了。
    # 系统上线前的日子不算数，否则永远飘红。
    born = when(runs[-1]).date()
    ledger: dict[str, list[str]] = {}
    for r in runs:
        if r["event"] in ("schedule", "workflow_dispatch"):  # 手动跑也会计入，偶尔多算一趟不影响判断
            ledger.setdefault(when(r).strftime("%m-%d"), []).append(when(r).strftime("%H:%M"))
    days = [now - timedelta(days=i) for i in range(5) if (now - timedelta(days=i)).date() > born]
    missed = [d for d in days[1:] if len(ledger.get(d.strftime("%m-%d"), [])) < RUNS_PER_DAY]  # 今天可能还没跑完，不算
    rep.add(WARN if missed else OK, "定时触发台账",
            " / ".join(f"{d:%m-%d} {len(ledger.get(d.strftime('%m-%d'), []))}趟" for d in days) or "上线首日，还没有整天台账",
            f"每天 {RUNS_PER_DAY} 趟")
    if missed:
        rep.note(f"少跑的那趟多半是 Cloudflare 定时没发出去，先看 Worker 日志（cloud/worker 里 npx wrangler tail），"
                 f"再看 GITHUB_TOKEN 是否过期。运行记录：{ACTIONS_URL}")
    return any(when(r).date() == now.date() for r in ok_runs)


def check_source(rep: Report, now: datetime, days: int) -> list[dict] | None:
    """得到大脑这头的料。比 radar 的窗口多看一天，才能发现「已经掉出窗口、再也补不回来」的条目。"""
    since = now - timedelta(days=days + 1)
    try:
        bloggers = getnote("kb", "bloggers", TOPIC_ID).get("bloggers") or []
    except FileNotFoundError:  # 本机环境缺工具，不是系统故障，别报成故障
        rep.add(SKIP, "得到大脑供料", "未测：本机找不到 getnote 命令", "命令能跑通")
        rep.note("装它：`npm i -g @getnote/cli@1.5.10`，再 `getnote auth login`。"
                 "云端 GitHub Actions 每次自己装，所以这不影响系统运行，只是本机少测两项。")
        return None
    except Exception as e:
        rep.add(BAD, "得到大脑供料", f"拉不动：{str(e)[:60]}", "命令能跑通")
        rep.note("多半是会员到期或 API key 失效。续会员，或本机 `getnote auth login` 重新授权后重跑一键配置。")
        return None

    bad = [b for b in bloggers if b.get("hook_state") != "READY"]
    rep.add(BAD if bad else OK, "博主订阅状态",
            f"{len(bloggers)} 个，异常 {len(bad)} 个" + (f"（{'、'.join(b.get('account_name', '?') for b in bad)}）" if bad else ""),
            "10 个全 READY")

    items: list[dict] = []
    for b in bloggers:
        name = b.get("account_name") or b["follow_id_str"]
        for it in radar.recent_items(b["follow_id_str"], since):
            at = parse_cst(it["post_publish_time"])
            if at >= since:
                items.append({"id": it["post_id_alias"], "博主": name, "发布": at})

    latest = max((i["发布"] for i in items), default=None)
    if latest is None:
        rep.add(BAD, "料源新鲜度", f"近 {days} 天一条新视频都没有", f"≤{STALE_HOURS}小时")
    else:
        age = hours_since(latest, now)
        rep.add(OK if age <= STALE_HOURS else BAD, "料源新鲜度",
                f"最新一条 {age:.1f} 小时前（{latest:%m-%d %H:%M}）", f"≤{STALE_HOURS}小时")
    return items


def check_tables(rep: Report, fs: Feishu, now: datetime, source: list[dict] | None, days: int) -> None:
    sub_tid = fs.ensure_table(radar.SUB_TABLE, radar.SUB_FIELDS)
    hot_tid = fs.ensure_table(radar.HOT_TABLE, radar.HOT_FIELDS)
    rows = fs.records(sub_tid, ["内容ID", "推荐等级", "发布时间"])
    hots = fs.records(hot_tid, ["选题"])
    rep.add(OK, "飞书两张表", f"订阅日报 {len(rows)} 行 / AI热点推荐 {len(hots)} 行", "能读到")

    # 入表新鲜度
    newest = max((r["created"] for r in rows), default=0)
    if newest:
        age = hours_since(datetime.fromtimestamp(newest / 1000, CST), now)
        stale = age > ROW_MAX_AGE_H and bool(source)
        rep.add(BAD if stale else OK, "入表新鲜度", f"最近一行 {age:.1f} 小时前",
                f"≤{ROW_MAX_AGE_H}小时（料源有货时）")
    else:
        rep.add(BAD, "入表新鲜度", "表里一行都没有", "至少有一行")

    # 对账：得到大脑有的，表里是不是都写进去了。
    # radar.py 每趟往回看 days 天，所以窗口内漏的下一趟会自己补上（得到大脑转写有延迟，属正常）；
    # 只有掉出窗口还没进表的，才是再也补不回来的真漏。
    if source is None:
        rep.add(SKIP, "对账", "未测：拿不到料源，无从比对", f"永久漏 0 条（掉出 {days} 天窗口才算）")
        lost = pending = []
    else:
        in_table = {r["内容ID"] for r in rows if r["内容ID"]}
        edge = now - timedelta(days=days)
        missing = [i for i in source if i["id"] not in in_table]
        lost = [i for i in missing if i["发布"] < edge]
        pending = [i for i in missing if i["发布"] >= edge]
        rep.add(BAD if lost else OK, f"对账（近{days + 1}天）",
                f"料源 {len(source)} 条，永久漏 {len(lost)} 条，待下趟补 {len(pending)} 条",
                f"永久漏 0 条（掉出 {days} 天窗口才算）")
    if lost:
        rep.note("永久漏清单：" + "、".join(f"{m['博主']}/{m['发布']:%m-%d %H:%M}" for m in lost[:8]))
        rep.note(f"这些已经掉出 {days} 天窗口，定时任务再也捞不到。先跑 `python3 local_env.py --dry-run` 确认，"
                 f"必要时临时加大窗口：`RADAR_WINDOW_DAYS=7 python3 local_env.py`。")
    elif pending:
        rep.note("待补清单：" + "、".join(f"{m['博主']}/{m['发布']:%m-%d %H:%M}" for m in pending[:8])
                 + "（得到大脑转写有延迟，下一趟自动补，不用管）")

    # 去重和打标完整性
    ids = [r["内容ID"] for r in rows if r["内容ID"]]
    dup = len(ids) - len(set(ids))
    rep.add(OK if dup == 0 else BAD, "去重", f"重复内容ID {dup} 条", "0 条")
    untagged = sum(1 for r in rows if r["推荐等级"] in ("", "待打标"))
    rep.add(OK if untagged == 0 else WARN, "AI 打标", f"待打标 {untagged} 行", "0 行")
    if untagged:
        rep.note("「待打标」是 DeepSeek 那次没调通留下的，不影响数据完整，多半是欠费。")


def heal(rep: Report, fs: Feishu) -> None:
    """今天云端一趟都没成功跑过，就地补跑一趟。这是「无人值守」最后那道保险。"""
    if not os.environ.get("DEEPSEEK_API_KEY"):
        rep.note("本该就地补跑，但缺 DEEPSEEK_API_KEY，跳过。")
        return
    rep.note("⚡ 今天一趟都没成功跑过，正在就地补跑。")
    try:
        radar.run(fs)
        rep.note("⚡ 补跑完成，数据已经补上。")
    except Exception as e:
        rep.add(BAD, "自愈补跑", f"失败：{str(e)[:90]}", "补跑成功")


def backup(fs: Feishu, outdir: str, now: datetime) -> str:
    """把整个多维表格导成一份 JSON 存本地。表在别人家，自己手里得有底。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    dump: dict[str, list] = {}
    for t in fs.call("GET", f"/bitable/v1/apps/{radar.BASE_TOKEN}/tables",
                     params={"page_size": 100}).get("items", []):
        rows, params = [], {"page_size": 500, "automatic_fields": "true"}
        while True:
            data = fs.call("GET", f"/bitable/v1/apps/{radar.BASE_TOKEN}/tables/{t['table_id']}/records",
                           params=params)
            rows += data.get("items") or []
            if not data.get("has_more"):
                break
            params["page_token"] = data["page_token"]
        dump[t["name"]] = rows
    path = out / f"内容雷达备份-{now:%Y%m%d}.json"
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def main() -> int:
    argv = sys.argv[1:]
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else radar.WINDOW_DAYS
    now = datetime.now(CST)
    rep = Report()

    env = load_keys()
    fs = Feishu(env["XIAOK_APP_ID"], env["XIAOK_APP_SECRET"])
    rep.add(OK, "小K 权限", "拿到令牌、能读表", "能读写飞书表")

    ran_today = check_schedule(rep, now)
    if "--heal" in argv and ran_today is False:
        heal(rep, fs)
    source = check_source(rep, now, days)
    check_tables(rep, fs, now, source, days)

    print(f"\n内容雷达巡检 · {now:%Y-%m-%d %H:%M}\n")
    print(rep.text())
    print()

    if "--backup" in argv:
        rep.note(f"已备份：{backup(fs, argv[argv.index('--backup') + 1], now)}")
        print(rep.notes[-1])

    if "--notify" in argv and rep.failed:
        fs.send(f"🔧 内容雷达巡检不合格 · {now:%-m月%-d日}",
                rep.markdown() + "\n\n把这条转给 WorkBuddy，让它按 content-radar 技能排查。")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
