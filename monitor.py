"""Zero-LLM public recruitment links watcher. Candidates require manual verification."""
from __future__ import annotations
import argparse
import ast
import hashlib
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data/items.json'
REPORT = ROOT / 'data/new_items.json'
STATUS = ROOT / 'data/source_status.json'
OUT = ROOT / 'docs/index.html'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; RecruitmentWatch/2.0; educational-personal-use)'}
JOBS = re.compile(r'招聘|招录|招考|人才引进|校招|应届毕业生|招聘岗位|公开选聘|招聘简章|人才招聘')
PROCESS = re.compile(r'拟聘|公示|笔试|面试|考察|后续工作安排|资格审核|笔试成绩|资格复审|进入面试|面试公告|考察人选|成绩查询|面试名单|资格审查|录用名单|补充公告|体检公告')
NOISE = re.compile(r'招标|采购|中标|投标|招商|研究生招生|博士生招生|招生简章|推免|复试|夏令营|博士后出站|公开招聘工作.*通知|公开招聘人员暂行规定|公开招聘人员实施办法|岗位信息表.*下载')
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


class FetchError(RuntimeError):
    pass


def error_message(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return 'HTTPS证书或TLS握手失败（未关闭证书校验）'
    if isinstance(exc, requests.exceptions.Timeout):
        return '连接或读取超时，已有限重试'
    if isinstance(exc, requests.exceptions.HTTPError):
        return f'服务器返回 HTTP {exc.response.status_code}，未取得公告页面'
    if isinstance(exc, requests.exceptions.ConnectionError):
        return '连接失败：域名解析、网络线路或服务器不可达'
    return str(exc)[:500]


def fetch(session, url, cache, **kwargs):
    # Reuse shared official fallback pages without requesting them for each unit.
    key = (url, json.dumps(kwargs, sort_keys=True, ensure_ascii=False))
    if key in cache:
        result = cache[key]
        if isinstance(result, Exception):
            raise result
        return result
    try:
        response = session.get(url, headers=HEADERS, timeout=(12, 30), **kwargs)
        response.raise_for_status()
        if not response.encoding or response.encoding.lower() in ('iso-8859-1', 'ascii'):
            response.encoding = response.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        if re.search(r'云防护|访问验证|安全验证|访问请求.*阻断|Just a moment|Access Denied', title, re.I):
            raise FetchError('返回的是访问防护页面，未取得公告内容')
        cache[key] = response
        return response
    except Exception as exc:
        cache[key] = exc
        raise


def item_record(source, title, target, context=''):
    m = DATE.search(context)
    date = f'{m.group(1)}-{int(m.group(2)):02}-{int(m.group(3)):02}' if m else ''
    kind = '招聘进度' if PROCESS.search(title) else '招聘公告'
    score = '2028届线索' if HIGH.search(title) else ('专业匹配关键词' if BOOST.search(title) else '普通公告')
    return dict(title=title, url=target, source=source['name'], group=source['group'],
                published=date, kind=kind, match=score, priority=(kind == '招聘公告' and score != '普通公告'))


def extract(source, session, cache=None):
    cache = {} if cache is None else cache
    response = fetch(session, source['url'], cache)
    soup = BeautifulSoup(response.text, 'html.parser')
    # CNAS publishes its list through the same public GET endpoint used by its page.
    if source.get('adapter') == 'cnas':
        script = soup.select_one('script[querydata][url]')
        if script is None:
            raise FetchError('认可中心动态列表结构已变化，需要检查接口')
        endpoint = urljoin(response.url, script['url'])
        if urlparse(endpoint).netloc != urlparse(response.url).netloc:
            raise FetchError('动态列表接口不是同源地址，停止读取')
        params = ast.literal_eval(script['querydata'])
        payload = fetch(session, endpoint, cache, params=params).json()
        fragment = payload.get('data', {}).get('html')
        if not payload.get('success') or not isinstance(fragment, str):
            raise FetchError('认可中心公告接口未返回有效列表')
        soup = BeautifulSoup(fragment, 'html.parser')
    for node in soup(['script', 'style', 'noscript', 'nav', 'footer']):
        node.decompose()
    if source.get('content_selector'):
        content = soup.select_one(source['content_selector'])
        if content is None:
            raise FetchError('招聘正文结构已变化，未找到指定内容区域')
        text = ' '.join(content.stripped_strings)
        if not JOBS.search(text) or len(text) < 60:
            raise FetchError('招聘正文为空或未识别到招聘内容')
        heading = content.select_one(source.get('title_selector', 'h1'))
        title = heading.get_text(' ', strip=True) if heading else source['name'] + '：招聘岗位（正文更新监控）'
        item = item_record(source, title, normalize(response.url), text)
        item['content_hash'] = hashlib.sha256(text.encode()).hexdigest()
        return [item]
    root_host = (urlparse(response.url).hostname or '').lower()
    allowed = {root_host, *source.get('allowed_hosts', [])}
    found = {}
    anchors = soup.select(source.get('link_selector', 'a[href]'))
    for a in anchors:
        label = re.sub(r'\s+', ' ', a.get('title') or a.get_text(' ', strip=True)).strip()
        if len(label) < 7 or len(label) > 220 or not JOBS.search(label) or NOISE.search(label):
            continue
        if source.get('title_filter') and not re.search(source['title_filter'], label):
            continue
        target = normalize(urljoin(response.url, a.get('href', '')))
        u = urlparse(target)
        host = (u.hostname or '').lower()
        if u.scheme not in ('http', 'https') or not host or not any(host == h or host.endswith('.' + h) for h in allowed):
            continue
        if normalize(response.url) == target:
            continue
        parent = a.parent.get_text(' ', strip=True)[:500] if a.parent else label
        found[target] = item_record(source, label, target, parent)
    return list(found.values())


def scan(source, session, cache):
    status = dict(name=source['name'], group=source['group'], kind=source.get('kind', ''),
                  url=source['url'], ok=False, found=0, health='error', note=source.get('note', ''), attempts=[])
    config = json.dumps(source, sort_keys=True, ensure_ascii=False)
    status['config_hash'] = hashlib.sha256(config.encode()).hexdigest()
    endpoints = [source, *[{**source, **fallback} for fallback in source.get('fallbacks', [])]]
    for index, endpoint in enumerate(endpoints):
        try:
            candidates = extract(endpoint, session, cache)
            if not candidates and index < len(endpoints) - 1:
                raise FetchError('页面可读取，但未提取到匹配公告；尝试备用栏目')
            status.update(ok=True, found=len(candidates), effective_url=endpoint['url'],
                          health='warning' if index or not candidates or endpoint['kind'] == 'homepage' else 'ok',
                          fallback=bool(index))
            if index:
                status['warning'] = '仅备用来源可用，覆盖不完整：' + endpoint.get('fallback_note', '主管部门公开公告')
            elif not candidates:
                status['warning'] = '页面可访问，但未提取到公告；不代表没有招聘'
            elif endpoint['kind'] == 'homepage':
                status['warning'] = '仅首页链接，覆盖不完整'
            if urlparse(endpoint['url']).scheme == 'http':
                status['health'] = 'warning'
                status['warning'] = (status.get('warning', '') + '；使用官方HTTP公开栏目，连接未加密').strip('；')
            return candidates, status
        except Exception as exc:
            status['attempts'].append(dict(url=endpoint['url'], error=error_message(exc)))
            print(f"WARNING {source['name']}: {endpoint['url']}: {exc}", file=sys.stderr)
    status['error'] = '；'.join(a['error'] for a in status['attempts'])
    return [], status


def merge_items(known, candidates, now, baseline):
    new = []
    for item in candidates:
        previous = known.get(item['url'])
        if previous is None:
            item['first_seen'] = now
            known[item['url']] = item
            if not baseline and item['kind'] == '招聘公告':
                new.append(item)
        else:
            changed = (item.get('content_hash') and previous.get('content_hash')
                       and item['content_hash'] != previous['content_hash'])
            # Stable recruitment pages can change their jobs without changing URL.
            for field in ('title', 'published', 'match', 'kind', 'priority', 'content_hash'):
                if field in item and (field != 'published' or not previous.get(field)):
                    previous[field] = item[field]
            if changed:
                previous['last_changed'] = now
                if not baseline:
                    new.append({**previous, 'event': '招聘正文更新'})
    return new


def needs_baseline(previous, current, first_run=False):
    return (first_run or not previous.get('ok')
            or previous.get('config_hash') != current['config_hash']
            or previous.get('effective_url') != current.get('effective_url'))


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
        esc = lambda value: html.escape(str(value or ''), quote=True)
        health = s.get('health', 'ok' if s['ok'] else 'error')
        label = {'ok': '栏目已读取', 'warning': '需注意', 'error': '检查失败'}[health]
        kind = {'recruitment': '招聘栏目', 'announcements': '综合公告', 'homepage': '首页兜底', 'content': '招聘正文更新监控'}.get(s.get('kind'), '')
        detail = s.get('error') or s.get('warning') or ''
        attempts = ''.join(f'<div class="muted">{esc(a["url"])}：{esc(a["error"])}</div>' for a in s.get('attempts', []))
        fallback_link = (f'<div class="muted">实际读取：<a href="{esc(s["effective_url"])}" target="_blank" rel="noopener noreferrer">备用栏目</a></div>'
                         if s.get('fallback') else '')
        statuscards.append(f'<div class="source"><span class="{health}">{"●" if health == "ok" else "!"}</span> '
                           f'<a href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer">{esc(s["name"])}</a>'
                           f' <span class="muted">{esc(s["group"])} · {s.get("found", 0)}条 · {label} · {kind}</span>'
                           f'<div class="muted">{esc(detail)}</div>{fallback_link}{attempts}'
                           f'<div class="muted">{esc(s.get("note"))}</div></div>')
    options = ''.join(f'<option value="{html.escape(g, quote=True)}">{html.escape(g)}</option>' for g in groups)
    markup = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>2028届中央事业单位招聘雷达</title><style>
    :root{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;color:#182536;background:#f5f7fa}*{box-sizing:border-box}body{max-width:1060px;margin:35px auto;padding:0 20px}h1{font-size:28px;margin:0 0 6px}h2{font-size:19px;margin-top:28px}p{color:#536477;line-height:1.7}.note{background:#eef4ff;padding:14px 17px;border-radius:10px}.bar{display:flex;gap:9px;flex-wrap:wrap;margin:20px 0}input,select{background:white;padding:10px;border:1px solid #ced7e3;border-radius:8px;font:inherit}input{flex:1;min-width:190px}article{padding:17px 19px;border:1px solid #e2e8f0;border-radius:10px;background:#fff;margin:9px 0}.meta,.muted{color:#65758a;font-size:12px}.meta{margin-bottom:7px}a{color:#1556bd;text-decoration:none}a:hover{text-decoration:underline}article>a{font-weight:600}.chip{color:#1a5798;background:#edf4ff;border-radius:4px;padding:2px 5px}details{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:15px}.source{padding:8px 0;border-bottom:1px solid #edf0f5;font-size:14px}.ok{color:#16834c}.warning{color:#a15c00}.error{color:#b42318}small{color:#65758a}[hidden]{display:none!important} .count{font-weight:700}
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
    previous_status = {s['name']: s for s in load(STATUS, [])}
    cache = {}
    with requests.Session() as session:
        retry = Retry(total=1, connect=1, read=1, status=1, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'], respect_retry_after_header=False)
        session.mount('https://', HTTPAdapter(max_retries=retry))
        session.mount('http://', HTTPAdapter(max_retries=retry))
        for source in sources:
            candidates, s = scan(source, session, cache)
            previous = previous_status.get(source['name'], {})
            # Recovery, a new URL, or a newly available fallback must not flood old notices.
            baseline = needs_baseline(previous, s, first_run)
            s['baseline'] = bool(baseline and s['ok'])
            new.extend(merge_items(known, candidates, now, baseline))
            print(f"{source['name']}: {len(candidates)} links; {s['health']}; baseline={baseline}", flush=True)
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
