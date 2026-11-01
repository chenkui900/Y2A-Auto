# -*- coding: utf-8 -*-
"""任务列表排序守卫：已下载比例 > 0 且未完成的任务从第 1 页起置顶、从大到小。

用临时库跑真实 SQL，不碰线上 db/tasks.db。
"""
import os
import tempfile
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RATIO_EXPR = ("COALESCE(download_progress, CASE WHEN video_path_local IS NOT NULL "
              "AND TRIM(video_path_local) != '' THEN 1.0 ELSE 0 END)")


class TaskListRatioOrderTests(unittest.TestCase):
    def setUp(self):
        from modules import task_manager as tm
        self.tm = tm
        self._orig_db = tm.DB_PATH
        tmp_dir = tempfile.mkdtemp()
        tm.DB_PATH = os.path.join(tmp_dir, 'tasks.db')
        tm.init_db()
        self.ids = {}

    def tearDown(self):
        self.tm.DB_PATH = self._orig_db

    def _add(self, name, status, ratio=None, video=None, queue_position=None, title=''):
        tid = str(uuid.uuid4())
        conn = self.tm.get_db_connection()
        conn.execute(
            "INSERT INTO tasks (id, youtube_url, status, created_at, updated_at, video_title_original, "
            "download_progress, video_path_local, queue_position, upload_target) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, f'https://youtu.be/{tid[:11]}', status, '2026-11-01 10:00:00', '2026-11-01 12:00:00',
             title or name, ratio, video, queue_position, 'acfun'),
        )
        conn.commit()
        conn.close()
        self.ids[tid[:8]] = name
        return tid

    def _order(self):
        data = self.tm.get_tasks_paginated(page=1, per_page=50)
        return [self.ids.get(t['id'][:8], '?') for t in data['tasks']]

    def test_ratio_tasks_promoted_after_upload_queue(self):
        self._add('uploading', 'uploading', 1.0, r'D:\dl\u.mp4')
        self._add('ready_1', 'ready_for_upload', 1.0, r'D:\dl\a.mp4', queue_position=1)
        self._add('ready_2', 'ready_for_upload', 1.0, r'D:\dl\b.mp4', queue_position=2)
        self._add('pending_90', 'pending', 0.9)
        self._add('downloading', 'downloading', 0.63)
        self._add('paused_55', 'paused', 0.55)
        self._add('pending_20', 'pending', 0.2)
        self._add('pending_0', 'pending', None)
        self._add('completed', 'completed', None)
        self._add('awaiting', 'awaiting_manual_review', None)

        self.assertEqual(
            self._order(),
            ['uploading', 'ready_1', 'ready_2', 'pending_90', 'downloading',
             'paused_55', 'pending_20', 'pending_0', 'awaiting', 'completed'],
        )

    def test_completed_always_last(self):
        # 已完成即使 100% 也不参与前面的排序，永远排在整张表最后
        for i in range(3):
            self._add(f'completed_{i}', 'completed', 1.0, rf'D:\dl\c{i}.mp4')
        self._add('pending_zero', 'pending', None)
        self._add('awaiting', 'awaiting_manual_review', None)
        order = self._order()
        self.assertEqual(order[:2], ['pending_zero', 'awaiting'])
        self.assertEqual(sorted(order[2:]), ['completed_0', 'completed_1', 'completed_2'])

    def test_completed_task_is_not_promoted_even_at_full_ratio(self):
        self._add('completed_full', 'completed', 1.0, r'D:\dl\c.mp4')
        self._add('pending_half', 'pending', 0.5)
        self.assertEqual(self._order(), ['pending_half', 'completed_full'])

    def test_video_without_ratio_counts_as_downloaded(self):
        self._add('pending_text', 'pending', None, r'D:\dl\x.mp4')
        self._add('pending_zero', 'pending', None)
        self.assertEqual(self._order(), ['pending_text', 'pending_zero'])

    def test_order_clause_uses_ratio_expression(self):
        import io
        with io.open(os.path.join(ROOT, 'modules', 'task_manager.py'), encoding='utf-8') as fh:
            source = fh.read()
        block = source.split('_ratio_expr = (', 1)[1].split('order_clause = f"""', 1)[0]
        self.assertIn('video_path_local', block)
        order_block = source.split('order_clause = f"""', 1)[1][:900]
        self.assertIn("TASK_STATES['COMPLETED']", order_block)


if __name__ == '__main__':
    unittest.main()
