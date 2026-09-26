#!/usr/bin/env python3
"""内容雷达看板生成器 —— 只读飞书三张表，吐出纯静态 docs/index.html。

数据源（小K 机器人 tenant token，只读）：
  订阅日报    tblhqkUN0o1i27IS   推荐等级 ∈ {强烈推荐, 推荐}，按发布时间倒序
  AI热点推荐  tbl4DgFmDlMc5SQ6   状态 ∈ {已采用, 待判断}
  网站速记    tblixXr2RAgrcYCW   全部，按时间倒序

凭证来源：优先环境变量 XIAOK_APP_ID / XIAOK_APP_SECRET（CI），
本机回落 ~/.lark-channel/config-xiaok.json。

红线：不写飞书；不输出文字稿、内容ID、record_id；不调 AI 改写摘要。
"""
import html
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

FEISHU = "https://open.feishu.cn/open-apis"
BASE_TOKEN = "UoUlb5rcca4QN1s8e5UcY7OVn8f"
TBL_SUB = "tblhqkUN0o1i27IS"   # 订阅日报
TBL_HOT = "tbl4DgFmDlMc5SQ6"   # AI热点推荐
TBL_NOTE = "tblixXr2RAgrcYCW"  # 网站速记
CST = timezone(timedelta(hours=8))

OUT_DIR = Path(__file__).resolve().parent / "docs"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 强制直连


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def http_json(method: str, url: str, body: dict | None = None,
              headers: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})})
    with OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def load_credentials() -> tuple[str, str]:
    app_id = os.environ.get("XIAOK_APP_ID")
    app_secret = os.environ.get("XIAOK_APP_SECRET")
    if app_id and app_secret:
        return app_id, app_secret
    cfg = Path.home() / ".lark-channel" / "config-xiaok.json"
    if cfg.exists():
        app = json.loads(cfg.read_text())["accounts"]["app"]
        return app["id"], app["secret"]
    raise RuntimeError("找不到小K凭证：环境变量 XIAOK_APP_ID/SECRET 未设，本机 config-xiaok.json 也没有")


class Feishu:
    def __init__(self, app_id: str, app_secret: str) -> None:
        r = http_json("POST", f"{FEISHU}/auth/v3/tenant_access_token/internal",
                      {"app_id": app_id, "app_secret": app_secret})
        if r.get("code") != 0:
            raise RuntimeError(f"小K 拿不到飞书令牌：{r.get('msg')}")
        self.token = r["tenant_access_token"]

    def records(self, table_id: str) -> list[dict]:
        """读整张表全部字段（field_names 不传 = 全量），分页拉完。"""
        out: list[dict] = []
        params: dict = {"page_size": 500}
        while True:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            r = http_json("GET", f"{FEISHU}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records?{qs}",
                          headers={"Authorization": f"Bearer {self.token}"})
            if r.get("code") != 0:
                raise RuntimeError(f"读表 {table_id} 失败：{r.get('code')} {r.get('msg')}")
            data = r.get("data") or {}
            out += data.get("items") or []
            if not data.get("has_more"):
                return out
            params["page_token"] = data["page_token"]


def cell_text(value: object) -> str:
    """飞书单元格：字符串 / 富文本分段列表 / 选项列表 / 链接对象 → 统一字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, dict):
                parts.append(seg.get("text") or seg.get("name") or seg.get("link") or "")
            elif isinstance(seg, str):
                parts.append(seg)
        return "".join(parts)
    if isinstance(value, dict):
        return value.get("link") or value.get("text") or ""
    return ""


def cell_bool(value: object) -> bool:
    return value is True


def ts_to_str(value: object) -> str:
    """毫秒时间戳或 ISO 字符串 → 'YYYY-MM-DD HH:mm'（东八区）。"""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, CST).strftime("%Y-%m-%d %H:%M")
    s = str(value)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(CST)
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s[:16]


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


# ---------- 数据装配 ----------

def fetch_sub(fs: Feishu) -> list[dict]:
    items = fs.records(TBL_SUB)
    rows = []
    for it in items:
        f = it.get("fields") or {}
        level = cell_text(f.get("推荐等级"))
        if level not in ("强烈推荐", "推荐"):
            continue
        rows.append({
            "level": level,
            "title": cell_text(f.get("标题")),
            "blogger": cell_text(f.get("博主")),
            "recommend": cell_text(f.get("选题推荐")),
            "summary_md": cell_text(f.get("AI摘要")),  # markdown，前端 marked 渲染
            "time": ts_to_str(f.get("发布时间")),
            "link": cell_text(f.get("链接")),
            "adopted": cell_bool(f.get("已采用")),
        })
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows


def fetch_hot(fs: Feishu) -> list[dict]:
    items = fs.records(TBL_HOT)
    rows = []
    for it in items:
        f = it.get("fields") or {}
        status = cell_text(f.get("状态")) or "待判断"
        if status not in ("已采用", "待判断"):
            continue
        rows.append({
            "status": status,
            "topic": cell_text(f.get("选题")),
            "core": cell_text(f.get("一句话核心内容")),
            "reason": cell_text(f.get("选题理由")),
            "angle": cell_text(f.get("切入角度")),
            "time": ts_to_str(f.get("发现时间")),
            "link": cell_text(f.get("原文链接")),
            "source": cell_text(f.get("来源")),
        })
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows


def fetch_notes(fs: Feishu) -> list[dict]:
    items = fs.records(TBL_NOTE)
    rows = []
    for it in items:
        f = it.get("fields") or {}
        status = cell_text(f.get("状态")) or "待分诊"
        if status == "已删除":
            continue
        rows.append({
            "status": status,
            "content": cell_text(f.get("内容")),
            "link": cell_text(f.get("链接")),
            "time": ts_to_str(f.get("时间")) or ts_to_str(it.get("created_time")),
        })
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows


# ---------- 页面生成 ----------

def build_html(sub_rows: list[dict], hot_rows: list[dict], note_rows: list[dict]) -> str:
    today = datetime.now(CST).strftime("%Y-%m-%d")
    today_sub = sum(1 for r in sub_rows if r["time"].startswith(today))
    today_hot = sum(1 for r in hot_rows if r["time"].startswith(today))
    n_strong = sum(1 for r in sub_rows if r["level"] == "强烈推荐")
    gen_time = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    # 数据烤进 HTML：只保留页面要显示的字段，无文字稿/内容ID/record_id
    payload = {
        "sub": sub_rows,
        "hot": hot_rows,
        "notes": note_rows,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    return TEMPLATE.replace("__DATA_JSON__", data_json) \
        .replace("__GEN_TIME__", esc(gen_time)) \
        .replace("__TODAY_NEW__", str(today_sub + today_hot)) \
        .replace("__N_STRONG__", str(n_strong)) \
        .replace("__N_POOL__", str(len(sub_rows) + len(hot_rows))) \
        .replace("__N_SUB__", str(len(sub_rows))) \
        .replace("__N_HOT__", str(len(hot_rows))) \
        .replace("__N_NOTE__", str(len(note_rows)))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>内容雷达 · 乔帮主选题看板</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<style>
:root{
  --bg-page:#f7f1e4; --bg-card:#ffffff;
  --hero-bg:#3d2436; --hero-fg:#f7f1e4;
  --fg-0:#2a2020; --fg-1:#7a6f68;
  --accent:#8a2f3f;
  --chip-rose:#f0d8d2; --chip-sage:#d8e3d3; --chip-tan:#ece0c4; --chip-mauve:#e6dbe6;
  --radius:14px;
}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  background:var(--bg-page); color:var(--fg-0);
  font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:15px; line-height:1.7;
}
.wrap{max-width:960px;margin:0 auto;padding:20px 16px 80px}

/* hero */
.hero{
  background:var(--hero-bg); color:var(--hero-fg);
  border-radius:var(--radius); padding:28px 26px 24px;
  margin-bottom:18px;
}
.hero .kicker{font-size:12px;letter-spacing:2px;opacity:.65;margin-bottom:6px}
.hero .big{font-size:44px;font-weight:700;line-height:1.15}
.hero .big small{font-size:16px;font-weight:400;opacity:.75;margin-left:6px}
.hero .guide{margin-top:8px;font-size:13px;opacity:.8}
.hero .meta{margin-top:12px;font-size:12px;opacity:.55}

/* 四宫格 */
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}
.chip{
  border-radius:var(--radius); padding:14px 14px 12px; cursor:pointer;
  border:1px solid rgba(42,32,32,.06);
  transition:transform .12s ease, box-shadow .12s ease;
  color:var(--fg-0); user-select:none;
}
.chip:hover{transform:translateY(-2px);box-shadow:0 4px 14px rgba(61,36,54,.10)}
.chip .n{font-size:26px;font-weight:700}
.chip .t{font-size:13px;margin-top:2px}
.chip.c-rose{background:var(--chip-rose)}
.chip.c-sage{background:var(--chip-sage)}
.chip.c-tan{background:var(--chip-tan)}
.chip.c-mauve{background:var(--chip-mauve)}
.chip.active{outline:2px solid var(--accent);outline-offset:2px}

/* 工具条 */
.toolbar{
  display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  margin-bottom:16px;
}
.seg{display:flex;background:#efe6d3;border-radius:999px;padding:3px}
.seg button{
  border:none;background:transparent;padding:6px 16px;border-radius:999px;
  font-size:13px;color:var(--fg-1);cursor:pointer;font-family:inherit;
}
.seg button.on{background:var(--accent);color:#fff;font-weight:600}
.filters{display:flex;gap:8px;flex-wrap:wrap}
.fbtn{
  border:1px solid #ddd0b8;background:transparent;color:var(--fg-1);
  padding:5px 12px;border-radius:999px;font-size:12.5px;cursor:pointer;font-family:inherit;
}
.fbtn.on{background:var(--fg-0);color:#f7f1e4;border-color:var(--fg-0)}
.search{
  border:1px solid #ddd0b8;background:#fff;border-radius:999px;
  padding:6px 14px;font-size:13px;font-family:inherit;min-width:140px;color:var(--fg-0);
}
.search:focus{outline:2px solid var(--accent);outline-offset:1px}

/* 内容卡 */
.card{
  background:var(--bg-card);border-radius:var(--radius);
  padding:20px 22px;margin-bottom:14px;
  border:1px solid rgba(42,32,32,.05);
}
.card .head{display:flex;align-items:flex-start;gap:10px;margin-bottom:6px;flex-wrap:wrap}
.tag{
  display:inline-block;font-size:11.5px;font-weight:600;
  padding:2px 10px;border-radius:999px;flex-shrink:0;line-height:1.8;
}
.tag.strong{background:var(--accent);color:#fff}
.tag.rec{background:var(--chip-rose);color:var(--accent)}
.tag.adopted{background:var(--chip-sage);color:#3f5a3a}
.tag.pending{background:#eee6d6;color:var(--fg-1)}
.card h3{font-size:17px;font-weight:700;line-height:1.5;flex:1;min-width:200px}
.card .sub{font-size:12.5px;color:var(--fg-1);margin-bottom:10px}
.card .sub a{color:var(--fg-1)}
.card .recommend{
  font-weight:700;font-size:14.5px;margin-bottom:10px;
  padding-left:10px;border-left:3px solid var(--accent);
}
.card .summary{border-top:1px dashed #e4d9c2;padding-top:12px;font-size:14px}
.card .summary h1,.card .summary h2,.card .summary h3,.card .summary h4{
  font-size:14.5px;margin:14px 0 6px;color:var(--accent)}
.card .summary h1:first-child,.card .summary h2:first-child,.card .summary h3:first-child{margin-top:0}
.card .summary ul,.card .summary ol{padding-left:20px;margin:6px 0}
.card .summary p{margin:6px 0}
.card .summary strong{color:var(--fg-0)}
.card .link-btn{
  display:inline-block;margin-top:12px;font-size:13px;
  color:var(--accent);text-decoration:none;font-weight:600;
}
.card .link-btn:hover{text-decoration:underline}

/* 速记 */
.notes-list .note{
  background:var(--bg-card);border-radius:10px;padding:12px 16px;margin-bottom:8px;
  border:1px solid rgba(42,32,32,.05);font-size:14px;
}
.note .nt{font-size:12px;color:var(--fg-1);margin-top:4px}
.note a{color:var(--accent);word-break:break-all}

/* 速记输入 */
.capture{
  background:var(--bg-card);border-radius:var(--radius);padding:18px 20px;margin-bottom:22px;
  border:1px dashed #d8c9a8;
}
.capture .ct{font-size:13px;font-weight:700;margin-bottom:10px}
.capture .row{display:flex;gap:8px;flex-wrap:wrap}
.capture input{
  flex:1;min-width:180px;border:1px solid #ddd0b8;border-radius:10px;
  padding:9px 14px;font-size:14px;font-family:inherit;background:#fbf8f0;color:var(--fg-0);
}
.capture input:focus{outline:2px solid var(--accent);outline-offset:1px}
.capture button{
  background:var(--accent);color:#fff;border:none;border-radius:10px;
  padding:9px 22px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;
}
.capture button:disabled{opacity:.5;cursor:default}
.capture .hint{font-size:12px;color:var(--fg-1);margin-top:8px}
.capture .ok-msg{color:#3f5a3a;font-size:13px;margin-top:8px;display:none}
.capture .err-msg{color:var(--accent);font-size:13px;margin-top:8px;display:none}

.empty{padding:40px 0;text-align:center;color:var(--fg-1);font-size:14px}
.section-title{font-size:15px;font-weight:700;margin:26px 0 12px;display:flex;align-items:center;gap:8px}
.section-title::after{content:"";flex:1;height:1px;background:#e4d9c2}

@media (max-width:640px){
  .grid4{grid-template-columns:repeat(2,1fr)}
  .hero .big{font-size:34px}
  .wrap{padding:14px 12px 60px}
  .card{padding:16px}
}
</style>
</head>
<body>
<div class="wrap">

  <div class="hero">
    <div class="kicker">内容雷达 · CONTENT RADAR</div>
    <div class="big">今日新增 __TODAY_NEW__ <small>条料</small></div>
    <div class="guide">强烈推荐 __N_STRONG__ 条 · 选题池累计 __N_POOL__ 条，看中哪条回飞书勾「已采用」</div>
    <div class="meta">更新于 __GEN_TIME__ · 数据来自飞书，静态生成，只读不改</div>
  </div>

  <div class="grid4">
    <div class="chip c-rose active" data-goto="sub"><div class="n">__N_SUB__</div><div class="t">订阅日报</div></div>
    <div class="chip c-sage" data-goto="hot"><div class="n">__N_HOT__</div><div class="t">AI 热点推荐</div></div>
    <div class="chip c-tan" data-goto="capture"><div class="n">✎</div><div class="t">灵感速记</div></div>
    <div class="chip c-mauve" data-goto="notes"><div class="n">__N_NOTE__</div><div class="t">速记回顾</div></div>
  </div>

  <div class="capture" id="sec-capture">
    <div class="ct">✎ 灵感速记 —— 一句话、一个链接，存进飞书「网站速记」</div>
    <div class="row">
      <input id="cap-content" type="text" placeholder="想到什么就写一句…" maxlength="500">
      <input id="cap-link" type="url" placeholder="链接（可选）" style="max-width:260px">
      <button id="cap-btn">存下来</button>
    </div>
    <div class="hint">存完立刻进飞书表；本网页是静态页，下次雷达跑完才会显示在这里。</div>
    <div class="ok-msg" id="cap-ok">✓ 已存进飞书「网站速记」</div>
    <div class="err-msg" id="cap-err"></div>
  </div>

  <div class="toolbar">
    <div class="seg" id="src-seg">
      <button data-src="sub" class="on">订阅日报</button>
      <button data-src="hot">AI 热点推荐</button>
    </div>
    <div class="filters" id="level-filters">
      <button class="fbtn on" data-level="all">全部</button>
      <button class="fbtn" data-level="强烈推荐">强烈推荐</button>
      <button class="fbtn" data-level="推荐">推荐</button>
    </div>
    <div class="filters" id="status-filters" style="display:none">
      <button class="fbtn on" data-status="all">全部</button>
      <button class="fbtn" data-status="待判断">待判断</button>
      <button class="fbtn" data-status="已采用">已采用</button>
    </div>
    <input class="search" id="search" type="search" placeholder="搜博主 / 标题…">
  </div>

  <div id="list"></div>

  <div class="section-title" id="sec-notes">速记回顾</div>
  <div class="notes-list" id="notes-list"></div>

</div>

<script>
const DATA = __DATA_JSON__;

const state = { src: 'sub', level: 'all', status: 'all', q: '' };
const listEl = document.getElementById('list');
const notesEl = document.getElementById('notes-list');

function escHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function mdRender(md){
  if (!md) return '';
  if (window.marked) {
    return marked.parse(md, { breaks: true });
  }
  return '<p>' + escHtml(md).replace(/\n/g, '<br>') + '</p>';
}

function subCard(r){
  const tagCls = r.level === '强烈推荐' ? 'strong' : 'rec';
  const adoptedTag = r.adopted ? '<span class="tag adopted">已采用</span>' : '';
  return `<div class="card">
    <div class="head"><span class="tag ${tagCls}">${escHtml(r.level)}</span>${adoptedTag}<h3>${escHtml(r.title)}</h3></div>
    <div class="sub">${escHtml(r.blogger)} · ${escHtml(r.time)}</div>
    ${r.recommend ? `<div class="recommend">${escHtml(r.recommend)}</div>` : ''}
    <div class="summary">${mdRender(r.summary_md)}</div>
    ${r.link ? `<a class="link-btn" href="${escHtml(r.link)}" target="_blank" rel="noopener">看原视频 →</a>` : ''}
  </div>`;
}

function hotCard(r){
  const tagCls = r.status === '已采用' ? 'adopted' : 'pending';
  return `<div class="card">
    <div class="head"><span class="tag ${tagCls}">${escHtml(r.status)}</span><h3>${escHtml(r.topic)}</h3></div>
    <div class="sub">${escHtml(r.source)} · ${escHtml(r.time)}${r.reason ? ' · ' + escHtml(r.reason) : ''}</div>
    ${r.core ? `<div class="recommend">${escHtml(r.core)}</div>` : ''}
    ${r.angle ? `<div class="summary"><p><strong>切入角度：</strong>${escHtml(r.angle)}</p></div>` : ''}
    ${r.link ? `<a class="link-btn" href="${escHtml(r.link)}" target="_blank" rel="noopener">看原文 →</a>` : ''}
  </div>`;
}

function render(){
  let html = '';
  if (state.src === 'sub'){
    const q = state.q.toLowerCase();
    const rows = DATA.sub.filter(r =>
      (state.level === 'all' || r.level === state.level) &&
      (!q || (r.title + ' ' + r.blogger).toLowerCase().includes(q))
    );
    html = rows.map(subCard).join('') || '<div class="empty">没有符合条件的条目</div>';
  } else {
    const q = state.q.toLowerCase();
    const rows = DATA.hot.filter(r =>
      (state.status === 'all' || r.status === state.status) &&
      (!q || (r.topic + ' ' + r.source).toLowerCase().includes(q))
    );
    html = rows.map(hotCard).join('') || '<div class="empty">没有符合条件的条目</div>';
  }
  listEl.innerHTML = html;
}

function renderNotes(){
  notesEl.innerHTML = DATA.notes.map(n => `<div class="note">
    <div>${escHtml(n.content)}</div>
    <div class="nt">${escHtml(n.time)}${n.link ? ' · <a href="' + escHtml(n.link) + '" target="_blank" rel="noopener">链接</a>' : ''} · ${escHtml(n.status)}</div>
  </div>`).join('') || '<div class="empty">还没有速记</div>';
}

// 分段控件：数据源切换
document.getElementById('src-seg').addEventListener('click', e => {
  const btn = e.target.closest('button'); if (!btn) return;
  state.src = btn.dataset.src;
  document.querySelectorAll('#src-seg button').forEach(b => b.classList.toggle('on', b === btn));
  document.getElementById('level-filters').style.display = state.src === 'sub' ? '' : 'none';
  document.getElementById('status-filters').style.display = state.src === 'hot' ? '' : 'none';
  document.getElementById('search').placeholder = state.src === 'sub' ? '搜博主 / 标题…' : '搜选题 / 来源…';
  render();
});

document.getElementById('level-filters').addEventListener('click', e => {
  const btn = e.target.closest('button'); if (!btn) return;
  state.level = btn.dataset.level;
  document.querySelectorAll('#level-filters .fbtn').forEach(b => b.classList.toggle('on', b === btn));
  render();
});

document.getElementById('status-filters').addEventListener('click', e => {
  const btn = e.target.closest('button'); if (!btn) return;
  state.status = btn.dataset.status;
  document.querySelectorAll('#status-filters .fbtn').forEach(b => b.classList.toggle('on', b === btn));
  render();
});

document.getElementById('search').addEventListener('input', e => {
  state.q = e.target.value.trim();
  render();
});

// 四宫格跳转
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    const target = chip.dataset.goto;
    if (target === 'sub' || target === 'hot'){
      state.src = target;
      document.querySelectorAll('#src-seg button').forEach(b => b.classList.toggle('on', b.dataset.src === target));
      document.getElementById('level-filters').style.display = target === 'sub' ? '' : 'none';
      document.getElementById('status-filters').style.display = target === 'hot' ? '' : 'none';
      render();
      document.querySelector('.toolbar').scrollIntoView({ behavior: 'smooth' });
    } else if (target === 'capture'){
      document.getElementById('sec-capture').scrollIntoView({ behavior: 'smooth' });
      document.getElementById('cap-content').focus();
    } else if (target === 'notes'){
      document.getElementById('sec-notes').scrollIntoView({ behavior: 'smooth' });
    }
  });
});

// 灵感速记提交（Cloudflare Worker 代理，密钥不进浏览器）
document.getElementById('cap-btn').addEventListener('click', async () => {
  const content = document.getElementById('cap-content').value.trim();
  const link = document.getElementById('cap-link').value.trim();
  const okEl = document.getElementById('cap-ok');
  const errEl = document.getElementById('cap-err');
  okEl.style.display = 'none'; errEl.style.display = 'none';
  if (!content){ errEl.textContent = '写一句再存'; errEl.style.display = 'block'; return; }
  const btn = document.getElementById('cap-btn');
  btn.disabled = true; btn.textContent = '存…';
  try {
    const resp = await fetch('/api/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, link }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    okEl.style.display = 'block';
    document.getElementById('cap-content').value = '';
    document.getElementById('cap-link').value = '';
  } catch (err) {
    errEl.textContent = '没存上：' + err.message + '（可以去飞书表里手动补一条）';
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = '存下来';
  }
});

render();
renderNotes();
</script>
</body>
</html>
"""


def main() -> None:
    app_id, app_secret = load_credentials()
    fs = Feishu(app_id, app_secret)

    sub_rows = fetch_sub(fs)
    log(f"订阅日报：强烈推荐+推荐 共 {len(sub_rows)} 条")
    hot_rows = fetch_hot(fs)
    log(f"AI热点推荐：已采用+待判断 共 {len(hot_rows)} 条")
    note_rows = fetch_notes(fs)
    log(f"网站速记：{len(note_rows)} 条")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "CNAME").write_text("radar.qiao-flow.cn\n", encoding="utf-8")
    html_text = build_html(sub_rows, hot_rows, note_rows)
    out = OUT_DIR / "index.html"
    out.write_text(html_text, encoding="utf-8")
    log(f"已生成 {out}（{len(html_text)//1024} KB）")

    # 红线自检：不该出现的东西直接报错退出，让 CI 标红
    banned = ["文字稿", "内容ID", "record_id", "app_secret", "tenant_access_token"]
    for word in banned:
        if word in html_text:
            print(f"红线违规：生成的 HTML 里出现了「{word}」", file=sys.stderr)
            sys.exit(1)
    log("红线自检通过：无文字稿/内容ID/record_id/密钥")


if __name__ == "__main__":
    main()
