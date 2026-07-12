/**
 * Settings page - Tab management, URL hash deep-linking, lazy initialization
 */
(function() {
    'use strict';

    const TAB_MAP = {
        '#profile': 'profile-tab',
        '#usage': 'usage-tab',
        '#wellbeing': 'wellbeing-tab',
        '#memory': 'memory-tab',
        '#api-keys': 'api-keys-tab'
    };

    const initialized = {
        profile: false,
        usage: false,
        wellbeing: false,
        memory: false,
        'api-keys': false
    };

    let chartJsLoaded = false;

    // Activate tab from URL hash
    function activateFromHash() {
        const hash = window.location.hash || '#profile';
        const tabId = TAB_MAP[hash];
        if (tabId) {
            const tabEl = document.getElementById(tabId);
            if (tabEl) {
                const tab = new bootstrap.Tab(tabEl);
                tab.show();
            }
        }
    }

    // Update URL hash on tab change
    function setupHashSync() {
        const tabEls = document.querySelectorAll('#settingsTabs button[data-bs-toggle="tab"]');
        tabEls.forEach(tabEl => {
            tabEl.addEventListener('shown.bs.tab', function(event) {
                const target = event.target.getAttribute('data-bs-target');
                history.replaceState(null, '', target);
                initTab(target.replace('#', ''));
            });
        });
    }

    // Lazy init each tab on first view
    function initTab(tabName) {
        if (initialized[tabName]) return;
        initialized[tabName] = true;

        switch(tabName) {
            case 'profile':
                // edit_profile.js initializes on DOMContentLoaded, already fired
                break;
            case 'usage':
                loadUsageTab();
                break;
            case 'wellbeing':
                loadWellbeingTab();
                break;
            case 'memory':
                break;
            case 'api-keys':
                // api-credentials.js initializes on DOMContentLoaded, already fired
                // But if it hasn't been visible yet, we may need to trigger it
                break;
        }
    }

    // --- Usage Tab (adapted from my_usage.html inline JS) ---
    let spendingChart = null;

    function loadUsageTab() {
        // Load Chart.js dynamically if not loaded
        if (!chartJsLoaded) {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js';
            script.onload = function() {
                chartJsLoaded = true;
                loadUsageData();
            };
            document.head.appendChild(script);
        } else {
            loadUsageData();
        }
    }

    async function loadUsageData() {
        const dateRangeEl = document.getElementById('usageDateRange');
        if (!dateRangeEl) return;
        const days = dateRangeEl.value;
        const params = new URLSearchParams();
        if (days !== 'all') params.append('days', days);

        try {
            const fetcher = typeof secureFetch === 'function' ? secureFetch : fetch;
            const response = await fetcher('/api/my-usage?' + params.toString(), { credentials: 'include' });
            if (!response || !response.ok) throw new Error('Failed to load data');
            const data = await response.json();

            updateBalance(data.balance);
            updateStats(data.stats);
            updateStorage(data.storage);
            updateUsageByType(data.by_type);
            updateChart(data.daily);
            updateDailyBreakdown(data.daily);
        } catch (error) {
            console.error('Error loading usage data:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error('Error', 'Failed to load usage data');
            }
        }
    }

    function updateBalance(balance) {
        const el = document.getElementById('usageCurrentBalance');
        if (el) el.textContent = '$' + (balance || 0).toFixed(2);
    }

    function updateStats(stats) {
        const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        setEl('statOperations', formatNumber(stats.total_operations || 0));
        setEl('statTokens', formatNumber(stats.total_tokens || 0));
        setEl('statTokensBreakdown', formatNumber(stats.tokens_in || 0) + ' in / ' + formatNumber(stats.tokens_out || 0) + ' out');
        setEl('statCost', '$' + (stats.total_cost || 0).toFixed(2));
        setEl('statAvgDaily', '$' + (stats.avg_daily || 0).toFixed(2));
    }

    // --- Storage quota card (fed by the `storage` object in /api/my-usage) ---
    const GB_BYTES = 1024 * 1024 * 1024;

    function formatStorageBytes(bytes) {
        const value = Math.max(0, Number(bytes) || 0);
        if (value >= GB_BYTES) return (value / GB_BYTES).toFixed(1) + ' GB';
        if (value >= 1024 * 1024) return (value / (1024 * 1024)).toFixed(1) + ' MB';
        if (value >= 1024) return (value / 1024).toFixed(1) + ' KB';
        return Math.round(value) + ' B';
    }

    function updateStorage(storage) {
        const card = document.getElementById('storageCard');
        if (!card) return;
        if (!storage || typeof storage.used_bytes !== 'number') {
            // Older cached response without storage data: keep the card hidden.
            card.classList.add('d-none');
            return;
        }

        const amount = document.getElementById('storageAmount');
        const bar = document.getElementById('storageBar');
        const fill = document.getElementById('storageBarFill');
        const breakdown = document.getElementById('storageBreakdown');
        const used = storage.used_bytes || 0;
        const quota = storage.quota_bytes || 0;

        card.classList.remove('d-none', 'info', 'success', 'warning', 'danger');
        breakdown.textContent = 'Uploads ' + formatStorageBytes(storage.uploads_bytes || 0) +
            ' - Generated ' + formatStorageBytes(storage.generated_bytes || 0);

        if (quota === 0) {
            // Unlimited quota: used bytes only, no bar, no "of".
            amount.textContent = formatStorageBytes(used);
            card.classList.add('info');
            bar.classList.add('d-none');
            return;
        }

        const percent = (used / quota) * 100;
        const clamped = Math.max(0, Math.min(100, percent));
        amount.textContent = formatStorageBytes(used) + ' of ' + (quota / GB_BYTES).toFixed(1) + ' GB';
        bar.classList.remove('d-none');
        bar.setAttribute('aria-valuenow', String(Math.round(clamped)));
        bar.setAttribute('aria-label', 'Storage usage ' + percent.toFixed(1) + '%');
        fill.style.width = clamped + '%';
        fill.classList.remove('warn', 'danger');
        if (percent >= 95) {
            fill.classList.add('danger');
            card.classList.add('danger');
        } else if (percent >= 80) {
            fill.classList.add('warn');
            card.classList.add('warning');
        } else {
            card.classList.add('success');
        }
    }

    function updateUsageByType(byType) {
        const container = document.getElementById('usageByType');
        if (!container) return;

        if (!byType || byType.length === 0) {
            container.innerHTML = '<div class="text-center text-muted py-4">No usage data yet</div>';
            return;
        }

        const typeIcons = {
            'ai_tokens': 'fa-robot', 'tts': 'fa-volume-up', 'stt': 'fa-microphone',
            'image': 'fa-image', 'video': 'fa-video', 'domain': 'fa-globe'
        };
        const typeLabels = {
            'ai_tokens': 'AI Conversations', 'tts': 'Text-to-Speech', 'stt': 'Speech-to-Text',
            'image': 'Image Generation', 'video': 'Video Generation', 'domain': 'Custom Domains'
        };

        container.innerHTML = byType.map(t => `
            <div class="usage-item">
                <div class="details">
                    <span class="type-badge ${t.type}">
                        <i class="fas ${typeIcons[t.type] || 'fa-circle'}"></i>
                        ${typeLabels[t.type] || t.type}
                    </span>
                    <span class="ops">${formatNumber(t.operations)} operations</span>
                </div>
                <div class="cost">$${t.total_cost.toFixed(2)}</div>
            </div>
        `).join('');
    }

    function updateChart(daily) {
        const canvas = document.getElementById('usageSpendingChart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');

        if (spendingChart) spendingChart.destroy();
        if (!daily || daily.length === 0) return;

        const sorted = [...daily].sort((a, b) => a.date.localeCompare(b.date));
        const labels = sorted.map(d => formatDateShort(d.date));
        const costData = sorted.map(d => d.total_cost);

        spendingChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Daily Spending',
                    data: costData,
                    borderColor: 'rgb(250, 166, 26)',
                    backgroundColor: 'rgba(250, 166, 26, 0.15)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 3,
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: ctx => '$' + ctx.parsed.y.toFixed(2) } }
                },
                scales: {
                    x: {
                        ticks: {
                            color: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#72767d',
                            maxTicksLimit: 8
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' }
                    },
                    y: {
                        ticks: {
                            color: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#72767d',
                            callback: v => '$' + v.toFixed(2)
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' }
                    }
                }
            }
        });
    }

    function updateDailyBreakdown(daily) {
        const container = document.getElementById('usageDailyBreakdown');
        if (!container) return;

        if (!daily || daily.length === 0) {
            container.innerHTML = '<div class="text-center text-muted py-4">No activity yet</div>';
            return;
        }

        const sorted = [...daily].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 14);
        container.innerHTML = sorted.map(d => `
            <div class="daily-item">
                <div class="date">${formatDateLong(d.date)}</div>
                <div class="stats">
                    <span class="stat-val"><strong>${formatNumber(d.operations)}</strong> ops</span>
                    <span class="stat-val"><strong>${formatNumber(d.tokens_in + d.tokens_out)}</strong> tokens</span>
                    <span class="stat-val"><strong>$${d.total_cost.toFixed(2)}</strong></span>
                </div>
            </div>
        `).join('');
    }

    // --- Break Reminders Tab ---
    async function loadWellbeingTab() {
        setupWellbeingHandlers();
        await loadWellbeingPreferences();
    }

    function setupWellbeingHandlers() {
        const form = document.getElementById('wellbeingPreferencesForm');
        const resetBtn = document.getElementById('wellbeingResetSessionBtn');
        if (form && !form.dataset.bound) {
            form.dataset.bound = '1';
            form.addEventListener('submit', saveWellbeingPreferences);
        }
        if (resetBtn && !resetBtn.dataset.bound) {
            resetBtn.dataset.bound = '1';
            resetBtn.addEventListener('click', resetWellbeingSession);
        }
    }

    async function wellbeingFetch(url, options = {}) {
        const fetcher = typeof secureFetch === 'function' ? secureFetch : fetch;
        return fetcher(url, {
            credentials: 'include',
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...(options.headers || {})
            }
        });
    }

    async function loadWellbeingPreferences() {
        try {
            const response = await wellbeingFetch('/api/wellbeing/preferences');
            if (!response.ok) throw new Error('Failed to load break reminder settings');
            const data = await response.json();
            renderWellbeingPreferences(data.preferences || {});
            renderWellbeingStatus(data.status || {});
        } catch (error) {
            console.error('Error loading break reminder settings:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error('Error', 'Failed to load break reminder settings');
            }
        }
    }

    function renderWellbeingPreferences(preferences) {
        const remindersEnabled = document.getElementById('wellbeingRemindersEnabled');
        const intenseEnabled = document.getElementById('wellbeingIntenseEnabled');
        const preferredMinutes = document.getElementById('wellbeingPreferredSoftMinutes');
        if (remindersEnabled) remindersEnabled.checked = preferences.reminders_enabled !== false;
        if (intenseEnabled) intenseEnabled.checked = preferences.intense_reminders_enabled !== false;
        if (preferredMinutes) preferredMinutes.value = preferences.preferred_soft_minutes || '';
    }

    function renderWellbeingStatus(status) {
        const session = status.session || {};
        const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        setEl('wellbeingActiveMinutes', session.active_minutes !== undefined ? formatNumber(session.active_minutes) : '-');
        setEl('wellbeingUserMessages', session.user_messages_count !== undefined ? formatNumber(session.user_messages_count) : '-');
        setEl('wellbeingRemindersShown', session.reminders_shown !== undefined ? formatNumber(session.reminders_shown) : '-');
        setEl('wellbeingSeverity', session.current_severity || 'normal');

        const statusText = document.getElementById('wellbeingStatusText');
        if (statusText) {
            if (!session.id) {
                statusText.textContent = 'No active continuous session.';
            } else if (status.active_pause && status.pause_until) {
                statusText.textContent = 'Pause active until ' + new Date(status.pause_until).toLocaleTimeString();
            } else {
                statusText.textContent = 'Current session started ' + formatDateTime(session.started_at) + '.';
            }
        }
    }

    async function saveWellbeingPreferences(event) {
        event.preventDefault();
        const preferredMinutes = document.getElementById('wellbeingPreferredSoftMinutes');
        const payload = {
            reminders_enabled: document.getElementById('wellbeingRemindersEnabled')?.checked,
            intense_reminders_enabled: document.getElementById('wellbeingIntenseEnabled')?.checked,
            preferred_soft_minutes: preferredMinutes && preferredMinutes.value ? Number(preferredMinutes.value) : null
        };
        try {
            const response = await wellbeingFetch('/api/wellbeing/preferences', {
                method: 'PUT',
                body: JSON.stringify(payload)
            });
            if (!response.ok) throw new Error('Failed to save break reminder settings');
            const data = await response.json();
            renderWellbeingPreferences(data.preferences || {});
            renderWellbeingStatus(data.status || {});
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.success('Saved', 'Break reminder settings updated');
            }
        } catch (error) {
            console.error('Error saving break reminder settings:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error('Error', 'Failed to save break reminder settings');
            }
        }
    }

    async function resetWellbeingSession() {
        try {
            const response = await wellbeingFetch('/api/wellbeing/reset-session', {
                method: 'POST',
                body: JSON.stringify({})
            });
            if (!response.ok) throw new Error('Failed to reset current counter');
            const status = await response.json();
            renderWellbeingStatus(status || {});
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.success('Reset', 'Current break reminder counter reset');
            }
        } catch (error) {
            console.error('Error resetting break reminder counter:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error('Error', 'Failed to reset current counter');
            }
        }
    }

    function formatDateTime(value) {
        if (!value) return '-';
        const normalized = String(value).replace(' ', 'T') + (String(value).includes('Z') ? '' : 'Z');
        const date = new Date(normalized);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }

    function formatNumber(num) {
        if (num >= 1000000000) return (num / 1000000000).toFixed(1) + 'B';
        if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
        if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
        return num.toString();
    }

    function formatDateShort(dateStr) {
        const date = new Date(dateStr + 'T00:00:00');
        return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }

    function formatDateLong(dateStr) {
        const date = new Date(dateStr + 'T00:00:00');
        return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    }

    // Expose loadUsageData for the date range filter
    window.loadUsageData = loadUsageData;

    // Init
    document.addEventListener('DOMContentLoaded', function() {
        setupHashSync();
        activateFromHash();
        // Always init profile tab (default)
        const hash = window.location.hash || '#profile';
        initTab(hash.replace('#', ''));
    });
})();
