# -*- coding: utf-8 -*-
"""上传 / 下载分开调度 + 已下载任务自动转上传的守卫测试。

背景：重启前正在上传的任务会被「断点续跑」一律恢复成 pending，之后和下载
任务一起抢 ``MAX_CONCURRENT_TASKS``；下载配额一满，它就长期显示「等待处理」，
下载比例明明 100% 却永远等不到上传。修复后：
1. 上传中断的任务恢复为 ready_for_upload，直接进上传队列（独立配额）；
2. 已下载完成、上传前流水线也跑完的 pending 任务，会被自动升格进上传队列；
3. 只下载完、还没翻译/标签/分区的任务仍然回 pending 继续流水线，不能直接传。

覆盖点：状态判定函数、断点恢复落点、启动/扫描时的升格、以及调度入口的早分流。
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import modules.task_manager as tm

VIDEO_STAGES = [
    'fetch_info', 'translate_content', 'generate_tags',
    'recommend_partition', 'download_video',
]


class UploadQueueSeparationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='y2a_upload_sep_')
        self._old_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self._tmp, 'tasks.db')
        tm.init_db()
        self.video_path = os.path.join(self._tmp, 'video.mp4')
        with open(self.video_path, 'wb') as fh:
            fh.write(b'fake video')

    def tearDown(self):
        tm.DB_PATH = self._old_db_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---------- 工具 ----------

    def insert_task(self, task_id, status, **overrides):
        fields = {
            'id': task_id,
            'youtube_url': 'https://www.youtube.com/watch?v=' + task_id,
            'upload_target': 'bilibili',
            'status': status,
            'video_path_local': self.video_path,
            'download_progress': 1.0,
            'video_title_original': '原标题',
            'video_title_translated': '翻译标题',
            'tags_generated': '[]',
            'recommended_partition_id_bilibili': '267',
            'pipeline_checkpoint': json.dumps({'version': 1, 'completed': VIDEO_STAGES}, ensure_ascii=False),
        }
        fields.update(overrides)
        columns = ', '.join(fields.keys())
        placeholders = ', '.join(['?'] * len(fields))
        conn = sqlite3.connect(tm.DB_PATH)
        try:
            conn.execute(f'INSERT INTO tasks ({columns}) VALUES ({placeholders})', tuple(fields.values()))
            conn.commit()
        finally:
            conn.close()

    def fetch(self, task_id, columns='status, upload_progress, download_progress, preempted, queue_priority'):
        conn = sqlite3.connect(tm.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(f'SELECT {columns} FROM tasks WHERE id = ?', (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ---------- task_can_enter_upload_queue ----------

    def test_completed_download_and_pipeline_can_upload(self):
        task = {
            'status': tm.TASK_STATES['PENDING'],
            'video_path_local': self.video_path,
            'download_progress': 1.0,
            'video_title_original': '标题',
            'video_title_translated': 'translated',
            'tags_generated': '[]',
            'recommended_partition_id_bilibili': '267',
            'pipeline_checkpoint': json.dumps({'version': 1, 'completed': VIDEO_STAGES}),
        }
        self.assertTrue(tm.task_can_enter_upload_queue(task, {}))

    def test_missing_translation_stage_blocks_upload(self):
        task = {
            'status': tm.TASK_STATES['PENDING'],
            'video_path_local': self.video_path,
            'download_progress': 1.0,
            'video_title_original': '标题',
            'pipeline_checkpoint': json.dumps({'version': 1, 'completed': ['fetch_info', 'download_video']}),
        }
        self.assertFalse(tm.task_can_enter_upload_queue(task, {}))

    def test_missing_video_file_blocks_upload(self):
        task = {
            'status': tm.TASK_STATES['PENDING'],
            'video_path_local': os.path.join(self._tmp, 'not-exists.mp4'),
            'download_progress': 1.0,
            'pipeline_checkpoint': json.dumps({'version': 1, 'completed': VIDEO_STAGES}),
        }
        self.assertFalse(tm.task_can_enter_upload_queue(task, {}))

    def test_already_uploaded_task_not_requeued(self):
        task = {
            'status': tm.TASK_STATES['PENDING'],
            'video_path_local': self.video_path,
            'download_progress': 1.0,
            'upload_target': 'bilibili',
            'bilibili_upload_response': '{"bvid":"BV1xx"}',
            'pipeline_checkpoint': json.dumps({'version': 1, 'completed': VIDEO_STAGES}),
        }
        self.assertFalse(tm.task_can_enter_upload_queue(task, {}))

    def test_uploading_task_always_goes_back_to_queue(self):
        task = {
            'status': tm.TASK_STATES['UPLOADING'],
            'video_path_local': self.video_path,
            'download_progress': 1.0,
            'pipeline_checkpoint': json.dumps({'version': 1, 'completed': []}),
        }
        self.assertTrue(tm.task_can_enter_upload_queue(task, {}))

    # ---------- 断点恢复 ----------

    def test_recover_uploading_task_lands_in_upload_queue(self):
        self.insert_task('uploading-task', tm.TASK_STATES['UPLOADING'])
        tm.recover_interrupted_tasks_to_pending()
        row = self.fetch('uploading-task')
        self.assertEqual(row['status'], tm.TASK_STATES['READY_FOR_UPLOAD'])
        self.assertEqual(row['upload_progress'], tm.QUEUE_WAITING_MARK)
        self.assertEqual(row['preempted'], 0)

    def test_recover_partially_downloaded_task_stays_pending(self):
        self.insert_task(
            'downloading-task', tm.TASK_STATES['DOWNLOADING'],
            video_path_local='', download_progress=0.42,
            pipeline_checkpoint=json.dumps({'version': 1, 'completed': ['fetch_info']}),
        )
        tm.recover_interrupted_tasks_to_pending()
        row = self.fetch('downloading-task')
        self.assertEqual(row['status'], tm.TASK_STATES['PENDING'])
        self.assertAlmostEqual(row['download_progress'], 0.42, places=3)

    # ---------- 自动升格 ----------

    def test_promote_downloaded_pending_to_upload_queue(self):
        self.insert_task('ready-pending', tm.TASK_STATES['PENDING'])
        with mock.patch.object(tm, 'pump_upload_queue', return_value=0) as pump:
            promoted = tm.promote_downloaded_pending_tasks({})
        self.assertEqual(promoted, 1)
        row = self.fetch('ready-pending')
        self.assertEqual(row['status'], tm.TASK_STATES['READY_FOR_UPLOAD'])
        self.assertEqual(row['upload_progress'], tm.QUEUE_WAITING_MARK)
        pump.assert_called_once()

    def test_promote_skips_task_without_pipeline_stages(self):
        self.insert_task(
            'half-pending', tm.TASK_STATES['PENDING'],
            video_path_local='', download_progress=0.1,
            pipeline_checkpoint=json.dumps({'version': 1, 'completed': []}),
        )
        with mock.patch.object(tm, 'pump_upload_queue', return_value=0):
            promoted = tm.promote_downloaded_pending_tasks({})
        self.assertEqual(promoted, 0)
        self.assertEqual(self.fetch('half-pending')['status'], tm.TASK_STATES['PENDING'])

    # ---------- 调度入口 / 扫描器接入 ----------

    def _manager_source(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'modules', 'task_manager.py')
        with open(path, encoding='utf-8') as fh:
            return fh.read()

    def test_start_task_short_circuits_upload_only_tasks(self):
        source = self._manager_source()
        start_block = source.split('def start_task(task_id, config=None):', 1)[1].split('def force_upload_task', 1)[0]
        self.assertIn('task_can_enter_upload_queue(task, config)', start_block)
        # 早分流必须发生在抢下载配额（schedule_task）之前
        self.assertLess(start_block.index('task_can_enter_upload_queue(task, config)'),
                        start_block.index('processor.schedule_task(task_id)'))

    def test_scanner_promotes_before_pump(self):
        source = self._manager_source()
        scan_block = source.split('def _check_and_start_next_pending_task(self):', 1)[1].split('def ', 1)[0]
        self.assertIn('promote_downloaded_pending_tasks', scan_block)
        self.assertLess(scan_block.index('promote_downloaded_pending_tasks'),
                        scan_block.index("pump_upload_queue('定时扫描')"))


if __name__ == '__main__':
    unittest.main()
