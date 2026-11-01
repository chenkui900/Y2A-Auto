// 主JavaScript文件

// --- 主题（暗色模式）早期应用：放在 DOMContentLoaded 之前避免闪烁 ---
(function () {
    try {
        const theme = localStorage.getItem('y2a-theme');
        if (theme === 'dark') document.documentElement.classList.add('dark');
    } catch (e) { /* localStorage 不可用时忽略 */ }
})();

document.addEventListener('DOMContentLoaded', function() {
    console.log('Y2A-Auto 已加载');

    // --- 导航栏高度写入 CSS 变量：供吸顶工具栏（.tasks-head-sticky）定位使用 ---
    (function syncNavbarHeightVar() {
        const navbar = document.querySelector('nav.navbar');
        if (!navbar) return;
        const apply = () => {
            const h = Math.round(navbar.getBoundingClientRect().height);
            if (h > 0) document.documentElement.style.setProperty('--y2a-nav-h', h + 'px');
        };
        apply();
        window.addEventListener('resize', apply);
        if (window.ResizeObserver) {
            try { new ResizeObserver(apply).observe(navbar); } catch (e) { /* ignore */ }
        }
    })();

    // --- 主题切换按钮 ---
    const themeToggle = document.getElementById('themeToggle');
    const syncThemeIcon = () => {
        if (!themeToggle) return;
        const dark = document.documentElement.classList.contains('dark');
        themeToggle.innerHTML = dark
            ? '<i class="bi bi-sun-fill"></i>'
            : '<i class="bi bi-moon-stars"></i>';
        themeToggle.title = dark ? '切换到亮色模式' : '切换到暗色模式';
    };
    syncThemeIcon();
    if (themeToggle) {
        themeToggle.addEventListener('click', function () {
            const dark = document.documentElement.classList.toggle('dark');
            try { localStorage.setItem('y2a-theme', dark ? 'dark' : 'light'); } catch (e) {}
            syncThemeIcon();
        });
    }

    // --- Flash 提示自动消失（6 秒）---
    document.querySelectorAll('.flash-messages .alert-dismissible').forEach(el => {
        setTimeout(() => {
            try { bootstrap.Alert.getOrCreateInstance(el).close(); } catch (e) { el.remove(); }
        }, 6000);
    });

    // --- 设置页面的密码保护逻辑 ---
    const settingsForm = document.querySelector('form[method="post"][enctype="multipart/form-data"]');
    if (settingsForm) {
        const newPassword = document.getElementById('new_password');
        const confirmPassword = document.getElementById('confirm_password');
        const passwordError = document.getElementById('password-match-error');
        const passwordProtectionEnabled = document.getElementById('password_protection_enabled');
        const passwordFields = document.getElementById('password-fields');

        if (passwordProtectionEnabled) {
            function togglePasswordFields() {
                if (passwordProtectionEnabled.checked) {
                    passwordFields.style.display = 'block';
                } else {
                    passwordFields.style.display = 'none';
                }
            }

            // Initial state
            togglePasswordFields();
            passwordProtectionEnabled.addEventListener('change', togglePasswordFields);
        }

        settingsForm.addEventListener('submit', function(event) {
            // 仅在启用密码保护时才校验密码匹配
            if (passwordProtectionEnabled && passwordProtectionEnabled.checked &&
                newPassword && confirmPassword && newPassword.value !== confirmPassword.value) {
                event.preventDefault(); // 阻止表单提交
                if (passwordError) {
                    passwordError.classList.remove('d-none');
                }
            } else {
                if (passwordError) {
                    passwordError.classList.add('d-none');
                }
            }
        });
    }


    // --- 设置页面的日志清理按钮逻辑 ---
    // 绑定手动日志清理按钮
    const manualCleanupBtn = document.getElementById('manual-cleanup-btn');
    const logCleanupHoursField = document.getElementById('log-cleanup-hours');
    const cleanupHoursHidden = document.getElementById('cleanup-hours-input');
    if(manualCleanupBtn) {
        manualCleanupBtn.addEventListener('click', async function() {
            // 使用当前输入的小时数（若存在）
            if (logCleanupHoursField && cleanupHoursHidden) {
                const hours = parseInt(logCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    cleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧日志吗？将删除 ${cleanupHoursHidden ? cleanupHoursHidden.value : ''} 小时前的日志文件。`;
            if (await y2aConfirm(confirmMsg, { title: '清理旧日志', type: 'warning', confirmText: '开始清理' })) {
                const form = document.getElementById('cleanup-form');
                if (form) form.submit();
            }
        });
    }

    // 绑定立即清空日志按钮
    const clearLogsBtn = document.getElementById('clear-logs-btn');
    const confirmClearBtn = document.getElementById('confirm-clear-btn');
    const cancelClearBtn = document.getElementById('cancel-clear-btn');
    const clearWarning = document.getElementById('clear-warning');

    if (clearLogsBtn) {
        clearLogsBtn.addEventListener('click', function() {
            clearLogsBtn.classList.add('d-none');
            confirmClearBtn.classList.remove('d-none');
            cancelClearBtn.classList.remove('d-none');
            clearWarning.classList.remove('d-none');
        });
    }

    if (cancelClearBtn) {
        cancelClearBtn.addEventListener('click', function() {
            clearLogsBtn.classList.remove('d-none');
            confirmClearBtn.classList.add('d-none');
            cancelClearBtn.classList.add('d-none');
            clearWarning.classList.add('d-none');
        });
    }
    
    if (confirmClearBtn) {
        confirmClearBtn.addEventListener('click', function() {
             document.getElementById('clear-form').submit();
        });
    }

    // --- 设置页面的下载内容清理按钮逻辑 ---
    // 绑定手动下载内容清理按钮
    const manualDownloadCleanupBtn = document.getElementById('manual-download-cleanup-btn');
    const downloadCleanupHoursField = document.getElementById('download-cleanup-hours');
    const downloadCleanupHoursHidden = document.getElementById('download-cleanup-hours-input');
    if(manualDownloadCleanupBtn) {
        manualDownloadCleanupBtn.addEventListener('click', async function() {
            // 使用当前输入的小时数（若存在）
            if (downloadCleanupHoursField && downloadCleanupHoursHidden) {
                const hours = parseInt(downloadCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    downloadCleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧的下载内容吗？将删除 ${downloadCleanupHoursHidden ? downloadCleanupHoursHidden.value : ''} 小时前的下载文件和目录。`;
            if (await y2aConfirm(confirmMsg, { title: '清理下载内容', type: 'danger', confirmText: '开始清理' })) {
                const form = document.getElementById('download-cleanup-form');
                if (form) form.submit();
            }
        });
    }
}); 
// ==========================================================
// 设置页：下载内容清理 / 上传完就删除 的互斥说明
//  - 两个都勾选 → 以上传完就删为准，时间设置（保留时长/间隔）停用
//  - 只勾选自动清理 → 按时间设置执行
// ==========================================================
document.addEventListener('DOMContentLoaded', function () {
    const cleanupToggle = document.getElementById('download-cleanup-enabled');
    const deleteAfterUpload = document.getElementById('delete-download-after-upload');
    const hint = document.getElementById('download-cleanup-mode-hint');
    if (!cleanupToggle || !deleteAfterUpload || !hint) return;

    const timeSettings = document.getElementById('download-cleanup-time-settings');
    const hoursField = document.getElementById('download-cleanup-hours');
    const intervalField = document.getElementById('download-cleanup-interval');

    function readNumber(field, fallback) {
        const value = field ? parseInt(field.value, 10) : NaN;
        return (!isNaN(value) && value > 0) ? value : fallback;
    }

    function renderDownloadCleanupMode() {
        const cleanupOn = cleanupToggle.checked;
        const deleteOn = deleteAfterUpload.checked;
        let tone = 'alert-info';
        let text = '';

        if (cleanupOn && deleteOn) {
            tone = 'alert-success';
            text = '当前模式：上传完就删除。任务在所有目标平台上传成功后立即删除本地视频、封面和字幕；下方的时间设置与定时清理不生效。';
        } else if (cleanupOn) {
            text = '当前模式：按时间清理。每 ' + readNumber(intervalField, 24) + ' 小时清理一次 '
                + readNumber(hoursField, 72) + ' 小时前的下载内容（未完成任务的目录不会被删）。';
        } else if (deleteOn) {
            text = '当前模式：上传完就删除。任务上传成功后立即删除本地文件；未开启自动清理，失败任务的残留需手动清理。';
        } else {
            tone = 'alert-secondary';
            text = '当前未启用任何自动清理：下载文件会一直保留，直到手动删除任务文件。';
        }

        hint.className = 'alert ' + tone + ' py-2 px-3 mb-3';
        hint.textContent = text;

        // 上传完就删已生效时，时间设置没有意义：置灰并停用（停用后不会提交，保存时保留原值）
        if (timeSettings) timeSettings.classList.toggle('opacity-50', deleteOn);
        [hoursField, intervalField].forEach(function (field) {
            if (field) field.disabled = deleteOn;
        });
    }

    cleanupToggle.addEventListener('change', renderDownloadCleanupMode);
    deleteAfterUpload.addEventListener('change', renderDownloadCleanupMode);
    [hoursField, intervalField].forEach(function (field) {
        if (field) field.addEventListener('input', renderDownloadCleanupMode);
    });
    renderDownloadCleanupMode();
});
// ==========================================================
// 设置页：上传/下载限速的总开关与生效时间段
//  - 总开关关闭 → 速率输入与时间段全部停用（不提交，保存时保留原值）
//  - 时间段关闭 → 全天限速；开启 → 只在 [开始, 结束) 内限速，支持跨零点
// ==========================================================
document.addEventListener('DOMContentLoaded', function () {
    const enabledToggle = document.getElementById('rate-limit-enabled');
    const scheduleToggle = document.getElementById('rate-limit-time-enabled');
    const hint = document.getElementById('rate-limit-status-hint');
    if (!enabledToggle || !hint) return;

    // 限速控件 = 数值输入 + 单位下拉（K/M），另有一个隐藏域存 "500K"/"1M" 这种字符串。
    // 总开关关闭时数值/单位被 disabled，不参与提交，保存时保留原值。
    const rateFields = [
        {
            key: 'download',
            value: document.getElementById('download-rate-value'),
            unit: document.getElementById('download-rate-unit'),
            hidden: document.getElementById('download-rate-limit'),
            splitHint: document.getElementById('download-rate-split-hint'),
            threads: document.getElementById('youtube-download-threads')
        },
        {
            key: 'upload',
            value: document.getElementById('upload-rate-value'),
            unit: document.getElementById('upload-rate-unit'),
            hidden: document.getElementById('upload-rate-limit'),
            splitHint: document.getElementById('upload-rate-split-hint'),
            threads: document.getElementById('upload-threads')
        }
    ];
    const downloadSplitHint = rateFields[0].splitHint;
    const uploadSplitHint = rateFields[1].splitHint;
    const startField = document.getElementById('rate-limit-start-time');
    const endField = document.getElementById('rate-limit-end-time');
    const timeSettings = document.getElementById('rate-limit-time-settings');
    const UNIT_FACTORS = {K: 1024, M: 1024 * 1024, G: 1024 * 1024 * 1024};

    function rateUnit(field) {
        const unit = field && field.unit && field.unit.value ? String(field.unit.value).toUpperCase() : 'K';
        return UNIT_FACTORS[unit] ? unit : 'K';
    }

    // 数值 + 单位 -> "1.5M"；数值为空表示不限速
    function composeRate(field) {
        const raw = field && field.value && field.value.value !== null ? String(field.value.value).trim() : '';
        if (!raw || Number(raw) <= 0) return '';
        return raw + rateUnit(field);
    }

    function parseRateText(text) {
        const match = /^(\d+(?:\.\d+)?)\s*([KMG]?)$/i.exec(String(text || '').trim());
        if (!match) return 0;
        const factor = UNIT_FACTORS[(match[2] || '').toUpperCase()] || 1;
        return Math.floor(parseFloat(match[1]) * factor) || 0;
    }

    function formatRate(bytes) {
        if (!bytes || bytes <= 0) return '不限速';
        if (bytes >= UNIT_FACTORS.G) return (bytes / UNIT_FACTORS.G).toFixed(2).replace(/\.?0+$/, '') + ' GB/s';
        if (bytes >= UNIT_FACTORS.M) return (bytes / UNIT_FACTORS.M).toFixed(2).replace(/\.?0+$/, '') + ' MB/s';
        if (bytes >= UNIT_FACTORS.K) return (bytes / UNIT_FACTORS.K).toFixed(2).replace(/\.?0+$/, '') + ' KB/s';
        return bytes + ' B/s';
    }

    function threadCount(field, fallback) {
        const value = field && field.value !== '' ? parseInt(field.value, 10) : NaN;
        return isNaN(value) || value < 1 ? fallback : value;
    }

    // 并发数量决定同时下载的任务数，也决定限速怎么摊分；没保存前先在提示里按新值算一遍
    function syncSplitHints() {
        if (downloadSplitHint) {
            const tasks = threadCount(rateFields[0].threads, parseInt(downloadSplitHint.dataset.tasks, 10) || 1);
            downloadSplitHint.dataset.tasks = tasks;
        }
        if (uploadSplitHint) {
            const tasks = threadCount(rateFields[1].threads, parseInt(uploadSplitHint.dataset.tasks, 10) || 1);
            uploadSplitHint.dataset.tasks = tasks;
        }
    }

    function renderSplitHints() {
        if (downloadSplitHint) {
            const total = parseRateText(composeRate(rateFields[0]));
            if (!total) {
                downloadSplitHint.textContent = '留空表示不限速，下载按带宽满速跑。';
            } else {
                // 下载限速按「当前实际在下载的任务数」动态分配，不再按并发数量死板平分：
                // 只有 1 个任务在下载时就吃满全部带宽，多个同时下载时自动均分，总速度不超设定值；
                // 修改限速/并发后无需重启，正在下载的任务会自动以新速率续传。
                downloadSplitHint.textContent = '总 ' + formatRate(total)
                    + ' 会按「当前实际在下载的任务数」动态分配（并发数量只决定最多同时下载几个）：'
                    + '只有 1 个任务在下载时就吃满全部带宽，多个同时下载时自动均分，总速度也不会超过设定值。'
                    + '修改限速/并发后无需重启，正在下载的任务会自动以新速率续传。';
            }
        }
        if (uploadSplitHint) {
            const total = parseRateText(composeRate(rateFields[1]));
            const tasks = Math.max(1, parseInt(uploadSplitHint.dataset.tasks, 10) || 1);
            uploadSplitHint.textContent = total
                ? '总 ' + formatRate(total) + '，由 ' + tasks + ' 个并发上传任务共享（全局记账，不会翻倍）。'
                : '留空表示不限速，上传按带宽满速跑。';
        }
    }

    function syncHiddenRate() {
        rateFields.forEach(function (field) {
            if (field.hidden) field.hidden.value = composeRate(field);
        });
    }

    function toMinutes(field, fallback) {
        const text = field && field.value ? String(field.value).trim() : '';
        const match = /^(\d{1,2}):(\d{2})$/.exec(text);
        if (!match) return fallback;
        const hour = parseInt(match[1], 10);
        const minute = parseInt(match[2], 10);
        if (isNaN(hour) || isNaN(minute) || hour > 23 || minute > 59) return fallback;
        return hour * 60 + minute;
    }

    function pad(value) {
        return (value < 10 ? '0' : '') + value;
    }

    function formatMinutes(minutes) {
        const value = ((minutes % 1440) + 1440) % 1440;
        return pad(Math.floor(value / 60)) + ':' + pad(value % 60);
    }

    function inWindow(start, end, nowMinutes) {
        if (start === end) return true;
        if (start < end) return nowMinutes >= start && nowMinutes < end;
        return nowMinutes >= start || nowMinutes < end;
    }

    function renderRateLimitState() {
        const enabled = enabledToggle.checked;
        const scheduled = enabled && scheduleToggle && scheduleToggle.checked;

        // 总开关关闭时，数值与单位都停用；停用后不提交，保存时保留原值
        rateFields.forEach(function (field) {
            if (field.value) field.value.disabled = !enabled;
            if (field.unit) field.unit.disabled = !enabled;
        });
        if (scheduleToggle) scheduleToggle.disabled = !enabled;
        [startField, endField].forEach(function (field) {
            if (field) field.disabled = !scheduled;
        });
        if (timeSettings) timeSettings.classList.toggle('opacity-50', !scheduled);
        hint.classList.remove('alert-info', 'alert-secondary', 'alert-success', 'alert-warning');

        if (!enabled) {
            hint.classList.add('alert-secondary');
            hint.textContent = '限速已关闭：上传和下载都按满速传输。';
            return;
        }

        const hasRate = rateFields.some(function (field) {
            return parseRateText(composeRate(field)) > 0;
        });
        if (!hasRate) {
            hint.classList.add('alert-warning');
            hint.textContent = '已启用限速，但下载和上传速率都没填，实际仍是满速。填写速率后保存即可生效。';
            return;
        }
        if (!scheduled) {
            hint.classList.add('alert-info');
            hint.textContent = '全天限速：上传和下载始终按设定速率传输。';
            return;
        }

        const start = toMinutes(startField, 22 * 60);
        const end = toMinutes(endField, 6 * 60);
        const now = new Date();
        const nowMinutes = now.getHours() * 60 + now.getMinutes();
        const windowText = start > end
            ? formatMinutes(start) + ' - ' + formatMinutes(end) + '（跨夜）'
            : formatMinutes(start) + ' - ' + formatMinutes(end);
        if (start === end) {
            hint.classList.add('alert-info');
            hint.textContent = '起止时间相同，视为全天限速：上传和下载始终按设定速率传输。';
            return;
        }
        if (inWindow(start, end, nowMinutes)) {
            hint.classList.add('alert-success');
            hint.textContent = '当前处于限速时段（' + windowText + '），按设定速率传输。';
        } else {
            hint.classList.add('alert-info');
            hint.textContent = '当前不在限速时段（' + windowText + '），按满速传输；进入时段后自动限速。';
        }
    }

    function renderAll() {
        renderRateLimitState();
        syncHiddenRate();
        renderSplitHints();
    }

    [enabledToggle, scheduleToggle, startField, endField].forEach(function (field) {
        if (!field) return;
        field.addEventListener('change', renderAll);
    });
    rateFields.forEach(function (field) {
        if (field.value) {
            field.value.addEventListener('input', renderAll);
            field.value.addEventListener('change', renderAll);
        }
        if (field.unit) field.unit.addEventListener('change', renderAll);
    });
    [rateFields[0].threads, rateFields[1].threads].forEach(function (field) {
        if (!field) return;
        field.addEventListener('input', function () {
            syncSplitHints();
            renderSplitHints();
        });
    });

    syncSplitHints();
    renderAll();
    // 页面长时间停留时，跨过时段边界也要更新提示
    setInterval(renderRateLimitState, 30000);
});
// ==========================================================
// 通用提示对话框（美化版）：y2aAlert / y2aConfirm
//  - Promise 风格：await y2aConfirm('确定吗？')  -> true / false
//  - 支持标题、图标类型、补充说明、深浅色主题、ESC/点击遮罩关闭
// ==========================================================
(function () {
    const ICONS = {
        primary: 'bi-info-circle-fill',
        info: 'bi-info-circle-fill',
        success: 'bi-check-circle-fill',
        warning: 'bi-exclamation-triangle-fill',
        danger: 'bi-exclamation-octagon-fill'
    };

    function buildDialog() {
        const mask = document.createElement('div');
        mask.className = 'y2a-dialog-mask';
        mask.setAttribute('role', 'dialog');
        mask.setAttribute('aria-modal', 'true');
        mask.innerHTML = `
            <div class="y2a-dialog" role="document">
                <div class="y2a-dialog-head">
                    <div class="y2a-dialog-icon tone-primary"><i class="bi bi-info-circle-fill"></i></div>
                    <h3 class="y2a-dialog-title"></h3>
                </div>
                <div class="y2a-dialog-body">
                    <div class="y2a-dialog-message"></div>
                    <div class="y2a-dialog-detail d-none"></div>
                </div>
                <div class="y2a-dialog-actions">
                    <button type="button" class="y2a-dialog-btn ghost" data-role="cancel">取消</button>
                    <button type="button" class="y2a-dialog-btn primary" data-role="confirm">确定</button>
                </div>
            </div>`;
        document.body.appendChild(mask);
        return mask;
    }

    function openDialog(options) {
        const opts = Object.assign({
            title: '',
            message: '',
            detail: '',
            type: 'primary',
            confirmText: '确定',
            cancelText: '取消',
            showCancel: false,
            confirmTone: '' // 覆盖确认按钮配色：primary/success/warning/danger
        }, options || {});

        return new Promise(function (resolve) {
            const mask = buildDialog();
            const iconEl = mask.querySelector('.y2a-dialog-icon');
            const titleEl = mask.querySelector('.y2a-dialog-title');
            const msgEl = mask.querySelector('.y2a-dialog-message');
            const detailEl = mask.querySelector('.y2a-dialog-detail');
            const cancelBtn = mask.querySelector('[data-role="cancel"]');
            const confirmBtn = mask.querySelector('[data-role="confirm"]');

            const tone = opts.confirmTone || (opts.showCancel ? (opts.type === 'danger' ? 'danger' : opts.type) : (opts.type === 'danger' ? 'danger' : opts.type));
            iconEl.className = 'y2a-dialog-icon tone-' + (opts.type || 'primary');
            iconEl.innerHTML = '<i class="bi ' + (ICONS[opts.type] || ICONS.primary) + '"></i>';
            titleEl.textContent = opts.title || (opts.showCancel ? '请确认' : '提示');
            msgEl.textContent = opts.message || '';
            if (opts.detail) {
                detailEl.classList.remove('d-none');
                detailEl.textContent = opts.detail;
                if (opts.type === 'warning' || opts.type === 'danger') {
                    detailEl.classList.add('tone-' + opts.type);
                }
            }
            confirmBtn.className = 'y2a-dialog-btn ' + (opts.confirmTone || (opts.type === 'danger' ? 'danger' : (opts.type === 'warning' ? 'warning' : (opts.type === 'success' ? 'success' : 'primary'))));
            confirmBtn.textContent = opts.confirmText;
            cancelBtn.textContent = opts.cancelText;
            cancelBtn.classList.toggle('d-none', !opts.showCancel);

            let settled = false;
            function close(result) {
                if (settled) return;
                settled = true;
                document.removeEventListener('keydown', onKey);
                mask.classList.remove('show');
                document.body.style.overflow = '';
                setTimeout(function () { mask.remove(); }, 200);
                resolve(result);
            }
            function onKey(e) {
                if (e.key === 'Escape') close(false);
                else if (e.key === 'Enter') { e.preventDefault(); close(true); }
            }

            cancelBtn.addEventListener('click', function () { close(false); });
            confirmBtn.addEventListener('click', function () { close(true); });
            mask.addEventListener('click', function (e) { if (e.target === mask) close(false); });
            document.addEventListener('keydown', onKey);

            document.body.style.overflow = 'hidden';
            requestAnimationFrame(function () { mask.classList.add('show'); });
            setTimeout(function () { confirmBtn.focus(); }, 60);
        });
    }

    window.y2aAlert = function (message, options) {
        const opts = Object.assign({ title: '提示', type: 'primary', confirmText: '知道了' }, options || {}, { message: message, showCancel: false });
        return openDialog(opts);
    };

    window.y2aConfirm = function (message, options) {
        const opts = Object.assign({ title: '请确认', type: 'warning', confirmText: '确定', showCancel: true }, options || {}, { message: message, showCancel: true });
        return openDialog(opts);
    };

    window.y2aDialog = openDialog;
})();
