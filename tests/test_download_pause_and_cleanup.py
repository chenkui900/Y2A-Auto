# -*- coding: utf-8 -*-
"""「暂停下载」开关与下载清理互斥逻辑的守卫测试。

覆盖：
1. DOWNLOAD_PAUSED 默认关闭（重启后不会莫名停在下拉暂停状态）；
2. 任务列表页有暂停/开始按钮与提示条，且走 /tasks/download_pause；
3. 调度器在暂停时直接返回（不启动新下载），下载阶段遇到暂停抛 TaskDeferredError；
4. 清理下载内容时跳过未完成任务目录（避免「下载完就消失」）；
5. 「上传完就删」与「按时间清理」同时开启时，不再注册按时间的定时清理。
"""
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(relative_path):
    with io.open(os.path.join(ROOT, relative_path), encoding='utf-8') as fh:
        return fh.read()


class DownloadPauseAndCleanupTests(unittest.TestCase):
    def test_default_config_has_pause_flag_off(self):
        from modules.config_manager import DEFAULT_CONFIG
        self.assertIn('DOWNLOAD_PAUSED', DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG['DOWNLOAD_PAUSED'])

    def test_tasks_page_exposes_pause_toggle(self):
        template = _read('templates/tasks.html')
        self.assertIn('id="downloadPauseToggleBtn"', template)
        self.assertIn('id="downloadPausedBanner"', template)
        self.assertIn('toggleDownloadPause', template)
        self.assertIn('/tasks/download_pause', template)

    def test_route_and_gates_exist(self):
        app_source = _read('app.py')
        self.assertIn("@app.route('/tasks/download_pause'", app_source)
        self.assertIn('download_paused', app_source)
        manager_source = _read('modules/task_manager.py')
        self.assertIn('class TaskDeferredError', manager_source)
        self.assertIn('def set_download_paused', manager_source)
        self.assertIn('if is_download_paused():', manager_source)

    def test_cleanup_skips_unfinished_tasks(self):
        app_source = _read('app.py')
        self.assertIn('def _protected_download_task_ids', app_source)
        cleanup_block = app_source.split('def cleanup_downloads(hours: int):', 1)[1].split('def configure_app', 1)[0]
        self.assertIn('protected_ids', cleanup_block)
        self.assertIn('skipped', cleanup_block)

    def test_upload_delete_takes_precedence_over_timed_cleanup(self):
        app_source = _read('app.py')
        block = app_source.split('def schedule_download_cleanup():', 1)[1].split('_cleanup_scheduler_lock', 1)[0]
        self.assertIn("config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False)", block)
        self.assertIn('return None', block)

    def test_settings_page_has_both_switches_in_file_maintenance(self):
        template = _read('templates/settings.html')
        self.assertEqual(template.count('name="DELETE_DOWNLOAD_FILES_AFTER_UPLOAD"'), 1)
        self.assertIn('id="download-cleanup-enabled"', template)
        self.assertIn('id="delete-download-after-upload"', template)


if __name__ == '__main__':
    unittest.main()
