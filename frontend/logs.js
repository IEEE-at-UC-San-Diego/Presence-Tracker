// Logs Page Logic
let allLogs = [];
let currentView = 'by-person';
let selectedPerson = null;
let selectedDate = null;
let personModeSelectedPerson = null;
let dateModeSelectedDate = null;
let allModeViewMode = 'by-time';
let allModeSelectedViewMode = 'by-time';

function convertToPST_PDT(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleString('en-US', { timeZone: 'America/Los_Angeles' });
}

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
    switchTab('by-person');
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
    const allViewModeSelect = document.getElementById('all-view-mode');

    if (personSelect && !personSelect.dataset.setup) {
        personSelect.addEventListener('change', handlePersonChange);
        personSelect.dataset.setup = 'true';
    }

    if (datePicker && !datePicker.dataset.setup) {
        datePicker.addEventListener('change', handleDateChange);
        datePicker.dataset.setup = 'true';
    }

    if (allViewModeSelect && !allViewModeSelect.dataset.setup) {
        allViewModeSelect.addEventListener('change', handleAllViewModeChange);
        allViewModeSelect.dataset.setup = 'true';
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
    personModeSelectedPerson = e.target.value;
    renderCurrentView();
}

function handleDateChange(e) {
    selectedDate = e.target.value;
    dateModeSelectedDate = e.target.value;
    renderCurrentView();
}

function handleAllViewModeChange(e) {
    allModeViewMode = e.target.value;
    allModeSelectedViewMode = e.target.value;
    renderCurrentView();
}

window.switchTab = function(tabName) {
    currentView = tabName;

    document.querySelectorAll('.log-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });

    const personFilter = document.getElementById('person-filter');
    const dateFilter = document.getElementById('date-filter');
    const allFilter = document.getElementById('all-filter');
    const personSelect = document.getElementById('person-select');
    const datePicker = document.getElementById('date-picker');
    const allViewModeSelect = document.getElementById('all-view-mode');

    if (currentView === 'by-person') {
        personFilter.style.display = 'flex';
        dateFilter.style.display = 'none';
        allFilter.style.display = 'none';
        dateModeSelectedDate = selectedDate;
        selectedPerson = personModeSelectedPerson;
        if (personSelect) {
            personSelect.value = personModeSelectedPerson || '';
        }
    } else if (currentView === 'by-date') {
        personFilter.style.display = 'none';
        dateFilter.style.display = 'flex';
        allFilter.style.display = 'none';
        personModeSelectedPerson = selectedPerson;
        selectedDate = dateModeSelectedDate;
        if (datePicker) {
            datePicker.value = dateModeSelectedDate || '';
        }
    } else if (currentView === 'all') {
        personFilter.style.display = 'none';
        dateFilter.style.display = 'none';
        allFilter.style.display = 'flex';
        allModeViewMode = allModeSelectedViewMode;
        if (allViewModeSelect) {
            allViewModeSelect.value = allModeSelectedViewMode || 'by-time';
        }
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
    } else if (currentView === 'by-date') {
        renderLogsByDate(logsContent);
    } else if (currentView === 'all') {
        if (allModeViewMode === 'by-time') {
            renderLogsByTime(logsContent);
        } else {
            renderLogsByPersonPerDay(logsContent);
        }
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
                ${personLogs.map(log => renderLogEntry(log, true)).join('')}
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
    startDate.setUTCHours(0, 0, 0, 0);

    const endDate = new Date(selectedDate);
    endDate.setUTCHours(23, 59, 59, 999);

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
                <strong>${escapeHtml(new Date(selectedDate).toLocaleDateString('en-US', { timeZone: 'America/Los_Angeles' }))}</strong>
                <span class="log-count">${dateLogs.length} entries</span>
            </div>
            <div class="logs-list">
                ${dateLogs.map(log => renderLogEntry(log)).join('')}
            </div>
        </div>
    `;

    container.innerHTML = html;
}

function renderLogsByTime(container) {
    const sortedLogs = [...allLogs].sort((a, b) => b.timestamp - a.timestamp);

    let html = `
        <div class="all-time-view">
            <div class="all-time-header">
                <strong>All Logs</strong>
                <span class="log-count">${sortedLogs.length} entries</span>
            </div>
            <div class="logs-list">
                ${sortedLogs.map(log => renderLogEntry(log, false)).join('')}
            </div>
        </div>
    `;

    container.innerHTML = html;
}

function renderLogsByPersonPerDay(container) {
    const persons = [...new Set(allLogs.map(log => log.userName).filter(Boolean))].sort();
    
    let html = '';
    let totalEntries = 0;

    persons.forEach(person => {
        const personLogs = allLogs.filter(log => log.userName === person);
        totalEntries += personLogs.length;

        const dateGroups = {};
        personLogs.forEach(log => {
            const date = new Date(log.timestamp);
            const dateKey = date.toLocaleDateString('en-US', {
                timeZone: 'America/Los_Angeles',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit'
            });
            if (!dateGroups[dateKey]) {
                dateGroups[dateKey] = [];
            }
            dateGroups[dateKey].push(log);
        });

        const sortedDates = Object.keys(dateGroups).sort((a, b) => {
            const dateA = new Date(a);
            const dateB = new Date(b);
            return dateB - dateA;
        });

        html += `
            <div class="person-group-all">
                <div class="person-header">
                    <div class="person-info">
                        <strong>${escapeHtml(person)}</strong>
                    </div>
                    <span class="log-count">${personLogs.length} entries</span>
                </div>
                <div class="person-logs">
        `;

        sortedDates.forEach(dateKey => {
            const dateLogs = dateGroups[dateKey];
            dateLogs.sort((a, b) => b.timestamp - a.timestamp);

            html += `
                <div class="date-group-all">
                    <div class="date-group-header">
                        <span class="date-label">${escapeHtml(dateKey)}</span>
                        <span class="log-count">${dateLogs.length}</span>
                    </div>
                    <div class="logs-list">
                        ${dateLogs.map(log => renderLogEntry(log, true)).join('')}
                    </div>
                </div>
            `;
        });

        html += `
                </div>
            </div>
        `;
    });

    if (html === '') {
        container.innerHTML = '<div class="empty-state">No logs found.</div>';
        return;
    }

    html = `
        <div class="all-person-per-day-view">
            <div class="all-view-header">
                <strong>All Logs by Person Per Day</strong>
                <span class="log-count">${totalEntries} total entries</span>
            </div>
            ${html}
        </div>
    `;

    container.innerHTML = html;
}

function renderLogEntry(log, hidePerson = false) {
    const date = new Date(log.timestamp);
    const dateStr = date.toLocaleDateString('en-US', { timeZone: 'America/Los_Angeles' });
    const timeStr = date.toLocaleTimeString('en-US', { timeZone: 'America/Los_Angeles' });
    const statusClass = log.status === 'present' ? 'present' : 'absent';
    const statusText = log.status === 'present' ? 'Present' : 'Absent';

    return `
        <div class="log-entry">
            <div class="log-entry-header">
                ${!hidePerson ? `<div class="log-person">${escapeHtml(log.userName)}</div>` : ''}
                <div class="log-status"><span class="status-badge ${statusClass}">${statusText}</span></div>
            </div>
            <div class="log-entry-details">
                <div class="log-time">${dateStr} at ${timeStr}</div>
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
    } else if (currentView === 'by-date') {
        if (!selectedDate) {
            showToast('Please select a date to export', 'error');
            return;
        }
        const startDate = new Date(selectedDate);
        startDate.setUTCHours(0, 0, 0, 0);
        const endDate = new Date(selectedDate);
        endDate.setUTCHours(23, 59, 59, 999);
        logsToExport = allLogs.filter(log => {
            const logDate = new Date(log.timestamp);
            return logDate >= startDate && logDate <= endDate;
        });
        filenamePrefix = `logs-${selectedDate}`;
    } else if (currentView === 'all') {
        logsToExport = [...allLogs];
        filenamePrefix = 'logs-all';
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
        const pacificDateStr = date.toLocaleString('en-US', { timeZone: 'America/Los_Angeles' });
        csvContent += `"${escapeCsv(log.userName)}","${escapeCsv(log.deviceId)}","${escapeCsv(log.status)}","${escapeCsv(pacificDateStr)}"\n`;
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
