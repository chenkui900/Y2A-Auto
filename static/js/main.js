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
        manualCleanupBtn.addEventListener('click', function() {
            // 使用当前输入的小时数（若存在）
            if (logCleanupHoursField && cleanupHoursHidden) {
                const hours = parseInt(logCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    cleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧日志吗？将删除 ${cleanupHoursHidden ? cleanupHoursHidden.value : ''} 小时前的日志文件。`;
            if (confirm(confirmMsg)) {
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
        manualDownloadCleanupBtn.addEventListener('click', function() {
            // 使用当前输入的小时数（若存在）
            if (downloadCleanupHoursField && downloadCleanupHoursHidden) {
                const hours = parseInt(downloadCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    downloadCleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧的下载内容吗？将删除 ${downloadCleanupHoursHidden ? downloadCleanupHoursHidden.value : ''} 小时前的下载文件和目录。`;
            if (confirm(confirmMsg)) {
                const form = document.getElementById('download-cleanup-form');
                if (form) form.submit();
            }
        });
    }
}); 