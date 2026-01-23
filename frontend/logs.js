// Logs Page Logic
let allLogs = [];
let currentView = 'by-person';

window.showLogsView = async function() {
    const mainApp = document.getElementById('main-app');
    const dashboard = mainApp.querySelector('.dashboard');
    const logsView = document.getElementById('logs-view');

    if (!window.isAdmin()) {
        showToast('Admin access required', 'error');
        return;
    }

    const adminPassword = sessionStorage.getItem('ieee_presence_password');
    if (!adminPassword) {
        showToast('Please log in again to access logs', 'error');
        setTimeout(() => {
            window.logout();
        }, 2000);
        return;
    }

    dashboard.style.display = 'none';
    logsView.style.display = 'block';

    await fetchLogs();
}

window.hideLogsView = function() {
    const mainApp = document.getElementById('main-app');
    const dashboard = mainApp.querySelector('.dashboard');
    const logsView = document.getElementById('logs-view');

    logsView.style.display = 'none';
    dashboard.style.display = 'block';
}

async function fetchLogs() {
    const logsContent = document.getElementById('logs-content');
    logsContent.innerHTML = '<div class="loading-state">Loading logs...</div>';

    try {
        const adminPassword = sessionStorage.getItem('ieee_presence_password') || '';
        allLogs = await window.convexClient.query("logs:getAllStatusLogs", { adminPassword });
        renderCurrentView();
    } catch (error) {
        console.error('Error fetching logs:', error);
        logsContent.innerHTML = `<div class="empty-state">Error loading logs: ${error.message}</div>`;
    }
}

window.switchTab = function(tabName) {
    currentView = tabName;

    document.querySelectorAll('.log-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });

    renderCurrentView();
}

function renderCurrentView() {
    const logsContent = document.getElementById('logs-content');

    if (allLogs.length === 0) {
        logsContent.innerHTML = '<div class="empty-state">No status change logs found.</div>';
        return;
    }

    if (currentView === 'by-person') {
        renderLogsByPerson(logsContent);
    } else {
        renderLogsByDate(logsContent);
    }
}

function renderLogsByPerson(container) {
    const logsByPerson = {};

    allLogs.forEach(log => {
        const personName = log.personName || 'Unknown';
        if (!logsByPerson[personName]) {
            logsByPerson[personName] = [];
        }
        logsByPerson[personName].push(log);
    });

    const sortedPersons = Object.keys(logsByPerson).sort();

    let html = '';

    sortedPersons.forEach((personName, index) => {
        const logs = logsByPerson[personName];
        logs.sort((a, b) => b.timestamp - a.timestamp);

        html += `
            <div class="person-group">
                <div class="person-header" onclick="togglePersonGroup(${index})">
                    <div class="person-info">
                        <strong>${escapeHtml(personName)}</strong>
                        <span class="log-count">${logs.length} changes</span>
                    </div>
                    <span class="toggle-icon">▼</span>
                </div>
                <div class="person-logs" data-person-index="${index}">
                    ${logs.map(log => renderLogEntry(log)).join('')}
                </div>
            </div>
        `;
    });

    container.innerHTML = html;
}

function renderLogsByDate(container) {
    const sortedLogs = [...allLogs].sort((a, b) => b.timestamp - a.timestamp);

    let html = '<div class="logs-list">';

    sortedLogs.forEach(log => {
        html += renderLogEntry(log);
    });

    html += '</div>';
    container.innerHTML = html;
}

function renderLogEntry(log) {
    const date = new Date(log.timestamp);
    const dateStr = date.toLocaleDateString();
    const timeStr = date.toLocaleTimeString();
    const statusClass = log.status === 'present' ? 'present' : 'absent';
    const statusText = log.status === 'present' ? 'Present' : 'Absent';

    return `
        <div class="log-entry">
            <div class="log-entry-header">
                <div class="log-person">${escapeHtml(log.personName)}</div>
                <div class="log-status"><span class="status-badge ${statusClass}">${statusText}</span></div>
            </div>
            <div class="log-entry-details">
                <div class="log-time">${dateStr} at ${timeStr}</div>
                <div class="log-device">${escapeHtml(log.macAddress)}</div>
            </div>
        </div>
    `;
}

window.togglePersonGroup = function(index) {
    const group = document.querySelector(`.person-logs[data-person-index="${index}"]`);
    const icon = group.closest('.person-group').querySelector('.toggle-icon');

    if (group.style.display === 'none') {
        group.style.display = 'block';
        icon.style.transform = 'rotate(0deg)';
    } else {
        group.style.display = 'none';
        icon.style.transform = 'rotate(-90deg)';
    }
}

window.exportToCSV = function() {
    if (!window.isAdmin()) {
        showToast('Admin access required', 'error');
        return;
    }

    if (allLogs.length === 0) {
        showToast('No logs to export', 'error');
        return;
    }

    let csv = '';
    let rows = [];

    if (currentView === 'by-person') {
        csv = 'Person Name,MAC Address,Status,Timestamp\n';

        const logsByPerson = {};
        allLogs.forEach(log => {
            const personName = log.personName || 'Unknown';
            if (!logsByPerson[personName]) {
                logsByPerson[personName] = [];
            }
            logsByPerson[personName].push(log);
        });

        const sortedPersons = Object.keys(logsByPerson).sort();
        sortedPersons.forEach(personName => {
            const logs = logsByPerson[personName];
            logs.sort((a, b) => b.timestamp - a.timestamp);

            rows.push({ separator: true, value: personName });
            logs.forEach(log => {
                const date = new Date(log.timestamp);
                rows.push({
                    personName: log.personName,
                    macAddress: log.macAddress,
                    status: log.status,
                    timestamp: date.toISOString()
                });
            });
        });

        rows.forEach(row => {
            if (row.separator) {
                csv += `"${escapeCsv(row.value)}"\n`;
            } else {
                csv += `"${escapeCsv(row.personName)}","${escapeCsv(row.macAddress)}","${escapeCsv(row.status)}","${escapeCsv(row.timestamp)}"\n`;
            }
        });

    } else {
        csv = 'Person Name,MAC Address,Status,Timestamp\n';

        const sortedLogs = [...allLogs].sort((a, b) => b.timestamp - a.timestamp);
        sortedLogs.forEach(log => {
            const date = new Date(log.timestamp);
            csv += `"${escapeCsv(log.personName)}","${escapeCsv(log.macAddress)}","${escapeCsv(log.status)}","${escapeCsv(date.toISOString())}"\n`;
        });
    }

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', `presence-logs-${currentView}-${Date.now()}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showToast('Logs exported successfully', 'success');
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeCsv(text) {
    if (text === null || text === undefined) return '';
    const str = String(text);
    return str.replace(/"/g, '""');
}
