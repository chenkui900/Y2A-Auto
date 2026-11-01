# -*- coding: utf-8 -*-
"""「新建任务 + 导入 Excel」合并入口、以及设置页上传/下载限速控件的渲染测试。

需求：
1. 工具栏不再分「新建任务」「导入Excel」两个按钮，合并成「新建 / 导入任务」一个入口，
   弹窗里用标签页在「手动填写链接」和「Excel 批量导入」之间切换；
2. 两个表单仍各自提交到原来的路由，原有控件 id（youtube_url / excel_file /
   importExcelBtn 等）保持不变，避免打断既有 JS 与测试；
3. 设置页提供独立的「下载限速」「上传限速」输入框（旧键 YOUTUBE_THROTTLED_RATE
   不再出现在界面上）。
"""
import pathlib
import unittest

import app as web_app


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class AddTaskModalMergedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config.update(TESTING=True)
        cls.client = web_app.app.test_client()
        with cls.client.session_transaction() as session:
            session['logged_in'] = True
        cls.tasks_html = cls.client.get('/tasks').get_data(as_text=True)

    def test_single_toolbar_entry(self):
        html = self.tasks_html
        self.assertIn('新建 / 导入任务', html)
        self.assertIn('data-bs-target="#addTaskModal"', html)
        # 独立的「导入Excel」按钮和旧弹窗都应消失
        self.assertNotIn('id="importExcelModal"', html)
        self.assertNotIn('> 导入Excel</button>', html)

    def test_tabs_and_forms(self):
        html = self.tasks_html
        self.assertIn('id="addTaskManualPane"', html)
        self.assertIn('id="addTaskExcelPane"', html)
        self.assertIn('id="addTaskTabs"', html)
        # 两个表单各自提交到原路由
        self.assertIn('action="/tasks/add"', html)
        self.assertIn('action="/tasks/import-excel"', html)
        # 关键控件 id 保持不变
        for element_id in ('youtube_url', 'excel_file', 'importExcelBtn', 'upload_target', 'excel_upload_target'):
            self.assertIn(f'id="{element_id}"', html)

    def test_import_js_points_to_new_pane(self):
        html = self.tasks_html
        self.assertIn("document.querySelector('#addTaskExcelPane form')", html)
        self.assertNotIn("document.querySelector('#importExcelModal form')", html)


class SettingsRateLimitFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config.update(TESTING=True)
        cls.client = web_app.app.test_client()
        with cls.client.session_transaction() as session:
            session['logged_in'] = True
        response = cls.client.get('/settings')
        if response.status_code != 200:
            raise AssertionError(f'设置页渲染失败: {response.status_code}')
        cls.html = response.get_data(as_text=True)

    def test_upload_and_download_fields(self):
        html = self.html
        self.assertIn('name="DOWNLOAD_RATE_LIMIT"', html)
        self.assertIn('name="UPLOAD_RATE_LIMIT"', html)
        self.assertIn('下载限速', html)
        self.assertIn('上传限速', html)
        # 旧的下载限速控件已被替换
        self.assertNotIn('name="YOUTUBE_THROTTLED_RATE"', html)

    def test_server_side_normalizer_wired(self):
        source = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('normalize_rate_limit(_submitted_rate)', source)
        self.assertIn('invalidate_rate_limit_cache()', source)


if __name__ == '__main__':
    unittest.main()
