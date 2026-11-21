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
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

CST = timezone(timedelta(hours=8))
BASE_TOKEN = "UoUlb5rcca4QN1s8e5UcY7OVn8f"
BASE_URL = f"https://vcnf6h45v8ij.feishu.cn/base/{BASE_TOKEN}"
TOPIC_ID = "JlWpjOb0"  # 得到大脑知识库「对标博主」
NOTIFY_OPEN_ID = "ou_35260b8ce426b2d39a9e13b63f8da15b"  # 乔帮主在小K 应用下的 open_id
FEISHU = "https://open.feishu.cn/open-apis"
AIHOT_URL = "https://aihot.news/api/v1/items?mode=selected&window=24h"
OVERSEAS_RSS = [
    ("One Useful Thing", "https://www.oneusefulthing.org/feed"),
    ("Every", "https://every.to/feeds/rss.xml"),
    ("Interconnects", "https://www.interconnects.ai/feed"),
    ("Latent Space", "https://www.latent.space/feed"),
    ("Stratechery", "https://stratechery.com/feed/"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("Ben's Bites", "https://www.bensbites.com/feed"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
]
MACRO_RSS = [
    ("Project Syndicate", "https://www.project-syndicate.org/rss"),
    ("Calculated Risk", "https://www.calculatedriskblog.com/feeds/posts/default"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
]
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
WINDOW_DAYS = int(os.environ.get("RADAR_WINDOW_DAYS") or 3)
STALE_HOURS = 48
GETNOTE_RETRIES = 3  # 跨境调用偶发 TLS 握手超时，重试三次
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
    """飞书单元格读出来可能是字符串、数字、富文本分段列表或链接对象，统一成字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(int(value))
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

    def send_card(self, title: str, elements: list[dict], template: str = "indigo") -> None:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        }
        self.call("POST", "/im/v1/messages", {"receive_id": NOTIFY_OPEN_ID, "msg_type": "interactive",
                                              "content": json.dumps(card, ensure_ascii=False)},
                  params={"receive_id_type": "open_id"})


# ---------- 得到大脑 ----------

def getnote(*args: str) -> dict:
    """得到大脑的服务器在国内，GitHub 的机器在国外，偶发 TLS 握手超时。这里全是读操作，重试安全。"""
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    last = ""
    for attempt in range(1, GETNOTE_RETRIES + 1):
        r = subprocess.run(["getnote", *args, "-o", "json"], capture_output=True, text=True, timeout=120, env=env)
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            d = {}
        if r.returncode == 0 and d.get("success"):
            return d.get("data") or {}
        last = (r.stderr or r.stdout).strip()[-300:]
        if attempt < GETNOTE_RETRIES:
            time.sleep(5 * attempt)
    raise RuntimeError(f"得到大脑命令 `{' '.join(args[:2])}` 连试 {GETNOTE_RETRIES} 次都失败：{last}")


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
        # 单个博主拉不动就跳过它，不要把整趟拖死——去重靠内容ID，窗口有 3 天，下一趟会自动补上
        try:
            items = recent_items(b["follow_id_str"], since)
        except Exception as e:
            warnings.append(f"博主「{name}」这次没拉到，已跳过，下一趟会补：{str(e)[:120]}")
            continue
        times += [parse_cst(i["post_publish_time"]) for i in items]
        fresh = [i for i in items if parse_cst(i["post_publish_time"]) >= since and i["post_id_alias"] not in existing_ids]
        for i in fresh:
            try:
                posts.append(load_post(name, i))
            except Exception as e:
                warnings.append(f"博主「{name}」有一条正文没取到，已跳过，下一趟会补：{str(e)[:120]}")
    return sorted(posts, key=lambda p: p.published, reverse=True), warnings, max(times, default=None)


# ---------- AIHOT ----------

def fetch_aihot() -> list[dict]:
    r = http_json("GET", AIHOT_URL, headers={"User-Agent": "qiao-content-radar/1.0"}, timeout=30)
    items = r.get("items") or []
    for it in items:
        it.setdefault("_source_tag", "AIHOT")
    return items


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").replace("\n", " ").strip()


def _parse_rss_date(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fetch_rss_list(feeds: list[tuple[str, str]], prefix: str, since_hours: int = 72) -> list[dict]:
    """通用 RSS/Atom 抓取器（兼容 RSS 2.0 和 Atom）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    results: list[dict] = []
    for name, url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; qiao-content-radar/1.0)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            atom_ns = "{http://www.w3.org/2005/Atom}"
            entries = root.findall(f".//{atom_ns}entry")
            if entries:
                for entry in entries[:8]:
                    title = (entry.findtext(f"{atom_ns}title") or "").strip()
                    link_el = entry.find(f"{atom_ns}link[@rel='alternate']")
                    if link_el is None:
                        link_el = entry.find(f"{atom_ns}link")
                    link = (link_el.attrib.get("href") if link_el is not None else "") or ""
                    pub_raw = entry.findtext(f"{atom_ns}published") or entry.findtext(f"{atom_ns}updated") or ""
                    dt = _parse_rss_date(pub_raw)
                    if dt and dt < cutoff:
                        continue
                    summary = _strip_html(entry.findtext(f"{atom_ns}summary") or entry.findtext(f"{atom_ns}content") or "")
                    if title and link:
                        results.append({
                            "title": title,
                            "summary": summary[:450],
                            "links": {"original": link},
                            "source": {"name": f"{prefix}{name}"},
                            "_source_tag": f"{prefix}{name}",
                            "publishedAt": dt.isoformat() if dt else "",
                        })
            else:
                for item in root.findall(".//item")[:8]:
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    pub_raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
                    dt = _parse_rss_date(pub_raw)
                    if dt and dt < cutoff:
                        continue
                    summary = _strip_html(item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
                    if title and link:
                        results.append({
                            "title": title,
                            "summary": summary[:450],
                            "links": {"original": link},
                            "source": {"name": f"{prefix}{name}"},
                            "_source_tag": f"{prefix}{name}",
                            "publishedAt": dt.isoformat() if dt else "",
                        })
        except Exception as e:
            log(f"RSS抓取跳过（{name}）：{e}")
    return results


def fetch_overseas_rss(since_hours: int = 72) -> list[dict]:
    items = _fetch_rss_list(OVERSEAS_RSS, "海外·", since_hours)
    log(f"海外一手RSS抓到近 {since_hours}h 文章 {len(items)} 篇")
    return items


def fetch_macro_metrics() -> list[dict]:
    """抓取 3 个核心资金锚点：10年期美债收益率、离岸人民币汇率、上证指数。"""
    targets = [
        ("^TNX", "10Y美债", "%"),
        ("CNH=X", "离岸人民币", ""),
        ("000001.SS", "上证指数", ""),
    ]
    out: list[dict] = []
    for sym, label, unit in targets:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range=2d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose")
            if price is not None:
                diff_pct = ((price - prev) / prev * 100) if prev else 0.0
                sign = "+" if diff_pct >= 0 else ""
                val_str = f"{price:.2f}{unit}" if unit == "%" else (f"{price:.4f}" if sym == "CNH=X" else f"{price:.2f}")
                out.append({"label": label, "value": val_str, "change": f"{sign}{diff_pct:.2f}%"})
        except Exception as e:
            log(f"宏观指标获取跳过（{label}）：{e}")
    return out


def fetch_macro_opinions(since_hours: int = 72) -> list[dict]:
    """抓取全球经济学家/宏观分析师评论并请 DeepSeek 挑出 2 条翻译提炼。"""
    items = _fetch_rss_list(MACRO_RSS, "", since_hours)
    if not items:
        return []
    brief = [{"idx": i, "来源": it["_source_tag"], "标题": it["title"], "摘要": it["summary"][:300]} for i, it in enumerate(items[:18])]
    prompt = f"""你是给非金融专业出身的企业主/个人投资者做宏观内参的财经助理。
请从下面的全球经济学家/宏观分析师文章中，挑出最有助于理解「美联储利率/美元流向/中美经济/大类资产动荡」的 **2 篇**深度评论或数据解读。
要求：
- 标题：中文，不超过 24 字，点明核心判断
- 摘要：中文大白话（80~110字），讲清专家的核心观点、逻辑和对中国市场/资金流向的启示，禁止晦涩金融黑话
输出格式：{{"items": [{{"idx": 0, "标题": "...", "摘要": "..."}}]}}
文章列表：{json.dumps(brief, ensure_ascii=False)}"""
    try:
        picks = []
        for p in deepseek(prompt).get("items", [])[:2]:
            idx = p.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(items):
                it = items[idx]
                picks.append({
                    "source": it["_source_tag"],
                    "title": p.get("标题") or it["title"],
                    "summary": p.get("摘要") or "",
                    "url": (it.get("links") or {}).get("original", ""),
                    "publishedAt": it.get("publishedAt", ""),
                })
        return picks
    except Exception as e:
        log(f"宏观专家评论提炼跳过：{e}")
        return []


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
    # 判据只看料本身，不套人设。旧版写死了「管理和 HR 场景」，结果 15% 的选题推荐被拧成
    # 「以HR身份实测…」「用HR转型经历讲…」——那是替人编经历，也是老系统 32% 人设套壳的复发。
    # 「我能怎么用」永远由乔帮主自己填，机器只负责把料讲清楚。
    prompt = f"""下面是对标博主昨天前后发的短视频。逐条判断：
- 推荐等级：只能是「强烈推荐」「推荐」「一般」之一。判断只看这条料本身，不要考虑发布者是谁、适合谁讲。
  强烈推荐 = 讲的是非技术人也用得上的 AI 落地方法、工作流、职场或生活场景的真实用法，或有时效、有反常识的判断。强烈推荐最多占三成。
  推荐 = 有可用信息，但不够新或不够具体。
  一般 = 纯技术细节、纯娱乐、纯跑分融资、或只能做成焦虑叙事。
- 选题推荐：一句话（不超过 40 字），**客观说清这条料最有价值的那个点是什么**。
  只许压缩原内容，严禁替任何人设计角度、编造身份或经历。
  禁止出现「乔帮主」「以HR身份」「用自己的经历」这类字样，也不要写「可以从…角度讲」。
输出格式：{{"items": [{{"id": "...", "推荐等级": "...", "选题推荐": "..."}}]}}
视频列表：
{json.dumps(brief, ensure_ascii=False)}"""
    return {it["id"]: it for it in deepseek(prompt).get("items", []) if isinstance(it, dict) and it.get("id")}


def pick_hot(items: list[dict], recent_topics: list[str]) -> list[dict]:
    if not items:
        return []
    brief = [{"idx": i, "来源": it.get("_source_tag", "AIHOT"), "标题": it.get("title", ""), "摘要": (it.get("summary") or "")[:250]} for i, it in enumerate(items)]
    # 同上：判断只看热点本身对受众有没有用，不替乔帮主设计角度、编身份。
    prompt = f"""下面是今天的海内外 AI 精选资讯与一手文章（包含海外英文一手源和国内热点）。挑出最值得关注的，最多 8 条（**优先挑选「海外·xxx」的一手深度文章，每条英文内容必须翻译成自然地道的简体中文**）；同一件事只挑一条；纯跑分、纯融资新闻，除非对非技术受众有实用角度，否则不挑。
这些话题最近 3 天已经推荐过，同一件事不要再挑：{json.dumps(recent_topics, ensure_ascii=False)}
每条给出：
- 选题：中文标题，不超过 24 字
- 一句话核心内容：中文精华摘要（80~120字），用两三句大白话讲清文章背景、核心观点/做法及关键数据，让读者不点原文也能吸收八成干货
- 选题理由：从「目标受众关注」「有价值利他」「热点话题」里选 1 到 3 个
- 切入角度：中文，不超过 45 字，**客观说清这条料对受众最有用的那个点**，不是"乔帮主可以怎么讲"。
  严禁替任何人设计角度、编造身份或经历，禁止出现「乔帮主」「以HR身份」「用自己的经历」这类字样。
输出格式：{{"items": [{{"idx": 0, "选题": "...", "一句话核心内容": "...", "选题理由": ["..."], "切入角度": "..."}}]}}
候选列表：
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
        "来源": item.get("_source_tag") or (item.get("source") or {}).get("name") or "AIHOT",
        "发现时间": ms(datetime.fromisoformat(published.replace("Z", "+00:00"))) if published else None,
    }


def item_link(item: dict) -> str:
    links = item.get("links") or {}
    return links.get("original") or links.get("aihot") or ""


def fresh_hots(existing: list[dict], now: datetime) -> list[dict]:
    """每天各挑一次；用原文链接去重，分别检查 AIHOT 和 海外RSS 今天是否已入表，互不阻塞。"""
    today = ms(now.replace(hour=0, minute=0, second=0, microsecond=0))
    seen = {r["原文链接"] for r in existing}
    recent = [r["选题"] for r in existing if r["created"] >= today - 3 * 86400_000]
    has_overseas_today = any(r["created"] >= today and str(r.get("来源", "")).startswith("海外·") for r in existing)
    has_aihot_today = any(r["created"] >= today and str(r.get("来源", "")) == "AIHOT" for r in existing)

    candidates: list[dict] = []
    if not has_overseas_today:
        candidates.extend(fetch_overseas_rss())
    if not has_aihot_today:
        candidates.extend(fetch_aihot())
    if not candidates:
        log("今天海外RSS与热点均已推荐过，跳过")
        return []
    return pick_hot([it for it in candidates if item_link(it) not in seen], recent)


def _fmt_ts(val: object) -> str:
    try:
        num = float(val) if isinstance(val, (int, float, str)) and str(val).strip() else 0
        if num > 0:
            return datetime.fromtimestamp(num / 1000, CST).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        pass
    return ""


def _extract_url(val: object) -> str:
    if isinstance(val, dict):
        return val.get("link", "")
    if isinstance(val, str) and val.startswith("http"):
        return val
    return ""


def build_card_elements(
    rows: list[dict],
    hot_rows: list[dict],
    warnings: list[str],
    sub_tid: str,
    hot_tid: str,
    macro_metrics: list[dict] | None = None,
    macro_opinions: list[dict] | None = None,
) -> list[dict]:
    elements: list[dict] = []

    # 1. 海外一手洞察
    if hot_rows:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🔵 海外一手洞察**　<font color='grey'>外网原汁原味 · 中文精华精翻</font>"}})
        elements.append({"tag": "hr"})
        sorted_hots = sorted(hot_rows, key=lambda h: 0 if str(h.get("来源", "")).startswith("海外·") else 1)
        for idx, hr in enumerate(sorted_hots[:6], 1):
            src = str(hr.get("来源", "")).replace("海外·", "")
            title = hr.get("选题", "")
            core = hr.get("一句话核心内容", "")
            pub_t = _fmt_ts(hr.get("发现时间"))
            meta = f"<font color='blue'>{src}</font>　<font color='grey'>{pub_t}</font>" if pub_t else f"<font color='blue'>{src}</font>"
            url = _extract_url(hr.get("原文链接"))
            link_md = f"　[阅读原文 ↗]({url})" if url else ""

            block = f"{meta}\n**{idx}. {title}**{link_md}"
            if core:
                block += f"\n<font color='grey'>{core}</font>"
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
            if idx < min(len(sorted_hots), 6):
                elements.append({"tag": "hr"})

    # 2. 关注博主更新（得到大脑）
    valid_rows = [
        r for r in rows
        if r.get("转写状态") != "未转写" and "仅有标题" not in (r.get("选题推荐") or "")
    ]
    if valid_rows:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🟠 关注博主动态**　<font color='grey'>得到大脑自动转写</font>"}})
        elements.append({"tag": "hr"})
        sorted_rows = sorted(
            valid_rows,
            key=lambda r: (0 if r.get("推荐等级") == "强烈推荐" else 1, -int(float(r.get("发布时间") or 0))),
        )
        for idx, r in enumerate(sorted_rows[:5], 1):
            is_strong = r.get("推荐等级") == "强烈推荐"
            mark = "<font color='red'>🔥 强烈推荐</font>　" if is_strong else ""
            blogger = r.get("博主", "")
            title = (r.get("标题") or "")[:36]
            rec = r.get("选题推荐") or r.get("AI摘要") or ""
            pub_t = _fmt_ts(r.get("发布时间"))
            meta = f"{mark}<font color='orange'>{blogger}</font>　<font color='grey'>{pub_t}</font>" if pub_t else f"{mark}<font color='orange'>{blogger}</font>"
            url = _extract_url(r.get("链接"))
            link_md = f"　[看视频 ↗]({url})" if url else ""

            block = f"{meta}\n**{idx}. {title}**{link_md}"
            if rec:
                block += f"\n<font color='grey'>{rec[:140]}</font>"
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
            if idx < min(len(sorted_rows), 5):
                elements.append({"tag": "hr"})

    # 3. 宏观资金风向与经济学家视点
    if macro_metrics or macro_opinions:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🟢 财经风向与专家视点**　<font color='grey'>全球流动性 · 宏观专家拆解</font>"}})
        if macro_metrics:
            metrics_str = " ｜ ".join(f"**{m['label']}** `{m['value']}` ({m['change']})" for m in macro_metrics)
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": metrics_str}})
        if macro_opinions:
            elements.append({"tag": "hr"})
            for idx, op in enumerate(macro_opinions, 1):
                pub_t = op.get("publishedAt", "")[:10]
                meta = f"<font color='green'>{op['source']}</font>　<font color='grey'>{pub_t}</font>" if pub_t else f"<font color='green'>{op['source']}</font>"
                url = op.get("url", "")
                link_md = f"　[看原文 ↗]({url})" if url else ""
                block = f"{meta}\n**{idx}. {op['title']}**{link_md}\n<font color='grey'>{op['summary']}</font>"
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
                if idx < len(macro_opinions):
                    elements.append({"tag": "hr"})

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📂 订阅总表（看文字稿）"},
                "type": "default",
                "url": f"{BASE_URL}?table={sub_tid}",
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🌍 海外热点档案库"},
                "type": "primary",
                "url": f"{BASE_URL}?table={hot_tid}",
            },
        ],
    })
    if warnings:
        warn_text = "\n".join(f"⚠️ {w}" for w in warnings)
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": warn_text}]})
    return elements


def build_message(rows: list[dict], hots: list[dict], hot_rows: list[dict], warnings: list[str], sub_tid: str, hot_tid: str) -> str:
    lines: list[str] = []

    # 1. 海外一手精选（优先把「海外·」排在最前面展示）
    if hot_rows:
        lines.append("**🌍 海外一手与热点精选**")
        sorted_hots = sorted(hot_rows, key=lambda h: 0 if str(h.get("来源", "")).startswith("海外·") else 1)
        for idx, hr in enumerate(sorted_hots[:6], 1):
            src = hr.get("来源", "")
            title = hr.get("选题", "")
            core = hr.get("一句话核心内容", "")
            pub_t = _fmt_ts(hr.get("发现时间"))
            t_tag = f"`{pub_t}` " if pub_t else ""
            url = _extract_url(hr.get("原文链接"))
            link_md = f" 👉 [看原文]({url})" if url else ""
            lines.append(f"**【{idx}】** {t_tag}**[{src}] {title}**{link_md}")
            if core:
                lines.append(f"　　{core}")
        lines.append("")

    # 2. 得到大脑关注的国内博主动态（过滤掉“未转写/仅有标题”的废行，优先强烈推荐）
    valid_rows = [
        r for r in rows
        if r.get("转写状态") != "未转写" and "仅有标题" not in (r.get("选题推荐") or "")
    ]
    if valid_rows:
        lines.append("---")
        lines.append("**📱 关注博主更新（得到大脑）**")
        sorted_rows = sorted(
            valid_rows,
            key=lambda r: (0 if r.get("推荐等级") == "强烈推荐" else 1, -int(float(r.get("发布时间") or 0))),
        )
        for idx, r in enumerate(sorted_rows[:5], 1):
            mark = "🔥 " if r.get("推荐等级") == "强烈推荐" else ""
            blogger = r.get("博主", "")
            title = (r.get("标题") or "")[:36]
            rec = r.get("选题推荐") or ""
            pub_t = _fmt_ts(r.get("发布时间"))
            t_tag = f"`{pub_t}` " if pub_t else ""
            url = _extract_url(r.get("链接"))
            link_md = f" 👉 [看视频]({url})" if url else ""
            lines.append(f"**【{idx}】** {mark}{t_tag}**{blogger}｜{title}**{link_md}")
            if rec:
                lines.append(f"　　{rec}")
        lines.append("")

    lines.append("---")
    lines.append(f"📂 [订阅总表]({BASE_URL}?table={sub_tid}) ｜ [海外热点表]({BASE_URL}?table={hot_tid})")
    if warnings:
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
        hots = fresh_hots(fs.records(hot_tid, ["选题", "原文链接", "来源"]), now)
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
    if rows or hot_rows or warnings:
        macro_metrics = fetch_macro_metrics()
        macro_opinions = fetch_macro_opinions()
        elements = build_card_elements(rows, hot_rows, warnings, sub_tid, hot_tid, macro_metrics, macro_opinions)
        fs.send_card(f"☀️ 乔帮主高管早报 · {now:%-m月%-d日}", elements)
    else:
        log("本次无新增内容且无警告，跳过发信")
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
