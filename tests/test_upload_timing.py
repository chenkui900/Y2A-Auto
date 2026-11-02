# -*- coding: utf-8 -*-
"""上传中任务「已耗时 / 剩余时间」展示的测试。

覆盖：
1. _human_duration 的人类可读时长格式化；
2. _upload_timing_info 的耗时与剩余估算（非上传中返回 None、速度缺失不给剩余）；
3. update_task 进入上传状态自动记录 upload_started_at、已在上传中不重置、离开上传状态清空；
4. init_db 的 upload_started_at 列迁移；
5. 任务行/卡片模板：上传中显示「已耗时/剩余约」并保留其他状态的「已下载比例」。
"""
import os
import pathlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from app import _human_duration, _upload_timing_info, app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK_ROW = REPO_ROOT / 'templates' / 'partials' / 'task_row.html'
TASK_CARD = REPO_ROOT / 'templates' / 'partials' / 'task_card.html'

GB = 1024 ** 3


class HumanDurationTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(_human_duration(0), '0 秒')
        self.assertEqual(_human_duration(45), '45 秒')
        self.assertEqual(_human_duration(-3), '0 秒')

    def test_minutes(self):
        self.assertEqual(_human_duration(60), '1 分钟')
        self.assertEqual(_human_duration(200), '3 分 20 秒')

    def test_hours_and_days(self):
        self.assertEqual(_human_duration(3600), '1 小时 0 分')
        self.assertEqual(_human_duration(7500), '2 小时 5 分')
        self.assertEqual(_human_duration(27 * 3600), '1 天 3 小时')

    def test_invalid(self):
        self.assertEqual(_human_duration(None), '')
        self.assertEqual(_human_duration('abc'), '')


class UploadTimingInfoTests(unittest.TestCase):
    def test_non_uploading_returns_none(self):
        self.assertIsNone(_upload_timing_info({'status': 'downloading'}))
        self.assertIsNone(_upload_timing_info({'status': 'completed'}))

    def test_uploading_without_start_gives_no_elapsed(self):
        info = _upload_timing_info({'status': 'uploading', 'upload_started_at': None})
        self.assertIsNotNone(info)
        self.assertIsNone(info['elapsed_seconds'])
        self.assertEqual(info['elapsed_text'], '')
        self.assertIsNone(info['start_epoch'])

    def test_elapsed_from_start_time(self):
        start = datetime.now() - timedelta(hours=1, minutes=5)
        info = _upload_timing_info({
            'status': 'uploading',
            'upload_started_at': start.strftime('%Y-%m-%d %H:%M:%S'),
        })
        self.assertEqual(info['elapsed_text'], '1 小时 5 分')
        self.assertIsNotNone(info['start_epoch'])

    def test_eta_from_size_speed_and_percent(self):
        start = datetime.now() - timedelta(seconds=30)
        info = _upload_timing_info({
            'status': 'uploading',
            'upload_started_at': start.strftime('%Y-%m-%d %H:%M:%S'),
            'total_file_size': int(10 * GB),
            'upload_progress': '50.0%',
            'progress_speed': 1 * GB,  # 1 GB/s → 剩 5GB → 5 秒
        })
        self.assertEqual(info['eta_text'], '5 秒')

    def test_no_speed_no_eta(self):
        start = datetime.now() - timedelta(seconds=30)
        info = _upload_timing_info({
            'status': 'uploading',
            'upload_started_at': start.strftime('%Y-%m-%d %H:%M:%S'),
            'total_file_size': int(10 * GB),
            'upload_progress': '50.0%',
            'progress_speed': None,
        })
        self.assertIsNone(info['eta_seconds'])
        self.assertEqual(info['eta_text'], '')

    def test_completed_percent_no_eta(self):
        start = datetime.now() - timedelta(seconds=30)
        info = _upload_timing_info({
            'status': 'uploading',
            'upload_started_at': start.strftime('%Y-%m-%d %H:%M:%S'),
            'total_file_size': int(10 * GB),
            'upload_progress': '100.0%',
            'progress_speed': 1 * GB,
        })
        self.assertIsNone(info['eta_seconds'])


class UploadStartedAtFieldTests(unittest.TestCase):
    def setUp(self):
        import modules.task_manager as tm
        self.tm = tm
        self._tmp = tempfile.mkdtemp(prefix='y2a_upload_timing_')
        self._old_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self._tmp, 'tasks.db')
        tm.init_db()
        self.task_id = 'test-upload-timing-0001'
        conn = sqlite3.connect(tm.DB_PATH)
        conn.execute(
            "INSERT INTO tasks (id, youtube_url, upload_target, status) VALUES (?, ?, ?, ?)",
            (self.task_id, 'https://www.youtube.com/watch?v=abc', 'acfun', tm.TASK_STATES['PENDING']),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tm.DB_PATH = self._old_db_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fetch_task(self):
        conn = sqlite3.connect(self.tm.DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (self.task_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def test_migration_adds_column(self):
        task = self._fetch_task()
        self.assertIn('upload_started_at', task)

    def test_entering_uploading_records_start_time(self):
        before = datetime.now().replace(microsecond=0)
        self.assertTrue(self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['UPLOADING']))
        task = self._fetch_task()
        self.assertIsNotNone(task['upload_started_at'])
        recorded = datetime.strptime(str(task['upload_started_at']), '%Y-%m-%d %H:%M:%S')
        self.assertGreaterEqual(recorded, before - timedelta(seconds=1))

    def test_progress_update_does_not_reset_start_time(self):
        self.assertTrue(self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['UPLOADING']))
        task1 = self._fetch_task()
        first_start = task1['upload_started_at']
        self.assertTrue(self.tm.update_task(
            self.task_id, upload_progress='42.0%', progress_speed=1024.0))
        task2 = self._fetch_task()
        self.assertEqual(task2['upload_started_at'], first_start)
        self.assertEqual(task2['status'], self.tm.TASK_STATES['UPLOADING'])

    def test_leaving_uploading_clears_start_time(self):
        self.assertTrue(self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['UPLOADING']))
        self.assertTrue(self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['COMPLETED']))
        task = self._fetch_task()
        self.assertIsNone(task['upload_started_at'])


class TemplateUploadTimingTests(unittest.TestCase):
    def test_task_row_has_upload_timing_and_download_ratio(self):
        html = TASK_ROW.read_text(encoding='utf-8')
        self.assertIn('data-upload-timing', html)
        self.assertIn('已耗时', html)
        self.assertIn('剩余约', html)
        self.assertIn('data-upload-eta-wrap', html)
        self.assertIn('data-start-epoch', html)
        self.assertIn('data-total-bytes', html)
        # 非上传中仍显示已下载比例
        self.assertIn('data-task-download', html)
        self.assertIn('已下载', html)

    def test_task_card_has_upload_timing(self):
        html = TASK_CARD.read_text(encoding='utf-8')
        self.assertIn('data-upload-timing', html)
        self.assertIn('已耗时', html)
        self.assertIn('data-upload-eta-wrap', html)
        self.assertIn('data-task-download', html)


class TasksPageScriptTests(unittest.TestCase):
    def test_tasks_html_has_ticker_and_eta_logic(self):
        html = (REPO_ROOT / 'templates' / 'tasks.html').read_text(encoding='utf-8')
        self.assertIn('function formatDuration', html)
        self.assertIn('function updateUploadTiming', html)
        self.assertIn('function updateUploadEta', html)
        self.assertIn('setInterval(updateUploadTiming, 1000)', html)
        self.assertIn("updateUploadEta(box, payload.progress_speed, etaPct)", html)


if __name__ == '__main__':
    unittest.main()
