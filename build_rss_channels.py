#!/usr/bin/env python3
"""生成乔帮主 AI RSS 三大独立频道（一条一篇、中文预翻译 + 原文/完整逐字稿）：
1. docs/feed-overseas.xml  - 🔵 海外深度精翻（8大海外AI博客 + 经济学家评论）
2. docs/feed-x.xml         - 🔴 硅谷 X 一线动态（张咋啦14位掌门人推文）
3. docs/feed-bloggers.xml  - 🟡 得到大脑博主全文（13位国内博主完整逐字稿）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape as html_escape
from pathlib import Path

import radar

CST = timezone(timedelta(hours=8))
DOCS_DIR = Path(__file__).resolve().parent / "docs"
CACHE_FILE = DOCS_DIR / "rss_trans_cache.json"


def _load_cache() -> dict[str, dict]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    # 只保留最近 200 条翻译缓存，防止无限膨胀
    if len(cache) > 200:
        keys = list(cache.keys())[-200:]
        cache = {k: cache[k] for k in keys}
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<(p|div|br|li|h[1-6])[^>]*>", "\n", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_full_rss_items(feeds: list[tuple[str, str]], since_hours: int = 120, per_feed: int = 4) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    results: list[dict] = []
    for name, url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; qiao-content-radar/1.0)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                root = ET.fromstring(resp.read())
            atom_ns = "{http://www.w3.org/2005/Atom}"
            content_ns = "{http://purl.org/rss/1.0/modules/content/}"
            entries = root.findall(f".//{atom_ns}entry")
            if entries:
                for entry in entries[:per_feed]:
                    title = (entry.findtext(f"{atom_ns}title") or "").strip()
                    link_el = entry.find(f"{atom_ns}link[@rel='alternate']")
                    if link_el is None:
                        link_el = entry.find(f"{atom_ns}link")
                    link = (link_el.attrib.get("href") if link_el is not None else "") or ""
                    pub_raw = entry.findtext(f"{atom_ns}published") or entry.findtext(f"{atom_ns}updated") or ""
                    dt = radar._parse_rss_date(pub_raw) or datetime.now(timezone.utc)
                    if dt < cutoff:
                        continue
                    body_raw = entry.findtext(f"{atom_ns}content") or entry.findtext(f"{atom_ns}summary") or ""
                    body = _clean_html_text(body_raw)
                    if title and link and body:
                        results.append({"source": name, "title": title, "link": link, "dt": dt, "body": body[:3500]})
            else:
                for item in root.findall(".//item")[:per_feed]:
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    pub_raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
                    dt = radar._parse_rss_date(pub_raw) or datetime.now(timezone.utc)
                    if dt < cutoff:
                        continue
                    body_raw = item.findtext(f"{content_ns}encoded") or item.findtext("description") or ""
                    body = _clean_html_text(body_raw)
                    if title and link and body:
                        results.append({"source": name, "title": title, "link": link, "dt": dt, "body": body[:3500]})
        except Exception as e:
            radar.log(f"[RSS频道] 跳过 {name}: {e}")
    results.sort(key=lambda x: x["dt"], reverse=True)
    return results


def _translate_article(title: str, source: str, body: str) -> dict:
    prompt = f"""你是专业科技与商业翻译主编。请将下面这篇文章翻译成自然、流畅、去AI味的简体中文。
要求：
1. zh_title：翻译出信达雅的中文标题（保留关键英文产品/人名，不超过28字）
2. zh_body：完整分段翻译正文内容（保持原意与细节，用大白话讲清楚，段落之间用 \\n\\n 分隔）
输出严格 JSON：{{"zh_title": "...", "zh_body": "..."}}

来源：{source}
原标题：{title}
正文：
{body[:2800]}"""
    try:
        res = radar.deepseek(prompt)
        return {
            "zh_title": res.get("zh_title") or title,
            "zh_body": res.get("zh_body") or body,
        }
    except Exception as e:
        radar.log(f"[RSS翻译] 失败({title[:20]}): {e}")
        return {"zh_title": title, "zh_body": body}


def _write_xml(filename: str, ch_title: str, ch_desc: str, items: list[dict]) -> None:
    xml_items = []
    for it in items:
        xml_items.append(
            f"  <item>\n"
            f"    <title>{html_escape(it['title'])}</title>\n"
            f"    <link>{html_escape(it['link'])}</link>\n"
            f"    <guid isPermaLink=\"false\">{html_escape(it['guid'])}</guid>\n"
            f"    <pubDate>{html_escape(it['pubDate'])}</pubDate>\n"
            f"    <description><![CDATA[{it['html']}]]></description>\n"
            f"  </item>"
        )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '<channel>\n'
        f'  <title>{html_escape(ch_title)}</title>\n'
        '  <link>https://radar.qiao-flow.cn/</link>\n'
        f'  <description>{html_escape(ch_desc)}</description>\n'
        '  <language>zh-CN</language>\n'
        + "\n".join(xml_items) +
        '\n</channel>\n</rss>\n'
    )
    (DOCS_DIR / filename).write_text(rss, encoding="utf-8")
    radar.log(f"已生成 {filename}（共 {len(items)} 条独立文章）")


def _paragraphs_to_html(text: str) -> str:
    paras = [f"<p>{html_escape(p.strip())}</p>" for p in text.split("\n\n") if p.strip()]
    return "\n".join(paras) if paras else f"<p>{html_escape(text)}</p>"


def build_overseas_channel(cache: dict[str, dict]) -> None:
    feeds = radar.OVERSEAS_RSS
    raw_items = _fetch_full_rss_items(feeds, since_hours=120, per_feed=3)[:15]
    out_items = []
    new_trans = 0
    for it in raw_items:
        ckey = "overseas:" + it["link"]
        if ckey not in cache and new_trans < 10:
            tr = _translate_article(it["title"], it["source"], it["body"])
            cache[ckey] = tr
            new_trans += 1
        tr = cache.get(ckey) or {"zh_title": it["title"], "zh_body": it["body"]}
        zh_html = _paragraphs_to_html(tr["zh_body"])
        en_html = _paragraphs_to_html(it["body"][:2500])
        full_html = (
            f"<p><code>{html_escape(it['source'])}</code> · <a href=\"{html_escape(it['link'])}\">查看网页原文 ↗</a></p>\n"
            f"<h2>中文精翻</h2>\n{zh_html}\n"
            f"<hr/><h2>英文原文（Original）</h2>\n<blockquote>{en_html}</blockquote>"
        )
        out_items.append({
            "title": f"[{it['source']}] {tr['zh_title']}",
            "link": it["link"],
            "guid": "overseas-" + hashlib.sha256(it["link"].encode()).hexdigest()[:16],
            "pubDate": format_datetime(it["dt"].astimezone(CST)),
            "html": full_html,
        })
    _write_xml("feed-overseas.xml", "🔵 海外深度精翻", "8大海外AI博客深度文章（中文精翻+原文）", out_items)


def build_macro_channel(cache: dict[str, dict]) -> None:
    now = datetime.now(CST)
    day_key = now.strftime("%Y-%m-%d")
    out_items = []

    # 1. 每日股债金汇盘面卡片置顶
    metrics = radar.fetch_macro_metrics()
    if metrics:
        rows_html = []
        for m in metrics:
            chg = m["change"]
            arrow = "🔴 ▲" if chg.startswith("+") else "🟢 ▼"
            rows_html.append(
                f"<tr><td><strong>{html_escape(m['label'])}</strong></td>"
                f"<td><code>{html_escape(m['value'])}</code></td>"
                f"<td>{arrow} {html_escape(chg)}</td></tr>"
            )
        table_html = (
            "<h2>📊 今日股·债·金·汇核心盘面</h2>\n"
            "<table><thead><tr><th>核心资产</th><th>最新报价</th><th>涨跌幅</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody></table>\n"
            "<blockquote><p>关注核心锚点：10Y美债收益率决定全球流动性松紧；人民币汇率反映外资流向压力；沪深300与10年国债ETF反映国内股债跷跷板。</p></blockquote>"
        )
        out_items.append({
            "title": f"📊 今日股·债·金·汇盘面快照（{now:%-m月%-d日}）",
            "link": f"https://radar.qiao-flow.cn/#macro-{day_key}",
            "guid": f"macro-metrics-{day_key}",
            "pubDate": format_datetime(now),
            "html": table_html,
        })

    # 2. 全球经济学家/宏观分析师深度评论独立成篇
    raw_items = _fetch_full_rss_items(radar.MACRO_RSS, since_hours=120, per_feed=4)[:12]
    new_trans = 0
    for it in raw_items:
        ckey = "macro:" + it["link"]
        if ckey not in cache and new_trans < 10:
            tr = _translate_article(it["title"], it["source"], it["body"])
            cache[ckey] = tr
            new_trans += 1
        tr = cache.get(ckey) or {"zh_title": it["title"], "zh_body": it["body"]}
        zh_html = _paragraphs_to_html(tr["zh_body"])
        en_html = _paragraphs_to_html(it["body"][:2500])
        full_html = (
            f"<p><code>{html_escape(it['source'])}</code> · <a href=\"{html_escape(it['link'])}\">查看网页原文 ↗</a></p>\n"
            f"<h2>经济学家观点精翻</h2>\n{zh_html}\n"
            f"<hr/><h2>英文原文（Original）</h2>\n<blockquote>{en_html}</blockquote>"
        )
        out_items.append({
            "title": f"[{it['source']}] {tr['zh_title']}",
            "link": it["link"],
            "guid": "macro-" + hashlib.sha256(it["link"].encode()).hexdigest()[:16],
            "pubDate": format_datetime(it["dt"].astimezone(CST)),
            "html": full_html,
        })
    _write_xml("feed-macro.xml", "🟢 财经宏观与盘面", "股债金汇核心盘面 + 全球经济学家深度评论（中文精翻+原文）", out_items)


def build_x_channel(cache: dict[str, dict]) -> None:
    try:
        req = urllib.request.Request(radar.FOLLOW_BUILDERS_X_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        radar.log(f"[X频道] 拉取失败: {e}")
        return

    raw_tweets = []
    for builder in data.get("x") or []:
        name = builder.get("name") or builder.get("handle") or ""
        handle = builder.get("handle") or ""
        for tw in builder.get("tweets") or []:
            text = (tw.get("text") or "").strip()
            url = tw.get("url") or (f"https://x.com/{handle}/status/{tw.get('id')}" if tw.get("id") and handle else "")
            created = tw.get("createdAt") or tw.get("date") or ""
            dt = radar._parse_rss_date(created) or datetime.now(timezone.utc)
            if text and url and len(text) > 20:
                raw_tweets.append({"author": name, "handle": handle, "text": text, "url": url, "dt": dt})
    raw_tweets.sort(key=lambda x: x["dt"], reverse=True)
    raw_tweets = raw_tweets[:15]

    out_items = []
    new_trans = 0
    for tw in raw_tweets:
        ckey = "x:" + tw["url"]
        if ckey not in cache and new_trans < 12:
            tr = _translate_article(tw["text"][:60], f"X @{tw['handle']}", tw["text"])
            cache[ckey] = tr
            new_trans += 1
        tr = cache.get(ckey) or {"zh_title": tw["text"][:30], "zh_body": tw["text"]}
        full_html = (
            f"<p><code>@{html_escape(tw['handle'])}</code> · <a href=\"{html_escape(tw['url'])}\">查看 X 原推文 ↗</a></p>\n"
            f"<h2>中文翻译</h2>\n{_paragraphs_to_html(tr['zh_body'])}\n"
            f"<hr/><h2>英文原推文</h2>\n<blockquote>{_paragraphs_to_html(tw['text'])}</blockquote>"
        )
        out_items.append({
            "title": f"[{tw['author']}] {tr['zh_title']}",
            "link": tw["url"],
            "guid": "x-" + hashlib.sha256(tw["url"].encode()).hexdigest()[:16],
            "pubDate": format_datetime(tw["dt"].astimezone(CST)),
            "html": full_html,
        })
    _write_xml("feed-x.xml", "🔴 硅谷 X 一线动态", "14位一线AI掌门人Twitter/X实时动态（中文翻译+原文）", out_items)


def build_bloggers_channel(fs: radar.Feishu) -> None:
    sub_tid = fs.ensure_table(radar.SUB_TABLE, radar.SUB_FIELDS)
    recs = fs.records(sub_tid, ["标题", "博主", "推荐等级", "选题推荐", "AI摘要", "转写状态", "文字稿", "链接", "发布时间"])
    valid = [
        r for r in recs
        if r.get("转写状态") == "已转写" and len(r.get("文字稿") or "") > 80
    ]
    valid.sort(key=lambda r: int(float(r.get("发布时间") or 0)), reverse=True)
    valid = valid[:20]

    out_items = []
    for r in valid:
        blogger = r.get("博主", "")
        title = r.get("标题", "")
        url = radar._extract_url(r.get("链接")) or "https://radar.qiao-flow.cn/"
        ts_ms = int(float(r.get("发布时间") or 0))
        dt = datetime.fromtimestamp(ts_ms / 1000, CST) if ts_ms > 0 else datetime.now(CST)
        summary = r.get("AI摘要") or r.get("选题推荐") or ""
        transcript = r.get("文字稿") or ""
        full_html = (
            f"<p><code>{html_escape(blogger)}</code> · <code>{html_escape(r.get('推荐等级', ''))}</code> · <a href=\"{html_escape(url)}\">看原视频 ↗</a></p>\n"
            f"<h2>AI 核心摘要</h2>\n<blockquote>{_paragraphs_to_html(summary)}</blockquote>\n"
            f"<hr/><h2>完整视频逐字稿</h2>\n{_paragraphs_to_html(transcript)}"
        )
        out_items.append({
            "title": f"[{blogger}] {title}",
            "link": url,
            "guid": "blogger-" + hashlib.sha256((blogger + title + str(ts_ms)).encode()).hexdigest()[:16],
            "pubDate": format_datetime(dt),
            "html": full_html,
        })
    _write_xml("feed-bloggers.xml", "🟡 得到大脑博主全文", "13位对标抖音博主完整逐字稿与摘要", out_items)


def main() -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    cache = _load_cache()
    fs = radar.Feishu(os.environ["XIAOK_APP_ID"], os.environ["XIAOK_APP_SECRET"])
    build_overseas_channel(cache)
    build_x_channel(cache)
    build_bloggers_channel(fs)
    build_macro_channel(cache)
    _save_cache(cache)


if __name__ == "__main__":
    main()
