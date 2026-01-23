// Logs Page Logic
let allLogs = [];
let currentView = 'by-person';
let selectedPerson = null;
let selectedDate = null;

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
    setupEventListeners();
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
        allLogs = await window.convexClient.query("devices:getAttendanceLogs", { adminPassword });
        populatePersonSelect();
        renderCurrentView();
    } catch (error) {
        console.error('Error fetching logs:', error);
        logsContent.innerHTML = `<div class="empty-state">Error loading logs: ${error.message}</div>`;
    }
}

function setupEventListeners() {
    const personSelect = document.getElementById('person-select');
    const datePicker = document.getElementById('date-picker');

    if (personSelect && !personSelect.dataset.setup) {
        personSelect.addEventListener('change', handlePersonChange);
        personSelect.dataset.setup = 'true';
    }

    if (datePicker && !datePicker.dataset.setup) {
        datePicker.addEventListener('change', handleDateChange);
        datePicker.dataset.setup = 'true';
    }
}

function populatePersonSelect() {
    const personSelect = document.getElementById('person-select');
    if (!personSelect) return;

    const persons = [...new Set(allLogs.map(log => log.userName).filter(Boolean))].sort();

    personSelect.innerHTML = '<option value="">-- Choose a person --</option>';
    persons.forEach(person => {
        const option = document.createElement('option');
        option.value = person;
        option.textContent = person;
        personSelect.appendChild(option);
    });
}

function handlePersonChange(e) {
    selectedPerson = e.target.value;
    renderCurrentView();
}

function handleDateChange(e) {
    selectedDate = e.target.value;
    renderCurrentView();
}

window.switchTab = function(tabName) {
    currentView = tabName;

    document.querySelectorAll('.log-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });

    const personFilter = document.getElementById('person-filter');
    const dateFilter = document.getElementById('date-filter');

    if (currentView === 'by-person') {
        personFilter.style.display = 'flex';
        dateFilter.style.display = 'none';
        selectedDate = null;
    } else {
        personFilter.style.display = 'none';
        dateFilter.style.display = 'flex';
        selectedPerson = null;
    }

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
    if (!selectedPerson) {
        container.innerHTML = '<div class="empty-state">Please select a person to view their logs.</div>';
        return;
    }

    const personLogs = allLogs.filter(log => log.userName === selectedPerson);

    if (personLogs.length === 0) {
        container.innerHTML = '<div class="empty-state">No logs found for this person.</div>';
        return;
    }

    personLogs.sort((a, b) => b.timestamp - a.timestamp);

    let html = `
        <div class="person-single-view">
            <div class="person-single-header">
                <strong>${escapeHtml(selectedPerson)}</strong>
                <span class="log-count">${personLogs.length} entries</span>
            </div>
            <div class="logs-list">
                ${personLogs.map(log => renderLogEntry(log)).join('')}
            </div>
        </div>
    `;

    container.innerHTML = html;
}

function renderLogsByDate(container) {
    if (!selectedDate) {
        container.innerHTML = '<div class="empty-state">Please select a date to view logs.</div>';
        return;
    }

    const startDate = new Date(selectedDate);
    startDate.setHours(0, 0, 0, 0);

    const endDate = new Date(selectedDate);
    endDate.setHours(23, 59, 59, 999);

    const dateLogs = allLogs.filter(log => {
        const logDate = new Date(log.timestamp);
        return logDate >= startDate && logDate <= endDate;
    });

    if (dateLogs.length === 0) {
        container.innerHTML = '<div class="empty-state">No logs found for this date.</div>';
        return;
    }

    dateLogs.sort((a, b) => b.timestamp - a.timestamp);

    let html = `
        <div class="date-view">
            <div class="date-header">
                <strong>${escapeHtml(new Date(selectedDate).toLocaleDateString())}</strong>
                <span class="log-count">${dateLogs.length} entries</span>
            </div>
            <div class="logs-list">
                ${dateLogs.map(log => renderLogEntry(log)).join('')}
            </div>
        </div>
    `;

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
                <div class="log-person">${escapeHtml(log.userName)}</div>
                <div class="log-status"><span class="status-badge ${statusClass}">${statusText}</span></div>
            </div>
            <div class="log-entry-details">
                <div class="log-time">${dateStr} at ${timeStr}</div>
                <div class="log-device">${escapeHtml(log.deviceId)}</div>
            </div>
        </div>
    `;
}

window.exportToCSV = function() {
    if (!window.isAdmin()) {
        showToast('Admin access required', 'error');
        return;
    }

    let logsToExport = [];
    let filenamePrefix = 'logs';

    if (currentView === 'by-person') {
        if (!selectedPerson) {
            showToast('Please select a person to export', 'error');
            return;
        }
        logsToExport = allLogs.filter(log => log.userName === selectedPerson);
        filenamePrefix = `logs-${encodeURIComponent(selectedPerson)}`;
    } else {
        if (!selectedDate) {
            showToast('Please select a date to export', 'error');
            return;
        }
        const startDate = new Date(selectedDate);
        startDate.setHours(0, 0, 0, 0);
        const endDate = new Date(selectedDate);
        endDate.setHours(23, 59, 59, 999);
        logsToExport = allLogs.filter(log => {
            const logDate = new Date(log.timestamp);
            return logDate >= startDate && logDate <= endDate;
        });
        filenamePrefix = `logs-${selectedDate}`;
    }

    if (logsToExport.length === 0) {
        showToast('No logs to export', 'error');
        return;
    }

    const csv = 'Person Name,Device ID,Status,Timestamp\n';
    const sortedLogs = logsToExport.sort((a, b) => b.timestamp - a.timestamp);
    
    let csvContent = csv;
    sortedLogs.forEach(log => {
        const date = new Date(log.timestamp);
        csvContent += `"${escapeCsv(log.userName)}","${escapeCsv(log.deviceId)}","${escapeCsv(log.status)}","${escapeCsv(date.toISOString())}"\n`;
    });

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', `${filenamePrefix}-${Date.now()}.csv`);
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
