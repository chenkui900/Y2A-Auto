# -*- coding: utf-8 -*-
"""任务列表列宽调整与上传结果「完成」按钮的测试。

覆盖：
1. 列宽：更新时间/平台/分区收窄，操作列加宽到能放下 4 个按钮；
2. 上传结果：有投稿响应时渲染「完成」按钮（带 AC 号/BV号数据），点击弹窗显示详情；
3. tasks.html 中存在 uploadResultModal 与对应 JS 处理函数。
"""
import pathlib
import unittest

from flask import render_template

import app as web_app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TASKS_HTML = REPO_ROOT / 'templates' / 'tasks.html'
TASK_ROW = REPO_ROOT / 'templates' / 'partials' / 'task_row.html'

_BASE_TASK = {
    'id': 't1', 'status': 'completed',
    'video_title_translated': '测试标题', 'video_title_original': 'orig',
    'cover_path_local': '', 'total_file_size': 1024, 'upload_timing': None,
    'progress_speed': '', 'upload_progress': '', 'updated_at': '2026-11-02 10:00:00',
    'upload_target': 'both', 'selected_partition_id_acfun': 1,
    'recommended_partition_id_acfun': None,
    'selected_partition_id_bilibili': 2, 'recommended_partition_id_bilibili': None,
    'tags_generated': '["a"]', 'preempted': False,
    'acfun_upload_response': '', 'bilibili_upload_response': '',
    'error_message': '', 'error_at': None,
}


def _render_row(**overrides):
    task = dict(_BASE_TASK)
    task.update(overrides)
    with web_app.app.test_request_context('/tasks'):
        return render_template('partials/task_row.html', task=task, selected_task_ids=set())


class ColumnWidthTests(unittest.TestCase):
    def setUp(self):
        self.html = TASKS_HTML.read_text(encoding='utf-8')
        self.css = (REPO_ROOT / 'static' / 'css' / 'style.css').read_text(encoding='utf-8')

    def test_action_column_wide_enough_for_four_buttons(self):
        # 宽屏 / 中屏 / 窄屏三档，操作列都要 >= 156px（4 按钮 + 间距 + 内边距）
        import re
        block = re.search(r'const w = wide(.*?);\s*\n\s*// 切换表格', self.html, re.S).group(1)
        widths = [int(m) for m in re.findall(r'actions:\s*(\d+)', block)]
        self.assertEqual(len(widths), 3)
        for width in widths:
            self.assertGreaterEqual(width, 156)

    def test_time_platform_partition_narrowed(self):
        import re
        block = re.search(r'const w = wide(.*?);\s*\n\s*// 切换表格', self.html, re.S).group(1)
        for key, upper in (('time', 100), ('platform', 66), ('partition', 100)):
            values = [int(m) for m in re.findall(rf'{key}:\s*(\d+)', block)]
            self.assertEqual(len(values), 3)
            for value in values:
                self.assertLessEqual(value, upper, f'{key} 列应保持在 {upper}px 以内')

    def test_css_fallback_widths_updated(self):
        self.assertIn('.tasks-table th.col-time { width: 7%; }', self.css)
        self.assertIn('.tasks-table th.col-platform { width: 4%; }', self.css)
        self.assertIn('.tasks-table th.col-partition { width: 7%; }', self.css)
        self.assertIn('.tasks-table th.col-actions { width: 20%; }', self.css)


class UploadResultTriggerTests(unittest.TestCase):
    def test_completed_task_renders_done_button_with_data(self):
        html = _render_row(
            acfun_upload_response='{"ac_number": 123456, "title": "x"}',
            bilibili_upload_response='{"bvid": "BV1xx411c7mD", "aid": 1, "url": ""}',
        )
        self.assertIn('upload-result-trigger', html)
        self.assertIn('>完成</button>', html)
        self.assertIn('data-ac-number="123456"', html)
        self.assertIn('data-bvid="BV1xx411c7mD"', html)
        # url 为空时回退拼接 bilibili 链接
        self.assertIn('data-bili-url="https://www.bilibili.com/video/BV1xx411c7mD"', html)

    def test_task_without_response_renders_placeholder(self):
        html = _render_row()
        self.assertNotIn('upload-result-trigger', html)
        self.assertIn('<span class="text-muted small">-</span>', html)

    def test_modal_and_js_exist(self):
        html = TASKS_HTML.read_text(encoding='utf-8')
        self.assertIn('id="uploadResultModal"', html)
        self.assertIn('id="uploadResultBody"', html)
        self.assertIn('function showUploadResultModal', html)
        self.assertIn("'.upload-result-trigger'", html)


if __name__ == '__main__':
    unittest.main()
