# -*- coding: utf-8 -*-
"""任务列表「已下载比例」展示的守卫测试。

覆盖：
1. task_download_ratio 对任意状态都能给出 0~1 的值（已完成按 100% 算）；
2. 行/卡片模板不再按状态过滤，所有任务都渲染这一行；
3. 模板过滤器 download_ratio 已注册。
"""
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(relative_path):
    with io.open(os.path.join(ROOT, relative_path), encoding='utf-8') as fh:
        return fh.read()


class TaskDownloadRatioTests(unittest.TestCase):
    def _ratio(self, task):
        from modules.task_manager import task_download_ratio
        return task_download_ratio(task)

    def test_completed_task_without_stored_ratio_counts_as_full(self):
        # 老任务：download_progress 为空、本地文件也被删了 → 仍然显示 100%
        ratio = self._ratio({'status': 'completed', 'download_progress': None, 'video_path_local': None})
        self.assertAlmostEqual(ratio, 1.0)

    def test_downloading_task_uses_stored_ratio(self):
        ratio = self._ratio({'status': 'downloading', 'download_progress': 0.635, 'video_path_local': ''})
        self.assertAlmostEqual(ratio, 0.635)

    def test_downloaded_video_counts_as_full(self):
        ratio = self._ratio({'status': 'uploading', 'download_progress': None, 'video_path_local': r'D:\dl\a.mp4'})
        self.assertAlmostEqual(ratio, 1.0)

    def test_task_never_downloaded_is_zero(self):
        ratio = self._ratio({'status': 'awaiting_manual_review', 'download_progress': None, 'video_path_local': ''})
        self.assertAlmostEqual(ratio, 0.0)

    def test_legacy_text_percent_is_used_for_pending(self):
        ratio = self._ratio({'status': 'pending', 'download_progress': None, 'upload_progress': '63.5%'})
        self.assertAlmostEqual(ratio, 0.635)

    def test_upload_percent_is_not_mistaken_for_download(self):
        ratio = self._ratio({'status': 'uploading', 'download_progress': None, 'upload_progress': '42.0%'})
        self.assertAlmostEqual(ratio, 0.0)

    def test_none_task_is_safe(self):
        self.assertAlmostEqual(self._ratio(None), 0.0)

    def test_templates_always_render_ratio(self):
        for path in ('templates/partials/task_row.html', 'templates/partials/task_card.html'):
            source = _read(path)
            self.assertIn('data-task-download', source)
            block = source.split('data-task-download', 1)[0].splitlines()[-3:]
            joined = '\n'.join(block)
            self.assertNotIn("task.status in ['pending', 'paused']", joined)
            self.assertNotIn('task.download_progress', joined)

    def test_filter_registered(self):
        self.assertIn("filters['download_ratio']", _read('app.py'))


if __name__ == '__main__':
    unittest.main()
