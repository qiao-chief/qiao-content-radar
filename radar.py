#!/usr/bin/env python3
"""内容雷达：得到大脑订阅的抖音博主 + AIHOT 热点 → DeepSeek 打标 → 飞书多维表 → 小K 私信。

固定工序，没有 Agent。GitHub Actions 每天定时跑，本机也能手动跑（见 README.md）。
去重以飞书表为准：订阅日报按「内容ID」，AI热点推荐按「原文链接」，重跑多少次都不会重复写。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE_TOKEN = "UoUlb5rcca4QN1s8e5UcY7OVn8f"
BASE_URL = f"https://vcnf6h45v8ij.feishu.cn/base/{BASE_TOKEN}"
TOPIC_ID = "JlWpjOb0"  # 得到大脑知识库「对标博主」
NOTIFY_OPEN_ID = "ou_35260b8ce426b2d39a9e13b63f8da15b"  # 乔帮主在小K 应用下的 open_id
FEISHU = "https://open.feishu.cn/open-apis"
AIHOT_URL = "https://aihot.news/api/v1/items?mode=selected&window=24h"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
WINDOW_DAYS = int(os.environ.get("RADAR_WINDOW_DAYS") or 3)
STALE_HOURS = 48
DRY_RUN = os.environ.get("RADAR_DRY_RUN") == "1"

LEVELS = ["强烈推荐", "推荐", "一般", "待打标"]
REASONS = ["目标受众关注", "有价值利他", "热点话题"]
FAILED_TRANSCRIPT = re.compile(r"仅包含|无其他具体信息|缺乏明确主题|没有可提取")

SUB_TABLE = "订阅日报"
SUB_FIELDS = [
    {"field_name": "标题", "type": 1},
    {"field_name": "博主", "type": 1},
    {"field_name": "推荐等级", "type": 3, "property": {"options": [{"name": n} for n in LEVELS]}},
    {"field_name": "选题推荐", "type": 1},
    {"field_name": "AI摘要", "type": 1},
    {"field_name": "发布时间", "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
    {"field_name": "链接", "type": 15},
    {"field_name": "转写状态", "type": 3, "property": {"options": [{"name": "已转写"}, {"name": "未转写"}]}},
    {"field_name": "已采用", "type": 7},
    {"field_name": "文字稿", "type": 1},
    {"field_name": "内容ID", "type": 1},
    {"field_name": "入表时间", "type": 1001},
]
HOT_TABLE = "AI热点推荐"
HOT_FIELDS = [
    {"field_name": "选题", "type": 1},
    {"field_name": "状态", "type": 3, "property": {"options": [{"name": n} for n in ["待判断", "有价值利他", "热点话题", "已采用", "放弃"]]}},
    {"field_name": "一句话核心内容", "type": 1},
    {"field_name": "选题理由", "type": 4, "property": {"options": [{"name": n} for n in REASONS]}},
    {"field_name": "切入角度", "type": 1},
    {"field_name": "原文链接", "type": 15},
    {"field_name": "来源", "type": 1},
    {"field_name": "发现时间", "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
    {"field_name": "入表时间", "type": 1001},
]

PROFILE = """你在给「乔帮主」筛选题。
乔帮主：企业管理者出身（做过 HR），不懂代码，靠 Claude Code 等 AI 工具跨过了技术门槛，以「正在突破的同路人」身份做内容。
受众：非技术背景的知识工作者——管理者、HR、培训师、咨询师、内容创作者、想转型的专业人士。
受众的痛点：AI 工具太多不知道学哪个；技术门槛看着高；学了用不到自己工作里；跟着教程能做、自己做就卡住。
乔帮主的价值：帮他们判断什么 AI 工具值得学、怎么组合解决真实问题、怎么把 AI 拉进工作现场。
红线：不贩卖焦虑（不追裁员、被 AI 替代这类恐慌叙事）；不编造经历和数据；不做纯技术炫技。
只输出 JSON。"""

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 国内接口强制直连


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})})
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}：{e.read().decode(errors='ignore')[:300]}") from e


def cell_text(value: object) -> str:
    """飞书单元格读出来可能是字符串、富文本分段列表或链接对象，统一成字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(seg.get("text", "") for seg in value if isinstance(seg, dict))
    if isinstance(value, dict):
        return value.get("link") or value.get("text") or ""
    return ""


class Feishu:
    def __init__(self, app_id: str, app_secret: str) -> None:
        r = http_json("POST", f"{FEISHU}/auth/v3/tenant_access_token/internal",
                      {"app_id": app_id, "app_secret": app_secret})
        if r.get("code") != 0:
            raise RuntimeError(f"小K 拿不到飞书令牌：{r.get('msg')}")
        self.token = r["tenant_access_token"]

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        url = f"{FEISHU}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        r = http_json(method, url, body, {"Authorization": f"Bearer {self.token}"})
        if r.get("code") != 0:
            raise RuntimeError(f"飞书接口 {path} 失败：{r.get('code')} {r.get('msg')}")
        return r.get("data") or {}

    def ensure_table(self, name: str, fields: list[dict]) -> str:
        path = f"/bitable/v1/apps/{BASE_TOKEN}/tables"
        tables = self.call("GET", path, params={"page_size": 100}).get("items", [])
        found = next((t["table_id"] for t in tables if t["name"] == name), None)
        if found:
            return found
        log(f"飞书表「{name}」不存在，新建")
        return self.call("POST", path, {"table": {"name": name, "default_view_name": "全部", "fields": fields}})["table_id"]

    def records(self, table_id: str, fields: list[str]) -> list[dict]:
        """读整张表的指定列，外加每行的创建时间（毫秒）。"""
        out: list[dict] = []
        params = {"page_size": 500, "automatic_fields": "true", "field_names": json.dumps(fields, ensure_ascii=False)}
        while True:
            data = self.call("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records", params=params)
            out += [{"created": it.get("created_time") or 0,
                     **{f: cell_text((it.get("fields") or {}).get(f)) for f in fields}} for it in data.get("items") or []]
            if not data.get("has_more"):
                return out
            params["page_token"] = data["page_token"]

    def create_records(self, table_id: str, rows: list[dict]) -> None:
        for i in range(0, len(rows), 100):
            chunk = [{"fields": r} for r in rows[i:i + 100]]
            self.call("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_create", {"records": chunk})

    def send(self, title: str, md: str) -> None:
        content = {"zh_cn": {"title": title, "content": [[{"tag": "md", "text": md}]]}}
        self.call("POST", "/im/v1/messages", {"receive_id": NOTIFY_OPEN_ID, "msg_type": "post",
                                              "content": json.dumps(content, ensure_ascii=False)},
                  params={"receive_id_type": "open_id"})


# ---------- 得到大脑 ----------

def getnote(*args: str) -> dict:
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    r = subprocess.run(["getnote", *args, "-o", "json"], capture_output=True, text=True, timeout=120, env=env)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        d = {}
    if r.returncode != 0 or not d.get("success"):
        raise RuntimeError(f"得到大脑命令 `{' '.join(args[:2])}` 失败：{(r.stderr or r.stdout).strip()[-300:]}")
    return d.get("data") or {}


@dataclass
class Post:
    id: str
    blogger: str
    title: str
    summary: str
    published: datetime
    url: str
    transcript: str

    @property
    def transcribed(self) -> bool:
        return len(self.transcript) >= 30 and not FAILED_TRANSCRIPT.search(self.summary)


def parse_cst(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)


def clean_title(caption: str) -> str:
    """抖音没有独立标题，文案开头第一段通常就是标题；太短时退回截断的整段文案。"""
    text = re.sub(r"#\S+", "", caption).strip() or caption.strip()
    head = re.split(r"\s+", text, maxsplit=1)[0]
    return head if len(head) >= 6 else text[:50]


def recent_items(follow_id: str, since: datetime) -> list[dict]:
    items: list[dict] = []
    for page in range(1, 4):
        data = getnote("kb", "blogger-contents", TOPIC_ID, follow_id, "--page", str(page))
        batch = data.get("contents") or []
        items += batch
        if not data.get("has_more") or not batch or parse_cst(batch[-1]["post_publish_time"]) < since:
            break
    return items


def load_post(blogger: str, item: dict) -> Post:
    data = getnote("kb", "blogger-content", TOPIC_ID, item["post_id_alias"])
    detail = data["content"] if isinstance(data.get("content"), dict) else data
    return Post(
        id=item["post_id_alias"],
        blogger=blogger,
        title=clean_title(detail.get("post_name") or item.get("post_title") or ""),
        summary=(detail.get("post_summary") or item.get("post_summary") or "").strip(),
        published=parse_cst(item["post_publish_time"]),
        url=(detail.get("post_url") or "").split("?")[0],
        transcript=(detail.get("post_media_text") or "").strip(),
    )


def fetch_new_posts(existing_ids: set[str], since: datetime) -> tuple[list[Post], list[str], datetime | None]:
    posts: list[Post] = []
    warnings: list[str] = []
    times: list[datetime] = []
    for b in getnote("kb", "bloggers", TOPIC_ID).get("bloggers") or []:
        name = b.get("account_name") or b["follow_id_str"]
        if b.get("hook_state") != "READY":
            warnings.append(f"博主「{name}」订阅状态是 {b.get('hook_state')}，不是正常的 READY")
        items = recent_items(b["follow_id_str"], since)
        times += [parse_cst(i["post_publish_time"]) for i in items]
        fresh = [i for i in items if parse_cst(i["post_publish_time"]) >= since and i["post_id_alias"] not in existing_ids]
        posts += [load_post(name, i) for i in fresh]
    return sorted(posts, key=lambda p: p.published, reverse=True), warnings, max(times, default=None)


# ---------- AIHOT ----------

def fetch_aihot() -> list[dict]:
    r = http_json("GET", AIHOT_URL, headers={"User-Agent": "qiao-content-radar/1.0"}, timeout=30)
    return r.get("items") or []


# ---------- DeepSeek ----------

def deepseek(prompt: str) -> dict:
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": PROFILE}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 6000,
        "thinking": {"type": "disabled"},
    }
    r = http_json("POST", DEEPSEEK_URL, body, {"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"}, timeout=180)
    return json.loads(r["choices"][0]["message"]["content"])


def tag_posts(posts: list[Post]) -> dict[str, dict]:
    if not posts:
        return {}
    brief = [{"id": p.id, "博主": p.blogger, "标题": p.title,
              "摘要": p.summary[:300] if p.transcribed else "（视频未转写，只有标题）"} for p in posts]
    prompt = f"""下面是对标博主昨天前后发的短视频。逐条判断：
- 推荐等级：只能是「强烈推荐」「推荐」「一般」之一。
  强烈推荐 = 直击受众痛点，且乔帮主能用自己的实操讲（AI 工具落地、工作流、管理和 HR 场景），有时效或反常识。强烈推荐最多占三成。
  推荐 = 相关，能转译成乔帮主的内容。
  一般 = 纯技术细节、娱乐、和受众无关，或只能做成焦虑叙事。
- 选题推荐：一句话（不超过 40 字），说乔帮主可以从什么角度把它做成自己的内容。不要复述原视频。
输出格式：{{"items": [{{"id": "...", "推荐等级": "...", "选题推荐": "..."}}]}}
视频列表：
{json.dumps(brief, ensure_ascii=False)}"""
    return {it["id"]: it for it in deepseek(prompt).get("items", []) if isinstance(it, dict) and it.get("id")}


def pick_hot(items: list[dict], recent_topics: list[str]) -> list[dict]:
    if not items:
        return []
    brief = [{"idx": i, "标题": it.get("title", ""), "摘要": (it.get("summary") or "")[:200]} for i, it in enumerate(items)]
    prompt = f"""下面是今天的 AI 热点。挑出最值得乔帮主做的，最多 6 条；同一件事只挑一条；纯跑分、纯融资新闻，除非对非技术受众有实用角度，否则不挑。
这些话题最近 3 天已经推荐过，同一件事不要再挑：{json.dumps(recent_topics, ensure_ascii=False)}
每条给出：
- 选题：不超过 20 字
- 一句话核心内容：不超过 50 字，讲清发生了什么
- 选题理由：从「目标受众关注」「有价值利他」「热点话题」里选 1 到 3 个
- 切入角度：不超过 40 字，乔帮主可以怎么讲
输出格式：{{"items": [{{"idx": 0, "选题": "...", "一句话核心内容": "...", "选题理由": ["..."], "切入角度": "..."}}]}}
热点列表：
{json.dumps(brief, ensure_ascii=False)}"""
    picks = []
    for p in deepseek(prompt).get("items", []):
        idx = p.get("idx")
        if isinstance(idx, int) and 0 <= idx < len(items):
            picks.append({**p, "_item": items[idx]})
    return picks


# ---------- 组装与通知 ----------

def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def sub_row(p: Post, tag: dict) -> dict:
    level = tag.get("推荐等级") if tag.get("推荐等级") in LEVELS[:3] else "待打标"
    return {
        "标题": p.title,
        "博主": p.blogger,
        "推荐等级": level,
        "选题推荐": tag.get("选题推荐") or "",
        "AI摘要": p.summary if p.transcribed else "（得到大脑没转写出来，只有标题）",
        "发布时间": ms(p.published),
        "链接": {"link": p.url, "text": "看原视频"} if p.url else None,
        "转写状态": "已转写" if p.transcribed else "未转写",
        "文字稿": p.transcript[:20000] if p.transcribed else "",
        "内容ID": p.id,
    }


def hot_row(pick: dict) -> dict:
    item = pick["_item"]
    link = (item.get("links") or {}).get("original") or (item.get("links") or {}).get("aihot") or ""
    published = item.get("publishedAt") or item.get("discoveredAt")
    return {
        "选题": pick.get("选题") or item.get("title", ""),
        "状态": "待判断",
        "一句话核心内容": pick.get("一句话核心内容") or "",
        "选题理由": [r for r in pick.get("选题理由") or [] if r in REASONS],
        "切入角度": pick.get("切入角度") or "",
        "原文链接": {"link": link, "text": (item.get("source") or {}).get("name") or "原文"},
        "来源": "AIHOT",
        "发现时间": ms(datetime.fromisoformat(published.replace("Z", "+00:00"))) if published else None,
    }


def item_link(item: dict) -> str:
    links = item.get("links") or {}
    return links.get("original") or links.get("aihot") or ""


def fresh_hots(existing: list[dict], now: datetime) -> list[dict]:
    """热点一天只挑一次；挑之前去掉已入表的链接，并把近 3 天的话题告诉 AI 避免同一件事重复。"""
    today = ms(now.replace(hour=0, minute=0, second=0, microsecond=0))
    if any(r["created"] >= today for r in existing):
        log("今天已经推荐过热点，跳过")
        return []
    seen = {r["原文链接"] for r in existing}
    recent = [r["选题"] for r in existing if r["created"] >= today - 3 * 86400_000]
    return pick_hot([it for it in fetch_aihot() if item_link(it) not in seen], recent)


def build_message(rows: list[dict], hots: list[dict], warnings: list[str], sub_tid: str, hot_tid: str) -> str:
    strong = [r for r in rows if r["推荐等级"] == "强烈推荐"]
    lines = [f"**订阅日报**：新增 {len(rows)} 条，强烈推荐 {len(strong)} 条"]
    lines += [f"- 🔥 {r['博主']}｜{r['标题'][:30]}" for r in strong[:5]]
    lines += [f"**AI 热点**：新增 {len(hots)} 条待判断"]
    lines += [f"- {h.get('选题', '')}" for h in hots[:3]]
    lines += ["", f"👉 [打开订阅日报]({BASE_URL}?table={sub_tid})　[打开 AI 热点]({BASE_URL}?table={hot_tid})"]
    lines += [""] + [f"⚠️ {w}" for w in warnings]
    return "\n".join(lines)


def run_url() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{run_id}" if run_id else "本机运行"


def run(fs: Feishu) -> None:
    now = datetime.now(CST)
    sub_tid = fs.ensure_table(SUB_TABLE, SUB_FIELDS)
    hot_tid = fs.ensure_table(HOT_TABLE, HOT_FIELDS)

    existing_ids = {r["内容ID"] for r in fs.records(sub_tid, ["内容ID"])}
    posts, warnings, latest = fetch_new_posts(existing_ids, now - timedelta(days=WINDOW_DAYS))
    log(f"对标博主新视频 {len(posts)} 条")
    if latest is None or now - latest > timedelta(hours=STALE_HOURS):
        warnings.append(f"所有博主已超过 {STALE_HOURS} 小时没有新视频，得到大脑可能断流（会员到期？）")

    try:
        tags = tag_posts(posts)
    except Exception as e:  # 打标失败不丢数据，行照写、等级标「待打标」
        tags = {}
        warnings.append(f"AI 打标失败，本批标成「待打标」：{e}")

    try:
        hots = fresh_hots(fs.records(hot_tid, ["选题", "原文链接"]), now)
    except Exception as e:
        hots = []
        warnings.append(f"AIHOT 热点这次没拿到：{e}")
    log(f"AI 热点入选 {len(hots)} 条；提醒 {len(warnings)} 条")

    rows = [{k: v for k, v in sub_row(p, tags.get(p.id, {})).items() if v is not None} for p in posts]
    hot_rows = [{k: v for k, v in hot_row(h).items() if v is not None} for h in hots]
    if DRY_RUN:
        log("[试跑] 不写表、不发消息。预览：")
        print(json.dumps({"订阅日报": rows[:3], "AI热点推荐": hot_rows[:3], "提醒": warnings}, ensure_ascii=False, indent=2)[:4000])
        return
    fs.create_records(sub_tid, rows)
    fs.create_records(hot_tid, hot_rows)
    strong = [r for r in rows if r["推荐等级"] == "强烈推荐"]
    if strong or warnings:
        fs.send(f"🎯 内容雷达 · {now:%-m月%-d日}", build_message(rows, hots, warnings, sub_tid, hot_tid))
    else:
        log(f"无强烈推荐、无警告，不发私信。新增 {len(rows)} 条日报、{len(hots)} 条热点")
    log("完成")


def main() -> int:
    missing = [k for k in ("XIAOK_APP_ID", "XIAOK_APP_SECRET", "DEEPSEEK_API_KEY") if not os.environ.get(k)]
    if missing:
        log(f"缺少环境变量：{', '.join(missing)}")
        return 1
    fs = Feishu(os.environ["XIAOK_APP_ID"], os.environ["XIAOK_APP_SECRET"])
    try:
        run(fs)
        return 0
    except Exception as e:
        log(f"失败：{e}")
        if not DRY_RUN:
            fs.send("❌ 内容雷达运行失败", f"{e}\n\n运行记录：{run_url()}\n\n把这条消息转给 WorkBuddy 或 Claude，让它按 README 排查。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
