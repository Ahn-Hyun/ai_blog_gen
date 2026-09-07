import copy
import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import search_console_backlog as gsc


def row(keys, clicks=10, impressions=1000, position=12):
    return dict(keys=keys, clicks=clicks, impressions=impressions,
                ctr=clicks/impressions if impressions else 0, position=position)


class SearchConsoleChecks(unittest.TestCase):
    def test_public_ci_artifact_is_authenticated_encrypted_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {
                'GSC_REPORT_KEY': base64.b64encode(os.urandom(32)).decode()}):
            output = Path(tmp)
            payload = {'snapshot': {'private': 'query'}, 'report': {}, 'markdown': 'private search data'}
            gsc.save_report(output, payload, encrypted=True)
            self.assertEqual([p.name for p in output.iterdir()], ['report.enc'])
            data = (output / 'report.enc').read_bytes()
            self.assertNotIn(b'private search data', data)
            self.assertEqual(json.loads(gsc.report_cipher().decrypt(data[:12], data[12:], b'shipwrite-gsc-v1')), payload)
            with self.assertRaises(Exception):
                gsc.report_cipher().decrypt(data[:12], data[12:-1] + bytes([data[-1] ^ 1]), b'shipwrite-gsc-v1')

    def test_windows_are_inclusive_contiguous_and_leap_safe(self):
        periods = gsc.windows(date(2024, 3, 1))
        self.assertEqual(periods['current'], {'startDate': '2024-02-03', 'endDate': '2024-03-01'})
        self.assertEqual(periods['previous'], {'startDate': '2024-01-06', 'endDate': '2024-02-02'})

    def test_pagination_filters_and_retry_keep_all_rows(self):
        session = Mock()
        session.request.side_effect = [Mock(status_code=503),
            Mock(status_code=200, json=lambda: {'rows': [row(['a']), row(['b'])]}),
            Mock(status_code=200, json=lambda: {'rows': [row(['c'])]})]
        with patch.object(gsc, 'PAGE_LIMIT', 2), patch.object(gsc.time, 'sleep'):
            rows = gsc.fetch_rows(session, 'sc-domain:ship-write.com', gsc.windows(date(2026, 9, 3))['current'], ['page'])
        self.assertEqual(len(rows), 3)
        body = session.request.call_args.kwargs['json']
        self.assertEqual(body['startRow'], 2)
        self.assertEqual(body['dataState'], 'final')
        self.assertIn('sc-domain%3Aship-write.com', session.request.call_args.args[1])
        self.assertEqual(body['dimensionFilterGroups'][0]['filters'][0]['expression'], r'^https://ship-write\.com/blog/')

    def test_auth_and_permission_errors_never_become_empty_traffic(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(ValueError, '인증 필요'):
            gsc.authorized_session()
        session = Mock()
        session.request.return_value = Mock(status_code=403)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            gsc.collect(session, 'sc-domain:ship-write.com', date(2026, 9, 3))
        session.request.return_value = Mock(status_code=200, json=lambda: {'siteEntry': []})
        with self.assertRaisesRegex(ValueError, '접근할 수 없습니다'):
            gsc.collect(session, 'sc-domain:ship-write.com', date(2026, 9, 3))
        with self.assertRaisesRegex(ValueError, '영어'):
            gsc.collect(session, 'sc-domain:unrelated.com', date(2026, 9, 3))

    def test_report_uses_page_totals_preserves_missing_and_escapes_queries(self):
        url = 'https://ship-write.com/blog/example'
        posts = {'/blog/example': {'title': 'Example', 'url': url, 'file': 'src/content/blog/example.mdx'},
                 '/blog/absent': {'url': 'https://ship-write.com/blog/absent'}}
        current = {'pages': [row([url])],
                   'queries': [row([url, '<script>|query'], 1, 20)],
                   'segments': [row([url, 'usa', 'MOBILE'], 3, 200)], 'dates': []}
        snapshot = {'periods': gsc.windows(date(2026, 9, 3)), 'data': {'current': current,
                    'previous': {k: [] for k in gsc.GROUPS}}}
        report = gsc.build_backlog(snapshot, posts)
        self.assertEqual(report['candidates'][0]['current']['clicks'], 10)
        self.assertIsNone(report['candidates'][0]['previous'])
        self.assertEqual(len(report['unobserved_posts']), 1)
        text = gsc.render(report)
        self.assertIn('미반환', text)
        self.assertIn('&lt;script&gt;&#124;query', text)
        self.assertNotIn('<script>', text)
        low = copy.deepcopy(snapshot)
        low['data']['current']['pages'] = [row([url], 0, 20)]
        self.assertEqual(gsc.build_backlog(low, posts)['candidates'], [])
        low['data']['current']['pages'] = []
        self.assertEqual(gsc.build_backlog(low, posts)['status'], 'no_data')
        snapshot['data']['previous']['pages'] = [row([url], 30, 500)]
        self.assertTrue(any('클릭 하락' in a for a in gsc.build_backlog(snapshot, posts)['candidates'][0]['actions']))

    def test_inventory_matches_astro_routes_despite_stale_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'src/content/blog'
            directory.mkdir(parents=True)
            (directory / 'ok.mdx').write_text('---\ntitle: "Okay"\ndraft: false\n---\nBody')
            (directory / 'held.mdx').write_text('---\ndraft: true\n---\ndraft: false\n')
            (directory / 'kr.mdx').write_text('---\ndraft: false\n  canonical: "https://kr.ship-write.com/blog/kr"\n---\n')
            posts = gsc.inventory(root)
            self.assertEqual(set(posts), {'/blog/ok', '/blog/kr'})
            self.assertEqual(posts['/blog/kr']['url'], 'https://ship-write.com/blog/kr')
        self.assertIsNone(gsc.page_key('https://ship-write.com.evil.test/blog/a'))
        self.assertIsNone(gsc.page_key('https://ship-write.com/blog/a?duplicate=1'))


if __name__ == '__main__':
    unittest.main()
