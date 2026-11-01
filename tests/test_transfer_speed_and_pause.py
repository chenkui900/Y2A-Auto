# -*- coding: utf-8 -*-
"""传输速度展示 + 「暂停下载不影响上传」的守卫测试。

覆盖：
1. 任务列表里下载中显示 ↓ 速度、上传中显示 ↑ 速度（蓝色/紫色小标签都带 data 属性）；
2. 进度更新只带 upload_progress 时不会把刚算出的速度清掉，只有状态切到非传输阶段才清；
3. 暂停下载期间上传队列照常补位（ready_for_upload 会被启动），等待下载的任务不动。
"""
import os
import re
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TransferSpeedAndPauseTests(unittest.TestCase):
    def setUp(self):
        from modules import task_manager as tm
        self.tm = tm
        self._orig_db = tm.DB_PATH
        self._orig_paused = tm.is_download_paused()
        tmp_dir = tempfile.mkdtemp()
        tm.DB_PATH = os.path.join(tmp_dir, 'tasks.db')
        tm.init_db()
        tm.set_download_paused(False, persist=False)
        # 其他用例可能留下正在收尾的补位线程，全局补位锁被占用时 pump 会直接跳过；
        # 先等它释放，避免这条用例被无关的时序噪声打成失败。
        for _ in range(100):
            if not tm._QUEUE_PUMP_LOCK.locked():
                break
            time.sleep(0.05)

    def tearDown(self):
        self.tm.set_download_paused(self._orig_paused, persist=False)
        self.tm.DB_PATH = self._orig_db

    def _add(self, status, progress=None, speed=None, video=None):
        tid = str(uuid.uuid4())
        conn = self.tm.get_db_connection()
        conn.execute(
            "INSERT INTO tasks (id, youtube_url, status, created_at, updated_at, video_title_original, "
            "upload_progress, progress_speed, download_progress, video_path_local, upload_target) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f'https://youtu.be/{tid[:11]}', status, '2026-11-01 10:00:00', '2026-11-01 12:00:00',
             status, progress, speed, 0.635, video, 'bilibili'),
        )
        conn.commit()
        conn.close()
        return tid

    def test_download_and_upload_rows_show_speed(self):
        self._add('downloading', '63.5%', 3_600_000)
        self._add('uploading', '12.0%', 820_000)
        import app as web_app
        html = web_app.app.test_client().get('/tasks').get_data(as_text=True)
        self.assertIn('task-speed-down', html)
        self.assertIn('task-speed-up', html)
        self.assertIn('↓ 3.4 MB/s', html)
        self.assertIn('↑ 800.8 KB/s', html)

    def test_plain_progress_update_keeps_speed(self):
        tid = self._add('uploading', '12.0%', 820_000)
        self.tm.update_task(tid, upload_progress='13.0%', silent=True)
        task = self.tm.get_task(tid)
        self.assertAlmostEqual(task['progress_speed'], 820_000, delta=1)
        # 状态切到非传输阶段时才清空
        self.tm.update_task(tid, status='translating_subtitle', upload_progress='翻译中', silent=True)
        self.assertIsNone(self.tm.get_task(tid)['progress_speed'])

    def test_pause_does_not_block_upload_queue(self):
        ready = self._add('ready_for_upload', '0.0%', None, video=r'D:\dl\a.mp4')
        pending = self._add('pending')
        self.tm.set_download_paused(True, persist=False)

        started = []
        original = self.tm._run_queued_upload_job
        self.tm._run_queued_upload_job = lambda task_id: started.append(task_id)
        try:
            self.tm.pump_upload_queue('测试')
            deadline = time.time() + 5
            while not started and time.time() < deadline:
                time.sleep(0.05)
        finally:
            self.tm._run_queued_upload_job = original

        self.assertEqual(started, [ready], '暂停下载时上传队列仍必须补位')
        self.assertEqual(self.tm.get_task(pending)['status'], 'pending', '暂停时不应启动新的下载任务')
        self.assertTrue(self.tm.get_task(ready)['upload_progress'].startswith(self.tm.QUEUE_STARTING_MARK))

    def test_no_speed_chip_for_non_transfer_status(self):
        self._add('translating_subtitle', '翻译中', None, video=r'D:\dl\a.mp4')
        import app as web_app
        html = web_app.app.test_client().get('/tasks').get_data(as_text=True)
        chips = [t.strip() for t in re.findall(r'<span class="task-speed[^"]*"[^>]*>([^<]*)</span>', html)]
        self.assertTrue(chips, '应至少渲染出速度标签占位')
        self.assertTrue(all(not chip.strip('↑↓ ') for chip in chips), chips)


if __name__ == '__main__':
    unittest.main()
