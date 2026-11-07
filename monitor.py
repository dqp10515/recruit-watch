"""Zero-LLM public recruitment links watcher. Candidates require manual verification."""
from __future__ import annotations
import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data/items.json'
REPORT = ROOT / 'data/new_items.json'
STATUS = ROOT / 'data/source_status.json'
OUT = ROOT / 'docs/index.html'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; RecruitmentWatch/2.0; educational-personal-use)'}
JOBS = re.compile(r'招聘|招录|招考|人才引进|校招|应届毕业生|招聘岗位|公开选聘|招聘简章|人才招聘')
PROCESS = re.compile(r'拟聘|公示|笔试成绩|资格复审|进入面试|面试公告|考察人选|成绩查询|面试名单|资格审查|录用名单|补充公告|体检公告')
NOISE = re.compile(r'招标|采购|中标|投标|招商|研究生招生|博士生招生|招生简章|推免|复试|夏令营|博士后出站')
BOOST = re.compile(r'2028届|2028年.*毕业|博士|人工智能|大模型|计算机|网络安全|数据安全|软件|信息安全|事业编|编制')
HIGH = re.compile(r'2028届|2028年.*毕业')
DATE = re.compile(r'(20\d{2})[-年./](0?[1-9]|1[0-2])[-月./](0?[1-9]|[12]\d|3[01])')


def load(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def normalize(url):
    url, _ = urldefrag(url)
    return url.rstrip('/')


def extract(source, session):
    response = session.get(source['url'], headers=HEADERS, timeout=18)
    response.raise_for_status()
    # requests can get a wrong default for Chinese pages without charset
    if not response.encoding or response.encoding.lower() in ('iso-8859-1', 'ascii'):
        response.encoding = response.apparent_encoding or 'utf-8'
    soup = BeautifulSoup(response.text, 'html.parser')
    for item in soup(['script', 'style', 'noscript', 'nav', 'footer']):
        item.decompose()
    root_host = (urlparse(response.url).hostname or '').lower()
    found = {}
    for a in soup.select('a[href]'):
        label = ' '.join(a.stripped_strings)
        label = re.sub(r'\s+', ' ', label).strip()
        if len(label) < 7 or len(label) > 220 or not JOBS.search(label) or NOISE.search(label):
            continue
        target = normalize(urljoin(response.url, a.get('href', '')))
        u = urlparse(target)
        if u.scheme not in ('http', 'https') or not u.hostname:
            continue
        # Don't send a user to an arbitrary cross-domain destination on a compromised page.
        host = u.hostname.lower()
        if host != root_host and not host.endswith('.' + root_host):
            continue
        if normalize(response.url) == target:
            continue
        parent = a.parent.get_text(' ', strip=True)[:350] if a.parent else label
        m = DATE.search(parent)
        date = f'{m.group(1)}-{int(m.group(2)):02}-{int(m.group(3)):02}' if m else ''
        kind = '招聘进度' if PROCESS.search(label) else '招聘公告'
        score = '2028届线索' if HIGH.search(label) else ('专业匹配关键词' if BOOST.search(label) else '普通公告')
        found[target] = dict(title=label, url=target, source=source['name'], group=source['group'],
                             published=date, kind=kind, match=score, priority=(kind == '招聘公告' and score != '普通公告'))
    return list(found.values())


def page(items, statuses, scanned):
    groups = sorted({x['group'] for x in statuses})
    cards = []
    for item in sorted(items, key=lambda x: (x.get('published') or '', x.get('first_seen') or ''), reverse=True):
        esc = lambda s: html.escape(str(s or ''), quote=True)
        searchtext = ' '.join([item['title'], item['source'], item['group'], item.get('match', '')]).lower()
        cards.append(f'<article data-text="{esc(searchtext)}" data-group="{esc(item["group"])}" data-kind="{esc(item.get("kind", "招聘公告"))}">'
                     f'<div class="meta">{esc(item["group"])} · {esc(item["source"])} · {esc(item.get("published") or "发布时间未识别")}'
                     f' · <span class="chip">{esc(item.get("match", "普通公告"))}</span> · {esc(item.get("kind", "招聘公告"))}</div>'
                     f'<a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["title"])}</a></article>')
    statuscards = []
    for s in statuses:
        c = 'ok' if s['ok'] else 'error'
        statuscards.append(f'<div class="source"><span class="{c}">{"●" if s["ok"] else "!"}</span> '
                           f'<a href="{html.escape(s["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(s["name"])}</a>'
                           f' <span class="muted">{html.escape(s["group"])} · {s.get("found", 0)}条'
                           f'{" · 检查失败" if not s["ok"] else ""} · {html.escape(s.get("kind", ""))}</span></div>')
    options = ''.join(f'<option value="{html.escape(g, quote=True)}">{html.escape(g)}</option>' for g in groups)
    markup = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>2028届中央事业单位招聘雷达</title><style>
    :root{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;color:#182536;background:#f5f7fa}*{box-sizing:border-box}body{max-width:1060px;margin:35px auto;padding:0 20px}h1{font-size:28px;margin:0 0 6px}h2{font-size:19px;margin-top:28px}p{color:#536477;line-height:1.7}.note{background:#eef4ff;padding:14px 17px;border-radius:10px}.bar{display:flex;gap:9px;flex-wrap:wrap;margin:20px 0}input,select{background:white;padding:10px;border:1px solid #ced7e3;border-radius:8px;font:inherit}input{flex:1;min-width:190px}article{padding:17px 19px;border:1px solid #e2e8f0;border-radius:10px;background:#fff;margin:9px 0}.meta,.muted{color:#65758a;font-size:12px}.meta{margin-bottom:7px}a{color:#1556bd;text-decoration:none}a:hover{text-decoration:underline}article>a{font-weight:600}.chip{color:#1a5798;background:#edf4ff;border-radius:4px;padding:2px 5px}details{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:15px}.source{padding:8px 0;border-bottom:1px solid #edf0f5;font-size:14px}.ok{color:#16834c}.error{color:#b42318}small{color:#65758a}[hidden]{display:none!important} .count{font-weight:700}
    </style></head><body><h1>📡 2028届中央事业单位招聘雷达</h1><p>工信部 · 科技部 · 市场监管总局 · 中科院及相关单位。零大模型调用，每周自动发现官方页面上的招聘链接。</p><p class="note"><strong>重要：</strong>网页采集的是候选公告，不自动确认2028届报考资格、事业编制或截止日期。官网首页兜底来源可能漏掉深层招聘页；“企业编制”不等于事业编。请以完整招聘公告和岗位表为准。</p><small>最近检查：__SCANNED__ | 当前显示 <span id="count" class="count">0</span> 条 | 共监控 __TOTAL__ 个来源</small><div class="bar"><input id="q" placeholder="搜索：2028 / 博士 / 计算机 / 人工智能 / 招聘…"><select id="group"><option value="">全部系统</option>__OPTIONS__</select><select id="kind"><option value="招聘公告">仅招聘公告（推荐）</option><option value="">含笔试、面试、公示等进度</option><option value="招聘进度">仅招聘进度</option></select></div><main>__CARDS__</main><h2>监控来源与健康状态</h2><details><summary>展开 __TOTAL__ 个来源（抓取失败也会显示）</summary>__STATUS__</details><p><small>页面由公开网页列表提取并按链接去重。监控异常不意味着该单位没有招聘。最后更新：__SCANNED__。</small></p><script>function f(){let q=document.querySelector('#q').value.trim().toLowerCase(),g=document.querySelector('#group').value,k=document.querySelector('#kind').value,n=0;document.querySelectorAll('article').forEach(e=>{let ok=(!q||e.dataset.text.includes(q))&&(!g||e.dataset.group===g)&&(!k||e.dataset.kind===k);e.hidden=!ok;n+=ok});document.querySelector('#count').textContent=n}for(let id of ['q','group','kind'])document.getElementById(id).addEventListener(id==='q'?'input':'change',f);f()</script></body></html>'''
    return (markup.replace('__SCANNED__', html.escape(scanned)).replace('__TOTAL__', str(len(statuses)))
            .replace('__OPTIONS__', options).replace('__CARDS__', '\n'.join(cards) or '<p>暂无公告，请查看下方来源状态。</p>')
            .replace('__STATUS__', '\n'.join(statuscards)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--baseline', action='store_true', help='Initialize without notifications')
    args = ap.parse_args()
    sources = load(ROOT / 'sources.json', [])
    old = load(DATA, [])
    known = {v['url']: v for v in old}
    first_run = not DATA.exists() or args.baseline
    new, status = [], []
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    with requests.Session() as session:
        for source in sources:
            s = dict(name=source['name'], group=source['group'], kind=source.get('kind',''), url=source['url'], ok=False, found=0)
            try:
                candidates = extract(source, session)
                s.update(ok=True, found=len(candidates))
                print(f'{source["name"]}: {len(candidates)} links')
                for item in candidates:
                    if item['url'] not in known:
                        item['first_seen'] = now
                        known[item['url']] = item
                        if not first_run and item['kind'] == '招聘公告':
                            new.append(item)
                    else:
                        old_item = known[item['url']]
                        for field in ('published', 'match', 'kind', 'priority'):
                            if field != 'published' or not old_item.get(field):
                                old_item[field] = item[field]
            except Exception as exc:
                s['error'] = str(exc)[:180]
                print(f'WARNING {source["name"]}: {exc}', file=sys.stderr)
            status.append(s)
    if status and not any(s['ok'] for s in status):
        sys.exit('All source fetches failed. Old snapshot preserved.')
    for p in (DATA, REPORT, STATUS, OUT): p.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(list(known.values()), ensure_ascii=False, indent=2), encoding='utf-8')
    REPORT.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding='utf-8')
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    OUT.write_text(page(list(known.values()), status, now), encoding='utf-8')
    print(f'Total: {len(known)}; new job links: {len(new)}; sources OK: {sum(s["ok"] for s in status)}/{len(status)}')

if __name__ == '__main__':
    main()
