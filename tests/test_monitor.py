import unittest
from unittest.mock import Mock
import requests
import monitor


def response(body, url='https://example.org/jobs'):
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result._content = body.encode()
    result.encoding = 'utf-8'
    return result


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(name='测试单位', group='测试', url='https://example.org/jobs', kind='recruitment')

    def test_public_dynamic_list_and_cross_domain_allowlist(self):
        session = Mock()
        session.get.side_effect = [
            response('<script querydata="{\'pageId\':\'jobs\'}" url="/api/list"></script>'),
            response('{"success":true,"data":{"html":"<a href=\\"/job1\\">测试单位2027年公开招聘公告</a><a href=\\"https://evil.org/x\\">测试单位2027年公开招聘公告</a>"}}'),
        ]
        items = monitor.extract({**self.source, 'adapter': 'cnas'}, session)
        self.assertEqual([x['url'] for x in items], ['https://example.org/job1'])
        self.assertEqual(session.get.call_args.kwargs['params'], {'pageId': 'jobs'})

    def test_defense_page_is_not_success_even_with_http_200(self):
        session = Mock()
        session.get.return_value = response('<title>请稍候... | 云防护</title>')
        items, status = monitor.scan(self.source, session, {})
        self.assertFalse(status['ok'])
        self.assertEqual(items, [])
        self.assertIn('访问防护', status['error'])

    def test_fallback_filters_other_units_and_keeps_primary_error(self):
        session = Mock()
        session.get.side_effect = [requests.Timeout(), response('<a href="/one">甲单位2027年公开招聘公告</a><a href="/two">乙单位2027年公开招聘公告</a>')]
        items, status = monitor.scan({**self.source, 'fallbacks': [dict(url='https://example.org/fallback', title_filter='甲单位')]}, session, {})
        self.assertEqual(len(items), 1)
        self.assertIn('甲单位', items[0]['title'])
        self.assertEqual(status['health'], 'warning')
        self.assertTrue(status['fallback'])
        self.assertIn('超时', status['attempts'][0]['error'])

    def test_stable_url_content_changes_notify_but_baseline_does_not(self):
        source = {**self.source, 'content_selector': '.jobs'}
        session = Mock()
        session.get.return_value = response('<div class="jobs"><h1>测试单位公开招聘公告</h1>' + '计算机岗位任职要求与报名方式。' * 10 + '</div>')
        original = monitor.extract(source, session)[0]
        known = {}
        self.assertEqual(monitor.merge_items(known, [original], 'first', True), [])
        session.get.return_value = response('<div class="jobs"><h1>测试单位公开招聘公告</h1>' + '软件岗位任职要求与报名方式。' * 10 + '</div>')
        changed = monitor.extract(source, session)[0]
        notified = monitor.merge_items(known, [changed], 'second', False)
        self.assertEqual(notified[0]['event'], '招聘正文更新')
        self.assertEqual(monitor.merge_items(known, [changed], 'third', False), [])
        self.assertEqual(monitor.merge_items(known, [original], 'fourth', True), [])

    def test_recovery_and_endpoint_changes_require_baseline(self):
        current = dict(ok=True, config_hash='a', effective_url='https://example.org/jobs')
        self.assertFalse(monitor.needs_baseline(current, current))
        self.assertTrue(monitor.needs_baseline({**current, 'ok': False}, current))
        self.assertTrue(monitor.needs_baseline({**current, 'config_hash': 'old'}, current))
        self.assertTrue(monitor.needs_baseline({**current, 'effective_url': 'other'}, current))
        self.assertTrue(monitor.needs_baseline(current, current, True))
        # A failed cloud run must not erase the successful local collector baseline.
        failed_cloud = dict(ok=False, last_success=current)
        self.assertFalse(monitor.needs_baseline(failed_cloud, current))
        self.assertTrue(monitor.needs_baseline(failed_cloud, {**current, 'config_hash': 'changed'}))

    def test_empty_list_and_homepage_are_warnings(self):
        session = Mock()
        session.get.return_value = response('<title>招聘栏目</title><p>暂时没有内容</p>')
        _, status = monitor.scan(self.source, session, {})
        self.assertTrue(status['ok'])
        self.assertEqual(status['health'], 'warning')
        self.assertIn('不代表没有招聘', monitor.page([], [status], 'now'))

    def test_curl_transport_retains_certificate_verification(self):
        from unittest.mock import patch
        body = '<a href="/one">测试单位2027年公开招聘公告</a>'.encode()
        result = Mock(returncode=0, stdout=body + b'\nhttps://example.org/jobs\n200')
        with patch('monitor.subprocess.run', return_value=result) as run:
            items = monitor.extract({**self.source, 'transport': 'curl'}, Mock())
        self.assertEqual(len(items), 1)
        command = run.call_args.args[0]
        self.assertNotIn('--insecure', command)
        self.assertNotIn('-k', command)
        self.assertIn('--max-time', command)

    def test_readable_homepage_is_not_misreported_when_fallback_fails(self):
        session = Mock()
        session.get.side_effect = [response('<title>官网</title>'), requests.Timeout()]
        _, status = monitor.scan({**self.source, 'fallbacks': [dict(url='https://example.org/alternate')]}, session, {})
        self.assertTrue(status['ok'])
        self.assertEqual(status['health'], 'warning')
        self.assertEqual(len(status['attempts']), 2)


if __name__ == '__main__':
    unittest.main()
