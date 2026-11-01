'use strict';
/*
 * 任务列表「跨页批量选择只允许同一状态」的运行时行为测试。
 *
 * 从渲染后的 /tasks 页面提取批量选择逻辑段（锚点见
 * tests/test_tasks_selection_same_status.py），放进 Node + 最小 DOM 桩里执行，
 * 真实跑一遍勾选 → 锁状态 → 拒绝异状态 → 清空解锁的完整流程。
 *
 * 用法：node tests/js/tasks_selection_same_status.js <提取出的JS文件>
 * 输出：单行 JSON（stdout），由对应的 Python 测试断言。
 */

const fs = require('fs');
const vm = require('vm');

class ClassList {
    constructor(owner) { this.owner = owner; this.items = new Set(); }
    add(...names) { names.forEach((n) => { if (n) { this.items.add(n); } }); }
    remove(...names) { names.forEach((n) => this.items.delete(n)); }
    contains(name) { return this.items.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this.items.has(name) : !!force;
        if (on) { this.items.add(name); } else { this.items.delete(name); }
        return on;
    }
    toString() { return Array.from(this.items).join(' '); }
}

class El {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.classList = new ClassList(this);
        this.attrs = new Map();
        this.id = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.title = '';
        this.style = {};
        this.dataset = {};
        this._text = '';
    }
    get textContent() { return this._text; }
    set textContent(value) { this._text = String(value); }
    get innerHTML() { return this._html || ''; }
    set innerHTML(value) { this._html = String(value); }
    setAttribute(key, value) { this.attrs.set(key, String(value)); if (key === 'id') { this.id = String(value); } }
    getAttribute(key) { return this.attrs.has(key) ? this.attrs.get(key) : null; }
    removeAttribute(key) { this.attrs.delete(key); }
    addEventListener() {}
    closest() { return null; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    remove() {}
}

function buildWorld() {
    // 三行任务：A/C 等待处理（pending），B 下载中（downloading）
    const makeBox = (id, status, label) => {
        const holder = new El('tr');
        holder.dataset.status = status;
        const cb = new El('input');
        cb.type = 'checkbox';
        cb.value = id;
        cb.dataset.taskStatus = status;
        cb.dataset.statusLabel = label;
        cb.closest = () => holder;
        cb.holder = holder;
        return cb;
    };
    const boxes = [
        makeBox('task-A', 'pending', '等待处理'),
        makeBox('task-B', 'downloading', '下载中'),
        makeBox('task-C', 'pending', '等待处理'),
    ];

    const byId = {};
    const reg = (id, tag) => { const el = new El(tag || 'span'); el.setAttribute('id', id); byId[id] = el; return el; };
    const selectAllEl = reg('selectAllTasks', 'input');
    reg('selectedCount');
    reg('selectionBar', 'div');
    reg('selCountNum');
    reg('selScopeHint');
    reg('selectAllFilteredBtn', 'button');
    reg('filterTotalNum');
    reg('selStatusLock');
    reg('selStatusLockLabel');
    reg('downloadPausedBanner', 'div');

    const storage = new Map();
    const alerts = [];

    const document = {
        getElementById: (id) => byId[id] || null,
        querySelector: () => null,
        querySelectorAll: (selector) => {
            const sel = String(selector);
            if (sel.indexOf('task-checkbox') !== -1) { return boxes.slice(); }
            return [];
        },
        createElement: (tag) => new El(tag),
        addEventListener() {},
    };

    const context = {
        document,
        window: { addEventListener() {}, location: { pathname: '/tasks' } },
        sessionStorage: {
            getItem: (k) => (storage.has(k) ? storage.get(k) : null),
            setItem: (k, v) => { storage.set(k, String(v)); },
            removeItem: (k) => { storage.delete(k); },
        },
        y2aAlert: (message, options) => { alerts.push({ message: String(message || ''), options: options || {} }); return Promise.resolve(); },
        syncTasksLayoutVars: () => {},
        fetch: () => new Promise(() => {}),
        URLSearchParams: class { constructor() {} set() {} get() { return null; } toString() { return ''; } },
        bootstrap: { Modal: { getInstance: () => null }, Tooltip: { getInstance: () => null } },
        console,
        Set, Map, RegExp, Math, JSON, String, Number, Object, Array, Error,
        Boolean, Date, Promise, parseInt, parseFloat, isNaN, isFinite,
        setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    };
    context.globalThis = context;
    return { context: vm.createContext(context), boxes, selectAllEl, alerts, storage };
}

async function runProbe(source) {
    const world = buildWorld();
    const probe = `
(async () => {
    const out = { alerts: [], sel: [], checks: {}, lockFlow: {}, persist: null, selectAllFlow: {} };
    const selectedNow = () => {
        const ids = (typeof getSelectedIds === 'function') ? getSelectedIds() : [];
        return ids;
    };
    const snapshot = () => {
        const boxes = document.querySelectorAll('.task-checkbox');
        return {
            sel: selectedNow(),
            count: getSelectedCount(),
            boxes: boxes.map(cb => ({ id: cb.value, checked: cb.checked, disabled: cb.disabled, title: cb.title })),
            status: (typeof selectionStatus !== 'undefined') ? selectionStatus : null,
            statusLabel: (typeof selectionStatusLabel !== 'undefined') ? selectionStatusLabel : null,
        };
    };

    // 1. 未勾选任何任务时点「跨页全选」：应提示先手动勾选一个任务
    await selectAllFiltered();

    // 2. 勾选等待处理任务 A → 锁定 pending
    const A = document.querySelectorAll('.task-checkbox')[0];
    const B = document.querySelectorAll('.task-checkbox')[1];
    const C = document.querySelectorAll('.task-checkbox')[2];
    A.checked = true;
    handleCheckboxChange(A);
    out.checks.afterA = snapshot();

    // 3. 试图勾选下载中任务 B → 被拒绝回退
    B.checked = true;
    handleCheckboxChange(B);
    out.checks.afterRejectB = snapshot();

    // 4. 勾选同为等待处理的 C → 允许
    C.checked = true;
    handleCheckboxChange(C);
    out.checks.afterC = snapshot();

    // 5. 取消 A → 仍锁定 pending
    A.checked = false;
    handleCheckboxChange(A);
    out.checks.afterUncheckA = snapshot();

    // 6. 取消 C → 全部清空，锁解除
    C.checked = false;
    handleCheckboxChange(C);
    out.checks.afterUncheckC = snapshot();

    // 7. 表头全选：本页混合状态，只勾选第一个任务的状态
    toggleAllTasks({ checked: true });
    out.selectAllFlow.afterToggleOn = snapshot();

    // 8. 表头取消全选 → 清空并解锁
    toggleAllTasks({ checked: false });
    out.selectAllFlow.afterToggleOff = snapshot();

    out.alerts = (typeof alerts !== 'undefined') ? alerts : [];
    try {
        out.persist = JSON.parse(sessionStorage.getItem('y2a_task_selection_v1') || 'null');
    } catch (e) { out.persist = String(e); }
    return out;
})()
`;
    world.context.globalThis.__alertsRef = world.alerts;
    world.context.alerts = world.alerts;
    const code = source + '\nglobalThis.__alerts = alerts;\n' +
        'globalThis.__probeResult = ' + probe + ';';
    vm.runInContext(code, world.context, { filename: 'tasks-selection.js' });
    return await world.context.globalThis.__probeResult;
}

module.exports = { runProbe };

if (require.main === module) {
    const sourcePath = process.argv[2];
    if (!sourcePath) {
        process.stderr.write('用法: node tasks_selection_same_status.js <提取出的JS文件>\n');
        process.exit(2);
    }
    const source = fs.readFileSync(sourcePath, 'utf8');
    runProbe(source).then((report) => {
        process.stdout.write(JSON.stringify(report, null, 2));
    }, (err) => {
        process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.stack ? err.stack : err) }, null, 2));
        process.exitCode = 1;
    });
}
