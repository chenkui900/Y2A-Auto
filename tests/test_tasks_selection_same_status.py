"""任务列表「跨页批量选择只允许同一状态」的运行时行为测试。

背景：跨页批量选择原本允许混合勾选任意状态的任务，批量操作会作用到
状态各异的混合集合上。本次改为：第一次勾选哪个任务，就把可选范围锁死
在该任务的状态上——其他状态的复选框禁用并淡化，跨页全选也按该状态过滤。

本测试从渲染后的 /tasks 页面提取批量选择逻辑段，放进 Node + 最小 DOM 桩
（tests/js/tasks_selection_same_status.js）里真实执行一遍流程，覆盖：
1. 未勾选任何任务时点「跨页全选」→ 提示先手动勾选（避免选到混合状态）；
2. 勾选任务 A 后锁定其状态，异状态任务 B 的复选框被禁用；
3. 程序化把 B 置为选中也会被回退并弹提示（防御兜底）；
4. 同状态任务可以继续累加、跨页保留（sessionStorage 里记录状态锁）；
5. 取消全部勾选后解锁，异状态复选框恢复可用；
6. 表头全选在本页混合状态时只勾选第一个任务的状态，其余跳过并提示。

Node 不在 PATH 上时自动跳过（与既有 tests/js 约定一致）。
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import app as web_app


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / 'tests' / 'js' / 'tasks_selection_same_status.js'

# 提取锚点：任务页批量选择逻辑段的起点/终点，改动模板时锚点应保持稳定。
SCRIPT_START = '// --- 批量操作逻辑（支持跨页选择） ---'
SCRIPT_END = 'function openBatchPartitionModal'


def extract_selection_script(page: str) -> str:
    try:
        start = page.index(SCRIPT_START)
        end = page.index(SCRIPT_END, start)
    except ValueError as exc:  # pragma: no cover - 只在模板被大改时触发
        raise AssertionError(f'无法从渲染页面提取批量选择脚本（模板结构可能已变）：{exc}') from exc
    return page[start:end]


@unittest.skipUnless(shutil.which('node'), '需要 node 运行时（不在 PATH 上时跳过）')
class TasksSelectionSameStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HARNESS.exists():  # pragma: no cover
            raise AssertionError(f'缺少 DOM 桩脚本: {HARNESS}')
        web_app.app.config.update(TESTING=True)
        client = web_app.app.test_client()
        with client.session_transaction() as session:
            session['logged_in'] = True
        response = client.get('/tasks')
        if response.status_code != 200:
            raise AssertionError(f'任务列表渲染失败: {response.status_code}')
        cls.script = extract_selection_script(response.get_data(as_text=True))

    @classmethod
    def tearDownClass(cls):
        pass

    def run_probe(self):
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as fh:
            fh.write(self.script)
            script_path = fh.name
        try:
            proc = subprocess.run(
                ['node', str(HARNESS), script_path],
                capture_output=True, text=True, timeout=60,
            )
        finally:
            pathlib.Path(script_path).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, f'harness 执行失败: {proc.stderr}')
        return json.loads(proc.stdout)

    def test_selection_lock_flow(self):
        report = self.run_probe()
        alerts = report['alerts']
        self.assertTrue(alerts, '应至少产生一次提示')

        # 1. 未勾选时点跨页全选 → 提示先手动勾选一个任务
        self.assertIn('请先手动勾选', alerts[0]['message'])

        # 2. 勾选 A（等待处理）后锁定 pending，B（下载中）被禁用
        after_a = report['checks']['afterA']
        self.assertEqual(after_a['sel'], ['task-A'])
        self.assertEqual(after_a['status'], 'pending')
        self.assertEqual(after_a['statusLabel'], '等待处理')
        boxes = {b['id']: b for b in after_a['boxes']}
        self.assertTrue(boxes['task-A']['checked'])
        self.assertTrue(boxes['task-B']['disabled'], '异状态复选框应被禁用')
        self.assertIn('只能选同一状态', boxes['task-B']['title'])
        self.assertFalse(boxes['task-C']['disabled'], '同状态复选框应保持可用')

        # 3. 程序化勾选 B 被回退
        after_reject = report['checks']['afterRejectB']
        self.assertEqual(after_reject['sel'], ['task-A'])
        boxes = {b['id']: b for b in after_reject['boxes']}
        self.assertFalse(boxes['task-B']['checked'])
        self.assertIn('只支持同一状态', alerts[1]['message'])

        # 4. 同状态 C 可以继续勾选
        after_c = report['checks']['afterC']
        self.assertEqual(sorted(after_c['sel']), ['task-A', 'task-C'])
        self.assertEqual(after_c['status'], 'pending')

        # 5. 取消 A 后仍锁定 pending（还剩 C）
        after_un_a = report['checks']['afterUncheckA']
        self.assertEqual(after_un_a['sel'], ['task-C'])
        self.assertEqual(after_un_a['status'], 'pending')

        # 6. 全部取消后解锁
        after_un_c = report['checks']['afterUncheckC']
        self.assertEqual(after_un_c['sel'], [])
        self.assertIsNone(after_un_c['status'])
        boxes = {b['id']: b for b in after_un_c['boxes']}
        self.assertFalse(boxes['task-B']['disabled'], '解锁后异状态复选框应恢复可用')

        # 7. 表头全选：混合状态页只勾选第一个任务的状态，并提示跳过数量
        toggle_on = report['selectAllFlow']['afterToggleOn']
        self.assertEqual(sorted(toggle_on['sel']), ['task-A', 'task-C'])
        self.assertEqual(toggle_on['status'], 'pending')
        boxes = {b['id']: b for b in toggle_on['boxes']}
        self.assertTrue(boxes['task-A']['checked'])
        self.assertFalse(boxes['task-B']['checked'])
        self.assertTrue(boxes['task-B']['disabled'])
        self.assertTrue(any('已跳过' in a['message'] for a in alerts[2:]))

        # 8. 取消全选后清空并解锁
        toggle_off = report['selectAllFlow']['afterToggleOff']
        self.assertEqual(toggle_off['sel'], [])
        self.assertIsNone(toggle_off['status'])
        boxes = {b['id']: b for b in toggle_off['boxes']}
        self.assertFalse(boxes['task-B']['disabled'])

        # 9. sessionStorage 持久化包含状态锁（最后一次操作后为空锁）
        self.assertIsNotNone(report['persist'])
        self.assertIsNone(report['persist'].get('status'))


if __name__ == '__main__':
    unittest.main()
