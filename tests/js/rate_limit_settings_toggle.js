// 设置页「限速开关 + 速率单位 + 生效时间段」的运行期测试桩。
// 用最小 DOM 桩真实执行 static/js/main.js 中「设置页：上传/下载限速的总开关与生效时间段」
// 这一段逻辑，断言提示文案、控件禁用状态、隐藏域拼装结果与限速摊分提示。
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');

class ClassList {
    constructor() { this.items = new Set(); }
    add(...names) { names.forEach((n) => this.items.add(n)); }
    remove(...names) { names.forEach((n) => this.items.delete(n)); }
    contains(name) { return this.items.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this.items.has(name) : !!force;
        if (on) this.items.add(name); else this.items.delete(name);
        return on;
    }
    get value() { return Array.from(this.items).join(' '); }
}

class El {
    constructor(id, tag) {
        this.id = id;
        this.tagName = (tag || 'input').toUpperCase();
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.type = tag === 'div' ? 'div' : 'text';
        this.textContent = '';
        this.className = '';
        this.classList = new ClassList();
        this.dataset = {};
        this.listeners = {};
    }
    addEventListener(name, handler) { (this.listeners[name] = this.listeners[name] || []).push(handler); }
    setAttribute(name, value) { this[name] = value; }
    getAttribute(name) { return this[name]; }
}

const byId = {};
const reg = (id, tag) => {
    const el = new El(id, tag);
    byId[id] = el;
    return el;
};

const enabledToggle = reg('rate-limit-enabled');
enabledToggle.type = 'checkbox';
const scheduleToggle = reg('rate-limit-time-enabled');
scheduleToggle.type = 'checkbox';
const hint = reg('rate-limit-status-hint', 'div');
hint.className = 'alert alert-info py-2 px-3 mb-0 mt-3';

// 限速控件：数值输入（裸数字默认 KB/s，也可带 K/M/G 后缀）+ 自动单位标签 + 隐藏域 + 摊分提示
const downloadValue = reg('download-rate-value');
const downloadUnit = reg('download-rate-unit', 'span');
const downloadHidden = reg('download-rate-limit');
const downloadSplit = reg('download-rate-split-hint', 'div');
downloadSplit.dataset = { tasks: '4' };

const uploadValue = reg('upload-rate-value');
const uploadUnit = reg('upload-rate-unit', 'span');
const uploadHidden = reg('upload-rate-limit');
const uploadSplit = reg('upload-rate-split-hint', 'div');
uploadSplit.dataset = { tasks: '1' };

const downloadThreads = reg('youtube-download-threads');
downloadThreads.value = '4';
const uploadThreads = reg('upload-threads');
uploadThreads.value = '1';

const startField = reg('rate-limit-start-time');
startField.type = 'time';
const endField = reg('rate-limit-end-time');
endField.type = 'time';
const timeSettings = reg('rate-limit-time-settings', 'div');

const domReadyHandlers = [];
let fakeNow = { hour: 23, minute: 30 };

class FakeDate {
    constructor() {
        this._h = fakeNow.hour;
        this._m = fakeNow.minute;
    }
    getHours() { return this._h; }
    getMinutes() { return this._m; }
}

const context = {
    document: {
        getElementById: (id) => byId[id] || null,
        addEventListener: (name, handler) => {
            if (name === 'DOMContentLoaded') domReadyHandlers.push(handler);
        },
        querySelector: () => null,
        querySelectorAll: () => [],
    },
    window: { addEventListener() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
    Date: FakeDate,
    Set, Map, RegExp, Math, JSON, String, Number, Object, Array, Error,
    Boolean, Promise, parseInt, parseFloat, isNaN, isFinite,
};
context.globalThis = context;

const sandbox = vm.createContext(context);
vm.runInContext(source, sandbox);

// 只取本段注册的 DOMContentLoaded 回调（source 已被裁剪，理论上只有一个）
const run = () => {
    if (!domReadyHandlers.length) throw new Error('没有捕获到 DOMContentLoaded 回调');
    domReadyHandlers.forEach((handler) => handler());
};

const report = {
    scenarios: {},
    handlers: domReadyHandlers.length,
    errors: [],
};

function snapshot(name) {
    report.scenarios[name] = {
        hint: String(hint.textContent || '').replace(/\s+/g, ' ').trim(),
        classes: hint.classList.value,
        downloadDisabled: !!downloadValue.disabled,
        downloadUnitDisabled: !!downloadUnit.disabled,
        uploadDisabled: !!uploadValue.disabled,
        uploadUnitDisabled: !!uploadUnit.disabled,
        scheduleDisabled: !!scheduleToggle.disabled,
        startDisabled: !!startField.disabled,
        endDisabled: !!endField.disabled,
        timeDimmed: timeSettings.classList.contains('opacity-50'),
        downloadHidden: String(downloadHidden.value || ''),
        uploadHidden: String(uploadHidden.value || ''),
        downloadUnitLabel: String(downloadUnit.textContent || '').trim(),
        uploadUnitLabel: String(uploadUnit.textContent || '').trim(),
        downloadSplit: String(downloadSplit.textContent || '').replace(/\s+/g, ' ').trim(),
        uploadSplit: String(uploadSplit.textContent || '').replace(/\s+/g, ' ').trim(),
        downloadSplitTasks: downloadSplit.dataset.tasks,
    };
}

const fireInput = (el) => (el.listeners.input || []).forEach((fn) => fn());

try {
    // 1. 总开关关闭：所有输入停用
    enabledToggle.checked = false;
    scheduleToggle.checked = true;
    downloadValue.value = '1';
    uploadValue.value = '500';
    startField.value = '22:00';
    endField.value = '06:00';
    run();
    snapshot('disabled');

    // 2. 总开关开启 + 时段开启 + 当前 23:30（跨夜时段内）
    enabledToggle.checked = true;
    fakeNow = { hour: 23, minute: 30 };
    run();
    snapshot('insideWindow');

    // 3. 当前 12:00（跨夜时段外）
    fakeNow = { hour: 12, minute: 0 };
    run();
    snapshot('outsideWindow');

    // 4. 关闭时间段 → 全天限速
    scheduleToggle.checked = false;
    run();
    snapshot('alwaysOn');

    // 5. 时段边界：06:00 为右开区间，不在时段内
    scheduleToggle.checked = true;
    fakeNow = { hour: 6, minute: 0 };
    run();
    snapshot('boundaryEndExclusive');

    // 6. 时段起点 22:00 含在内
    fakeNow = { hour: 22, minute: 0 };
    run();
    snapshot('boundaryStartInclusive');

    // 7. 速率为空 → 提示实际仍是满速
    downloadValue.value = '';
    uploadValue.value = '';
    run();
    snapshot('emptyRate');

    // 8. 起止时间相同 → 视为全天
    downloadValue.value = '1M';
    uploadValue.value = '500';
    startField.value = '09:00';
    endField.value = '09:00';
    fakeNow = { hour: 3, minute: 0 };
    run();
    snapshot('sameStartEnd');

    // 9. 自动单位：裸数字 1024（KB/s）→ 隐藏域 1024K，标签自动切到 1.0 MB/s
    downloadValue.value = '1024';
    downloadUnit.textContent = '';
    fireInput(downloadValue);
    snapshot('unitK');

    // 10. 改下载并发数量：下载提示是「动态分配」语义，不随并发数改写每任务配额
    downloadValue.value = '1M';
    downloadThreads.value = '2';
    fireInput(downloadThreads);
    snapshot('threadsChanged');

    // 11. 上传限速带单位输入 2M：提示显示总速率与并发上传任务数，标签显示 2.00 MB/s
    uploadValue.value = '2M';
    fireInput(uploadValue);
    snapshot('uploadUnitM');

    // 12. 裸数字 2048（KB/s）→ 隐藏域 2048K，标签自动切到 2.00 MB/s（不再手动选单位）
    downloadValue.value = '2048';
    fireInput(downloadValue);
    snapshot('autoUnitSwitch');
} catch (exc) {
    report.errors.push(String(exc && exc.stack ? exc.stack : exc));
}

process.stdout.write(JSON.stringify(report));
