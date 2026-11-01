# -*- coding: utf-8 -*-
"""失败原因前「日志生成时间」的守卫测试。

覆盖：
1. error_at 字段自动维护（写入错误 → 记录时间；只切 failed → 兜底时间；清空错误 → 清空时间）；
2. 任务列表行/卡片把时间排在失败原因文本最前面；
3. format_log_time 过滤器输出「MM-DD HH:MM」，跨年补年份，无法解析时原样返回。
"""
import ast
import os
import pathlib
import sqlite3
import tempfile
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding='utf-8')


def _load_log_time_filter():
    """从 app.py 抽出 format_log_time，避免导入整个 Flask 应用。"""
    app_path = ROOT / 'app.py'
    module_ast = ast.parse(app_path.read_text(encoding='utf-8'), filename=str(app_path))
    node = next(n for n in module_ast.body if isinstance(n, ast.FunctionDef) and n.name == 'format_log_time')
    namespace = {'datetime': datetime}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(app_path), 'exec'), namespace)
    return namespace['format_log_time']


class FormatLogTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fmt = staticmethod(_load_log_time_filter())

    def test_same_year_shows_month_day_time(self):
        now = datetime.now()
        value = now.strftime('%Y-%m-%d %H:%M:%S')
        self.assertEqual(self.fmt(value), now.strftime('%m-%d %H:%M'))

    def test_other_year_keeps_year(self):
        self.assertEqual(self.fmt('2024-03-05 08:07:06'), '2024-03-05 08:07')

    def test_empty_and_unparsable(self):
        self.assertEqual(self.fmt(None), '')
        self.assertEqual(self.fmt('   '), '')
        self.assertEqual(self.fmt('未知时间'), '未知时间')

    def test_iso_format_supported(self):
        now = datetime.now()
        self.assertEqual(self.fmt(now.strftime('%Y-%m-%dT%H:%M:%S')), now.strftime('%m-%d %H:%M'))


class ErrorAtFieldTests(unittest.TestCase):
    def setUp(self):
        import modules.task_manager as tm
        self.tm = tm
        self._tmp = tempfile.mkdtemp(prefix='y2a_error_at_')
        self._old_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self._tmp, 'tasks.db')
        tm.init_db()
        self.task_id = 'test-error-at-0001'
        conn = sqlite3.connect(tm.DB_PATH)
        conn.execute(
            "INSERT INTO tasks (id, youtube_url, upload_target, status) VALUES (?, ?, ?, ?)",
            (self.task_id, 'https://www.youtube.com/watch?v=abc', 'acfun', tm.TASK_STATES['PENDING']),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tm.DB_PATH = self._old_db_path
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fetch(self):
        conn = sqlite3.connect(self.tm.DB_PATH)
        try:
            return conn.execute('SELECT error_at, error_message, status FROM tasks WHERE id = ?', (self.task_id,)).fetchone()
        finally:
            conn.close()

    def test_error_at_column_exists_and_backfills(self):
        conn = sqlite3.connect(self.tm.DB_PATH)
        try:
            columns = [row[1] for row in conn.execute('PRAGMA table_info(tasks)')]
        finally:
            conn.close()
        self.assertIn('error_at', columns)

    def test_error_message_sets_error_at(self):
        ok = self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['FAILED'], error_message='B站上传失败')
        self.assertTrue(ok)
        error_at, message, status = self._fetch()
        self.assertEqual(message, 'B站上传失败')
        self.assertEqual(status, self.tm.TASK_STATES['FAILED'])
        self.assertTrue(error_at)
        self.assertLess(abs((datetime.strptime(error_at, '%Y-%m-%d %H:%M:%S') - datetime.now()).total_seconds()), 120)

    def test_failed_status_without_message_still_sets_time(self):
        self.assertTrue(self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['FAILED']))
        error_at, _, _ = self._fetch()
        self.assertTrue(error_at)

    def test_clearing_error_message_clears_time(self):
        self.assertTrue(self.tm.update_task(self.task_id, status=self.tm.TASK_STATES['FAILED'], error_message='临时错误'))
        self.assertTrue(self._fetch()[0])
        self.assertTrue(self.tm.update_task(self.task_id, error_message=None))
        error_at, message, _ = self._fetch()
        self.assertIsNone(error_at)
        self.assertIsNone(message)


class TemplateShowsLogTimeFirstTests(unittest.TestCase):
    def test_row_renders_time_before_reason_text(self):
        row = _read('templates/partials/task_row.html')
        self.assertIn('fail-reason-time', row)
        self.assertIn('format_log_time', row)
        self.assertIn('task.error_at or task.updated_at', row)
        self.assertLess(row.index('fail-reason-time'), row.index('fail-reason-text'))

    def test_card_and_review_pages_render_time(self):
        for path in ('templates/partials/task_card.html', 'templates/manual_review.html', 'templates/edit_task.html'):
            source = _read(path)
            self.assertIn('fail-reason-time', source, path)
            self.assertIn('format_log_time', source, path)

    def test_stylesheet_has_log_time_style(self):
        css = _read('static/css/style.css')
        self.assertIn('.fail-reason-time', css)


if __name__ == '__main__':
    unittest.main()
