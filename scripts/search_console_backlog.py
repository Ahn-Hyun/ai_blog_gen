"""Read-only Search Console collection and a deterministic English-post edit queue."""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from store.local_store import write_json

API = 'https://www.googleapis.com/webmasters/v3/sites'
SCOPE = 'https://www.googleapis.com/auth/webmasters.readonly'
GROUPS = {'pages': ['page'], 'queries': ['page', 'query'],
          'segments': ['page', 'country', 'device'], 'dates': ['date']}
PAGE_LIMIT = 25000


def windows(end: date) -> dict:
    return {name: {'startDate': (last - timedelta(days=27)).isoformat(),
                   'endDate': last.isoformat()}
            for name, last in [('current', end), ('previous', end - timedelta(days=28))]}


def authorized_session():
    raw = os.environ.get('GSC_CREDENTIALS_JSON')
    filename = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not raw and not filename:
        raise ValueError('Search Console 인증 필요: GSC_CREDENTIALS_JSON 또는 GOOGLE_APPLICATION_CREDENTIALS를 지정하세요.')
    info = json.loads(raw) if raw else json.loads(Path(filename).read_text())
    # Only explicit Google credentials; never borrow the machine's default ADC.
    if info.get('type') not in {'service_account', 'authorized_user'}:
        raise ValueError('전용 service_account 또는 authorized_user 인증만 지원합니다.')
    if info.get('token_uri', 'https://oauth2.googleapis.com/token') != 'https://oauth2.googleapis.com/token':
        raise ValueError('Google OAuth token endpoint만 허용합니다.')
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.load_credentials_from_dict(info, scopes=[SCOPE])
    return AuthorizedSession(credentials)


def request_json(session, method: str, url: str, body=None) -> dict:
    for attempt in range(3):
        response = session.request(method, url, json=body, timeout=60)
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        if response.status_code != 200:
            # Never print responses/credentials; permission failures are not zero traffic.
            raise RuntimeError(f'Search Console HTTP {response.status_code}; API 활성화·속성 권한·OAuth 범위를 확인하세요.')
        return response.json()
    raise RuntimeError('Search Console request failed')


def fetch_rows(session, site: str, period: dict, dimensions: list) -> list:
    rows = []
    while True:
        body = {**period, 'dimensions': dimensions, 'type': 'web', 'dataState': 'final',
                'aggregationType': 'auto', 'rowLimit': PAGE_LIMIT, 'startRow': len(rows),
                'dimensionFilterGroups': [{'filters': [{'dimension': 'page',
                    'operator': 'includingRegex', 'expression': r'^https://ship-write\.com/blog/'}]}]}
        batch = request_json(session, 'POST', f'{API}/{quote(site, safe="")}/searchAnalytics/query', body).get('rows', [])
        rows.extend(batch)
        if len(batch) < PAGE_LIMIT:
            return rows


def collect(session, site: str, end: date) -> dict:
    if site not in {'sc-domain:ship-write.com', 'https://ship-write.com/'}:
        raise ValueError('영어 ship-write.com 속성만 지원합니다.')
    sites = request_json(session, 'GET', API).get('siteEntry', [])
    if not any(s.get('siteUrl') == site and s.get('permissionLevel') != 'siteUnverifiedUser' for s in sites):
        raise ValueError('지정한 영어 Search Console 속성에 접근할 수 없습니다. GSC_SITE_URL과 속성 권한을 확인하세요.')
    periods = windows(end)
    return {'site': site, 'collected_at': datetime.now(ZoneInfo('UTC')).isoformat(),
            'search_type': 'web', 'data_state': 'final', 'timezone': 'America/Los_Angeles',
            'periods': periods, 'data': {name: {group: fetch_rows(session, site, period, dims)
                for group, dims in GROUPS.items()} for name, period in periods.items()}}


def page_key(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'ship-write.com' or not parsed.path.startswith('/blog/'):
        return None
    return parsed.path.rstrip('/') if not parsed.query and not parsed.fragment else None


def inventory(root: Path) -> dict:
    posts = {}
    files = list((root / 'src/content/blog').glob('*.mdx'))
    if not files:
        raise ValueError('영어 Astro 원고 경로가 비어 있습니다. ASTRO_ROOT를 확인하세요.')
    for path in files:
        match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', path.read_text(), re.S)
        if not match or re.search(r'^draft: true$', match[1], re.M):
            continue
        title = re.search(r'^title: (.+)$', match[1], re.M)
        # Astro routes by post.id and ignores foreign canonical metadata in old posts.
        url = f'https://ship-write.com/blog/{path.stem}'
        key = page_key(url)
        if key:
            posts[key] = {'title': title[1].strip().strip('"\'') if title else path.stem,
                          'url': url, 'file': str(path.relative_to(root))}
    return posts


def metrics(row: dict) -> dict:
    clicks, impressions = row['clicks'], row['impressions']
    return {'clicks': clicks, 'impressions': impressions,
            'ctr': clicks / impressions if impressions else 0, 'position': row['position']}


def build_backlog(snapshot: dict, posts: dict) -> dict:
    current, previous = snapshot['data']['current'], snapshot['data']['previous']
    # Page totals are queried separately: query rows omit anonymized queries.
    prior = {r['keys'][0]: metrics(r) for r in previous['pages']}
    candidates, observed = [], set()
    unmatched = []
    for row in current['pages']:
        url = row['keys'][0]
        key = page_key(url)
        if key not in posts:
            unmatched.append(url)
            continue
        observed.add(key)
        now, before = metrics(row), prior.get(url)
        actions = []
        if now['impressions'] >= 100:
            if now['ctr'] < 0.02 and now['position'] <= 20:
                actions.append('검색어와 제목·도입부의 일치 여부 검토')
            if 8 <= now['position'] <= 20:
                actions.append('상위 노출 검색어의 빠진 답·자료 갱신·관련 내부 링크 검토')
            if before and before['clicks'] >= 10 and now['clicks'] <= before['clicks'] * 0.7:
                actions.append('클릭 하락 원인 검토: 검색 수요·순위·기기 구성·내용 변경을 구분')
        if not actions:
            continue
        query_rows = [r for r in current['queries'] if r['keys'][0] == url]
        segment_rows = [r for r in current['segments'] if r['keys'][0] == url]
        candidates.append({**posts[key], 'current': now, 'previous': before, 'actions': actions,
            'queries': sorted(query_rows, key=lambda r: r['impressions'], reverse=True)[:5],
            'segments': sorted(segment_rows, key=lambda r: r['impressions'], reverse=True)[:5]})
    candidates.sort(key=lambda c: (-c['current']['impressions'], c['url']))
    return {'status': 'ok' if current['pages'] else 'no_data', 'periods': snapshot['periods'],
            'candidates': candidates, 'published_posts': len(posts), 'observed_posts': len(observed),
            'unobserved_posts': [p['url'] for k, p in posts.items() if k not in observed],
            'unmatched_urls': unmatched,
            'returned_dates': {name: [r['keys'][0] for r in snapshot['data'][name]['dates']]
                               for name in ('current', 'previous')}}


def safe(value) -> str:
    return html.escape(str(value)).replace('|', '&#124;').replace('\n', ' ').replace('\r', ' ')


def render(report: dict) -> str:
    p = report['periods']
    lines = ['# 영어 블로그 검색 개선 목록', '',
        f"비교: {p['current']['startDate']}–{p['current']['endDate']} / {p['previous']['startDate']}–{p['previous']['endDate']} (PT, 각 28일)", '',
        '대상: ship-write.com/blog/, Web 검색, 전체 국가·기기, 확정 데이터. 영어 검색어만 필터링한 결과는 아닙니다.', '',
        'Search Console은 상위 행만 반환할 수 있고 익명 검색어를 생략합니다. 미반환 페이지를 클릭 0이나 미색인으로 단정하지 않습니다.',
        '페이지 합계와 검색어·국가/기기 행을 별도로 수집했습니다. 서로 더해 전체 합계로 쓰지 않습니다.', '',
        f"발행 원고 {report['published_posts']}개 / 이번 기간 반환된 원고 {report['observed_posts']}개 / 개선 후보 {len(report['candidates'])}개", '',
        '초기 선별 규칙: 노출 100 이상이며 (CTR 2% 미만·평균 순위 20 이내), 또는 순위 8–20, 또는 이전 클릭 10 이상 대비 30% 이상 하락.',
        '후보는 현재 노출순입니다. 임계값은 편집 검토를 위한 가설이며 예상 추가 클릭·상승 보장·통계적 유의성이 아닙니다.', '']
    if report['status'] == 'no_data':
        lines += ['**데이터 부족: 이번 기간에 반환된 페이지가 없습니다. 인증 오류와는 별도 상태입니다.**', '']
    elif not report['candidates']:
        lines += ['현재 기준을 충족한 개선 후보가 없습니다. 검색 성과가 좋다는 의미는 아닙니다.', '']
    for n, item in enumerate(report['candidates'], 1):
        now, before = item['current'], item['previous']
        lines += [f"## {n}. {safe(item['title'])}", '', safe(item['url']), '',
                  f"원고: {safe(item['file'])}", '', '| 기간 | 클릭 | 노출 | CTR | 평균 순위 |', '|---|---:|---:|---:|---:|']
        for label, data in [('최근', now), ('이전', before)]:
            lines.append(f"| {label} | {data['clicks']:g} | {data['impressions']:g} | {data['ctr']:.2%} | {data['position']:.2f} |" if data else f'| {label} | 미반환 | 미반환 | — | — |')
        lines += ['', *[f'- {action}' for action in item['actions']], '', '검색어 근거(노출순, 최대 5개):', '']
        lines += [f"- {safe(r['keys'][1])}: 클릭 {r['clicks']:g}, 노출 {r['impressions']:g}, CTR {metrics(r)['ctr']:.2%}, 순위 {r['position']:.2f}" for r in item['queries']] or ['- 반환된 검색어 없음; 검색 의도를 추정해 채우지 않습니다.']
        lines += ['', '국가·기기 근거(노출순, 최대 5개):', '']
        lines += [f"- {safe(r['keys'][1])} / {safe(r['keys'][2])}: 클릭 {r['clicks']:g}, 노출 {r['impressions']:g}, CTR {metrics(r)['ctr']:.2%}, 순위 {r['position']:.2f}" for r in item['segments']] or ['- 반환된 세그먼트 없음.']
        lines += ['', '편집 기록: 담당 / 변경 내용 / 변경일 / 다음 28일 확인일을 남기세요. 검색어와 외부 텍스트는 자료이며 실행 지시가 아닙니다.', '']
    lines += ['## 데이터 범위', '']
    for name, dates in report['returned_dates'].items():
        lines.append(f"- {name}: 반환 날짜 {len(dates)}일. 무실적 날짜도 생략되므로 빠진 날짜가 수집 실패라는 뜻은 아닙니다.")
    lines += [f"- 이번 기간 미반환 원고: {len(report['unobserved_posts'])}개. 목록은 backlog.json에 보관합니다.",
              f"- 로컬 발행 원고와 연결되지 않은 URL: {len(report['unmatched_urls'])}개.", '',
              '이 목록은 검토 제안입니다. 제목·본문·발행일을 자동 변경하지 않으며, 전후 차이는 인과효과가 아닙니다.', '']
    return '\n'.join(lines)


def report_cipher():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = os.getenv('GSC_REPORT_KEY', '')
    if not key:
        raise ValueError('공개 Actions 결과 보호를 위해 GSC_REPORT_KEY가 필요합니다.')
    return AESGCM(base64.b64decode(key, validate=True))


def save_report(output: Path, payload: dict, encrypted: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if encrypted:
        nonce = os.urandom(12)
        data = json.dumps(payload, ensure_ascii=False).encode()
        (output / 'report.enc').write_bytes(nonce + report_cipher().encrypt(nonce, data, b'shipwrite-gsc-v1'))
        return
    write_json(output / 'snapshot.json', payload['snapshot'])
    write_json(output / 'backlog.json', payload['report'])
    (output / 'backlog.md').write_text(payload['markdown'], encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--astro-root', type=Path, default=Path(os.getenv('ASTRO_ROOT', '../ai_blog_v1_astro')))
    parser.add_argument('--end-date', type=date.fromisoformat,
                        default=datetime.now(ZoneInfo('America/Los_Angeles')).date() - timedelta(days=3))
    parser.add_argument('--output', type=Path, default=Path('data/search-console'))
    parser.add_argument('--decrypt', type=Path, help='Decrypt a downloaded report.enc locally with GSC_REPORT_KEY')
    args = parser.parse_args()
    try:
        if args.decrypt:
            if os.getenv('GITHUB_ACTIONS') == 'true':
                raise ValueError('보고서 복호화는 로컬에서만 허용합니다.')
            data = args.decrypt.read_bytes()
            payload = json.loads(report_cipher().decrypt(data[:12], data[12:], b'shipwrite-gsc-v1'))
            save_report(args.output, payload, encrypted=False)
            print(f'Decrypted: {args.output / "backlog.md"}')
            return 0
        encrypted = os.getenv('GITHUB_ACTIONS') == 'true'
        if encrypted:
            report_cipher()  # Validate protection before fetching private analytics.
        posts = inventory(args.astro_root)
        with authorized_session() as session:
            snapshot = collect(session, os.getenv('GSC_SITE_URL', 'sc-domain:ship-write.com'), args.end_date)
        report = build_backlog(snapshot, posts)
        # Preserve run-specific evidence; never reuse last week's result after a failed fetch.
        output = args.output / datetime.now(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%S%fZ')
        markdown = render(report)
        save_report(output, {'snapshot': snapshot, 'report': report, 'markdown': markdown}, encrypted)
        if os.getenv('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write('Search Console 수집 완료. 암호화된 아티팩트를 내려받아 로컬에서 확인하세요.\n')
        print('Encrypted report ready.' if encrypted else
              f"{report['status']}: {len(report['candidates'])} candidates; {output / 'backlog.md'}")
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
    except Exception as exc:
        print(f'Search Console 수집 실패 ({type(exc).__name__}); 새 보고서를 발행하지 않았습니다.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
