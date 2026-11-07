import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import local_watch


class LocalWatchTests(unittest.TestCase):
    def setUp(self):
        self.items = [dict(url='https://example.org/a', title='测试招聘公告', source='测试来源')]

    def test_queue_is_durable_and_deduplicates_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / 'outbox'
            local_watch.queue_report(queue, self.items)
            local_watch.queue_report(queue, list(reversed(self.items)))
            files = list(queue.glob('*.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text()), self.items)
            self.assertEqual(list(queue.glob('*.tmp')), [])

    def test_pending_issue_is_removed_only_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            local_watch.queue_report(queue, self.items)
            with patch.object(local_watch, 'run', side_effect=['[]', RuntimeError('offline')]):
                with self.assertRaises(RuntimeError):
                    local_watch.notify_pending(queue)
            self.assertEqual(len(list(queue.glob('*.json'))), 1)
            marker = f'<!-- recruit-watch-local:{local_watch.batch_id(self.items)} -->'
            # A previous create succeeded before a crash: retry must not create a duplicate.
            with patch.object(local_watch, 'run', return_value=json.dumps([{'body': marker}])) as run:
                local_watch.notify_pending(queue)
                self.assertEqual(run.call_count, 1)
            self.assertEqual(list(queue.glob('*.json')), [])

    def test_content_updates_produce_distinct_batches(self):
        changed = [{**self.items[0], 'content_hash': 'new-content'}]
        self.assertNotEqual(local_watch.batch_id(self.items), local_watch.batch_id(changed))


if __name__ == '__main__':
    unittest.main()
