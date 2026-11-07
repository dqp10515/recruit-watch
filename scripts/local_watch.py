"""Mac launch agent collector. Use a dedicated clone, never the user's working checkout."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPO = 'dqp10515/recruit-watch'
SNAPSHOT = ['data/items.json', 'data/new_items.json', 'data/source_status.json', 'docs/index.html']


def batch_id(items):
    events = sorted((x['url'], x.get('content_hash', '')) for x in items)
    return hashlib.sha256(json.dumps(events, ensure_ascii=False).encode()).hexdigest()


def queue_report(directory, items):
    if not items:
        return
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (batch_id(items) + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def run(command, *, github=False, capture=False):
    env = os.environ.copy()
    if github and env.get('RECRUIT_GITHUB_PROXY'):
        env['HTTPS_PROXY'] = env['RECRUIT_GITHUB_PROXY']
        env['HTTP_PROXY'] = env['RECRUIT_GITHUB_PROXY']
    result = subprocess.run(command, cwd=ROOT, env=env, check=True,
                            text=True, encoding='utf-8', capture_output=capture)
    return result.stdout.rstrip('\r\n') if capture else ''


def notify_pending(directory):
    pending = sorted(directory.glob('*.json'))
    if not pending:
        return
    # Search recent issues directly, avoiding search-index delays when retrying after a crash.
    existing = json.loads(run(['gh', 'issue', 'list', '--repo', REPO, '--state', 'all',
                              '--limit', '100', '--json', 'body'], github=True, capture=True))
    bodies = [x.get('body') or '' for x in existing]
    for path in pending:
        items = json.loads(path.read_text(encoding='utf-8'))
        marker = f'<!-- recruit-watch-local:{path.stem} -->'
        if not any(marker in body for body in bodies):
            lines = ['本机补抓发现的招聘链接或招聘正文更新。请核实毕业届别、编制及截止日期。', '']
            for item in sorted(items, key=lambda x: (not x.get('priority', False), x['title']))[:30]:
                title = item['title'].replace('[', '［').replace(']', '］')
                lines.append(f'- [{title}]({item["url"]}) — {item["source"]} · {item.get("event", "新发现链接")}')
            lines += ['', f'[全部公告](https://dqp10515.github.io/recruit-watch/)（本批 {len(items)} 条）', '', marker]
            body = '\n'.join(lines)
            with tempfile.TemporaryDirectory() as temporary:
                body_file = Path(temporary) / 'issue.md'
                body_file.write_text(body, encoding='utf-8')
                date = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d')
                run(['gh', 'issue', 'create', '--repo', REPO, '--title',
                     f'本机补抓招聘公告变化：{len(items)} 条 ({date})', '--body-file', str(body_file)], github=True)
            bodies.append(body)
        path.unlink()


def push_snapshot():
    run(['git', 'add', '--', *SNAPSHOT])
    changed = run(['git', 'diff', '--cached', '--name-only'], capture=True)
    if changed:
        run(['git', 'commit', '-m', 'Update recruitment snapshot from local collector'])
    # The Mac runs an hour after cloud collection. Abort genuine conflicts for review.
    run(['git', 'pull', '--rebase'], github=True)
    run(['git', 'push', 'origin', 'main'], github=True)


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    # Explicit marker prevents accidental use against the user's own working copy.
    if not (ROOT / '.git' / 'recruit-watch-managed').exists():
        raise SystemExit('Refusing to run: this is not a dedicated managed clone.')
    with (state_dir / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A local collection is already running; skipping.', flush=True)
            return
        outbox = state_dir / 'outbox'
        changed = run(['git', 'status', '--porcelain', '--untracked-files=all'], capture=True)
        if changed:
            paths = {line[3:] for line in changed.splitlines()}
            if not paths.issubset(set(SNAPSHOT)):
                raise SystemExit('Managed checkout has unexpected changes; inspect before retrying.')
            # Recover an interrupted snapshot before collecting again.
            for file in SNAPSHOT[:3]:
                json.loads((ROOT / file).read_text(encoding='utf-8'))
            queue_report(outbox, json.loads((ROOT / SNAPSHOT[1]).read_text(encoding='utf-8')))
            push_snapshot()
        else:
            run(['git', 'pull', '--rebase'], github=True)
            run(['git', 'push', 'origin', 'main'], github=True)
        notify_pending(outbox)
        initialized = state_dir / 'initialized'
        baseline = args.baseline or not initialized.exists()
        command = [sys.executable, str(ROOT / 'monitor.py')]
        if baseline:
            command.append('--baseline')
        run(command)
        items = json.loads((ROOT / SNAPSHOT[1]).read_text(encoding='utf-8'))
        if not baseline:
            queue_report(outbox, items)
        push_snapshot()
        # Queue is outside the Git checkout and survives an API outage or a reboot.
        notify_pending(outbox)
        initialized.write_text('ready\n', encoding='utf-8')
        print(f'Local snapshot pushed; candidate changes: {len(items)}; baseline={baseline}', flush=True)


if __name__ == '__main__':
    main()
