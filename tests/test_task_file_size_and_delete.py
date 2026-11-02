# -*- coding: utf-8 -*-
"""任务列表「文件总大小 + 两个删除按钮」的测试。

覆盖：
1. _human_file_size 的人类可读格式化；
2. _task_total_file_size 的目录统计（含下载目录外视频文件、目录内不重复计）；
3. 任务行/卡片模板包含文件总大小展示与两个删除按钮（从数据库删除 / 删除任务和文件）。
"""
import pathlib
import tempfile
import unittest
from unittest import mock

from flask import render_template_string  # noqa: F401  (确保 Flask 可用)

from app import _human_file_size, _task_total_file_size, app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK_ROW = REPO_ROOT / 'templates' / 'partials' / 'task_row.html'
TASK_CARD = REPO_ROOT / 'templates' / 'partials' / 'task_card.html'

GB = 1024 ** 3


class HumanFileSizeTests(unittest.TestCase):
    def test_zero_and_invalid_are_empty(self):
        self.assertEqual(_human_file_size(0), '')
        self.assertEqual(_human_file_size(None), '')
        self.assertEqual(_human_file_size('abc'), '')
        self.assertEqual(_human_file_size(-5), '')

    def test_bytes_and_units(self):
        self.assertEqual(_human_file_size(512), '512 B')
        self.assertEqual(_human_file_size(2048), '2.0 KB')
        self.assertEqual(_human_file_size(5 * 1024 * 1024), '5.0 MB')
        self.assertEqual(_human_file_size(int(15.58 * GB)), '15.6 GB')
        self.assertEqual(_human_file_size(2.5 * 1024 * GB), '2.5 TB')


class TaskTotalFileSizeTests(unittest.TestCase):
    def _make_task(self, task_id, video_path_local=None):
        return {'id': task_id, 'video_path_local': video_path_local}

    def test_sums_all_files_in_task_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = pathlib.Path(tmp) / 'task-1'
            task_dir.mkdir()
            (task_dir / 'video.mp4').write_bytes(b'x' * 1000)
            (task_dir / 'cover.webp').write_bytes(b'y' * 200)
            sub = task_dir / 'sub'
            sub.mkdir()
            (sub / 'subtitle.srt').write_bytes(b'z' * 50)
            with mock.patch('modules.task_manager.DOWNLOADS_DIR', tmp):
                total = _task_total_file_size(self._make_task('task-1'))
            self.assertEqual(total, 1250)

    def test_missing_dir_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('modules.task_manager.DOWNLOADS_DIR', tmp):
                self.assertEqual(_task_total_file_size(self._make_task('nope')), 0)

    def test_video_outside_dir_is_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = pathlib.Path(tmp) / 'task-2'
            task_dir.mkdir()
            (task_dir / 'cover.webp').write_bytes(b'c' * 10)
            outside = pathlib.Path(tmp) / 'elsewhere.mp4'
            outside.write_bytes(b'v' * 500)
            with mock.patch('modules.task_manager.DOWNLOADS_DIR', tmp):
                total = _task_total_file_size(
                    self._make_task('task-2', video_path_local=str(outside)))
            self.assertEqual(total, 510)

    def test_video_inside_dir_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = pathlib.Path(tmp) / 'task-3'
            task_dir.mkdir()
            video = task_dir / 'video.mp4'
            video.write_bytes(b'v' * 700)
            with mock.patch('modules.task_manager.DOWNLOADS_DIR', tmp):
                total = _task_total_file_size(
                    self._make_task('task-3', video_path_local=str(video)))
            self.assertEqual(total, 700)

    def test_empty_task_id_is_zero(self):
        self.assertEqual(_task_total_file_size({'id': ''}), 0)


class TemplateElementTests(unittest.TestCase):
    def test_task_row_has_size_and_two_delete_buttons(self):
        html = TASK_ROW.read_text(encoding='utf-8')
        self.assertIn('文件总大小', html)
        self.assertIn('format_file_size', html)
        # 从数据库删除（保留文件）
        self.assertIn('bi-database-x', html)
        self.assertIn("name=\"delete_record\" value=\"true\"", html)
        self.assertIn("name=\"delete_files\" value=\"false\"", html)
        # 删除任务和文件
        self.assertIn("name=\"delete_files\" value=\"true\"", html)
        self.assertIn('无法恢复', html)
        self.assertEqual(html.count('delete_task_route'), 2)

    def test_task_card_has_size_and_two_delete_buttons(self):
        html = TASK_CARD.read_text(encoding='utf-8')
        self.assertIn('文件总大小', html)
        self.assertIn('从数据库删除', html)
        self.assertIn('删除任务和文件', html)
        self.assertIn("name=\"delete_files\" value=\"false\"", html)
        self.assertIn("name=\"delete_files\" value=\"true\"", html)

    def test_filter_registered(self):
        self.assertIn('format_file_size', app.jinja_env.filters)


if __name__ == '__main__':
    unittest.main()
