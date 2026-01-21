// Initialize Convex Client
window.convexClient = new convex.ConvexClient(window.CONVEX_URL);
const convexClient = window.convexClient;

// State
let selectedMacForRegistration = null;
let subscriptionInitialized = false;

document.addEventListener('DOMContentLoaded', () => {
    // Only setup subscription if already authenticated (session persisted)
    if (sessionStorage.getItem('ieee_presence_authenticated') === 'true') {
        initializeApp();
    }
});

// Expose this function for auth.js to call after successful login
window.initializeApp = function () {
    if (!subscriptionInitialized) {
        setupConvexSubscription();
        subscriptionInitialized = true;
    }
}

function setupConvexSubscription() {
    // Subscribe to the getDevices query
    convexClient.onUpdate("devices:getDevices", {}, (devices) => {
        renderDevices(devices);
    });
}

function renderDevices(devices) {
    const residentsGrid = document.getElementById('residents-grid');
    const pendingList = document.getElementById('pending-list');
    const residentsCount = document.getElementById('residents-count');

    console.log("[Frontend renderDevices] Received devices:", devices.map(d => ({
        _id: d._id,
        macAddress: d.macAddress,
        name: d.name,
        firstName: d.firstName,
        lastName: d.lastName,
        pendingRegistration: d.pendingRegistration,
    })));

    // Filter devices
    // Note: The new plan says "Confirmed devices" = !pendingRegistration (Registered)
    // "New/Pending devices" = pendingRegistration (Pending)

    // Fix: Treat undefined/null as pending (only explicitly false means registered)
    const residents = devices.filter(d => d.pendingRegistration === false);

    // Sort: Active > Inactive, then Alphabetical
    residents.sort((a, b) => {
        // 1. Sort by Status (Active > Inactive)
        const aActive = a.status === 'present';
        const bActive = b.status === 'present';

        if (aActive && !bActive) return -1;
        if (!aActive && bActive) return 1;

        // 2. Sort by Name (Alphabetical)
        const aName = (a.firstName && a.lastName) ? `${a.firstName} ${a.lastName}` : (a.name || a.macAddress);
        const bName = (b.firstName && b.lastName) ? `${b.firstName} ${b.lastName}` : (b.name || b.macAddress);

        return aName.localeCompare(bName);
    });

    const pending = devices.filter(d => d.pendingRegistration !== false);

    // Update Counts
    residentsCount.textContent = residents.length;

    // Render Residents using reconciliation
    reconcileResidents(residentsGrid, residents);

    // Render Pending Devices using reconciliation
    reconcilePending(pendingList, pending);
}

// Helper to create a resident card element
function createResidentCard(device) {
    const isPresent = device.status === 'present';
    const statusClass = isPresent ? 'present' : 'away';

    const fullName = device.firstName && device.lastName
        ? `${device.firstName} ${device.lastName}`
        : (device.name || "Unknown");

    let timeMessage = '';
    if (isPresent) {
        if (device.connectedSince) {
            const connectedDate = new Date(device.connectedSince);
            const timeStr = connectedDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            timeMessage = `Connected at ${timeStr}`;
        } else {
            timeMessage = `Connected`;
        }
    } else {
        timeMessage = `Last seen: ${formatTimeAgo(device.lastSeen)}`;
    }

    const card = document.createElement('div');
    card.className = `resident-card ${statusClass}`;
    card.dataset.mac = device.macAddress;
    card.innerHTML = `
        <div class="card-header">
            <div>
                <div class="user-name">${fullName}</div>
                <div class="user-mac">${device.macAddress}</div>
            </div>
            <span class="status-badge ${statusClass}">
                ${isPresent ? 'Present' : 'Away'}
            </span>
        </div>
        <div class="last-seen">
            ${timeMessage}
        </div>
        ${window.isAdmin && window.isAdmin() ? `
        <div class="admin-actions" style="margin-top: 10px; display: flex; justify-content: flex-end; gap: 8px;">
             <button class="btn btn-secondary" style="font-size: 0.7rem; padding: 2px 6px;"
                onclick="openEditModal('${device._id}', '${device.firstName || ''}', '${device.lastName || ''}')">
                Edit
             </button>
             <button class="btn" style="font-size: 0.7rem; padding: 2px 6px; background: #e74c3c; border-color: #c0392b; color: white;"
                onclick="forgetDevice('${device._id}', '${device.macAddress}')">
                Forget
             </button>
        </div>
        ` : ''}
    `;
    return card;
}

// Helper to update a resident card in-place
function updateResidentCard(card, device) {
    const isPresent = device.status === 'present';
    const statusClass = isPresent ? 'present' : 'away';

    const fullName = device.firstName && device.lastName
        ? `${device.firstName} ${device.lastName}`
        : (device.name || "Unknown");

    let timeMessage = '';
    if (isPresent) {
        if (device.connectedSince) {
            const connectedDate = new Date(device.connectedSince);
            const timeStr = connectedDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            timeMessage = `Connected at ${timeStr}`;
        } else {
            timeMessage = `Connected`;
        }
    } else {
        timeMessage = `Last seen: ${formatTimeAgo(device.lastSeen)}`;
    }

    // Update class (present/away)
    card.className = `resident-card ${statusClass}`;

    // Update content
    const userName = card.querySelector('.user-name');
    const statusBadge = card.querySelector('.status-badge');
    const lastSeen = card.querySelector('.last-seen');

    if (userName) userName.textContent = fullName;
    if (statusBadge) {
        statusBadge.className = `status-badge ${statusClass}`;
        statusBadge.textContent = isPresent ? 'Present' : 'Away';
    }
    if (lastSeen) lastSeen.textContent = timeMessage;

    // Update button onclick handlers (in case _id changed) - only if admin
    const adminActions = card.querySelector('.admin-actions');
    if (adminActions) {
        const editBtn = adminActions.querySelector('.btn-secondary');
        const forgetBtn = adminActions.querySelector('.btn:last-child');
        if (editBtn) {
            editBtn.onclick = () => openEditModal(device._id, device.firstName || '', device.lastName || '');
        }
        if (forgetBtn) {
            forgetBtn.onclick = () => forgetDevice(device._id, device.macAddress);
        }
    }
}

// Reconcile residents grid - update in-place, add new, remove old
function reconcileResidents(container, residents) {
    // Handle empty state
    if (residents.length === 0) {
        container.innerHTML = '<div class="empty-state">No IEEE members registered yet.</div>';
        return;
    }

    // Remove loading state and empty state if present
    const loadingState = container.querySelector('.loading-state');
    const emptyState = container.querySelector('.empty-state');
    if (loadingState || emptyState) {
        container.innerHTML = '';
    }

    // Build a map of current cards by MAC address
    const existingCards = new Map();
    container.querySelectorAll('.resident-card[data-mac]').forEach(card => {
        existingCards.set(card.dataset.mac, card);
    });

    // Track which MACs are in the new data
    const newMacs = new Set(residents.map(d => d.macAddress));

    // Remove cards that are no longer in the data
    existingCards.forEach((card, mac) => {
        if (!newMacs.has(mac)) {
            card.style.opacity = '0';
            card.style.transform = 'scale(0.95)';
            setTimeout(() => card.remove(), 200);
        }
    });

    // Update existing cards or add new ones
    residents.forEach(device => {
        const existingCard = existingCards.get(device.macAddress);
        if (existingCard) {
            // Update in-place
            updateResidentCard(existingCard, device);
        } else {
            // Create new card
            const newCard = createResidentCard(device);
            newCard.style.opacity = '0';
            newCard.style.transform = 'scale(0.95)';
            container.appendChild(newCard);
            // Trigger animation
            requestAnimationFrame(() => {
                newCard.style.transition = 'opacity 0.2s ease, transform 0.2s ease';
                newCard.style.opacity = '1';
                newCard.style.transform = 'scale(1)';
            });
        }
    });
}

// Helper to create a pending item element
function createPendingItem(device) {
    const item = document.createElement('div');
    item.className = 'pending-item';
    item.dataset.mac = device.macAddress;
    item.innerHTML = `
        <div class="pending-info">
            <div class="device-details">
                <strong>${device.name || 'Unknown Device'}</strong>
                <span>${device.macAddress}</span>
            </div>
        </div>
        <button class="btn btn-primary" onclick="openModal('${device.macAddress}')">
            Register
        </button>
    `;
    return item;
}

// Reconcile pending list - update in-place, add new, remove old
function reconcilePending(container, pending) {
    // Handle empty state
    if (pending.length === 0) {
        container.innerHTML = '<div class="empty-state">No new devices nearby</div>';
        return;
    }

    // Remove loading state and empty state if present
    const loadingState = container.querySelector('.loading-state');
    const emptyState = container.querySelector('.empty-state');
    if (loadingState || emptyState) {
        container.innerHTML = '';
    }

    // Build a map of current items by MAC address
    const existingItems = new Map();
    container.querySelectorAll('.pending-item[data-mac]').forEach(item => {
        existingItems.set(item.dataset.mac, item);
    });

    // Track which MACs are in the new data
    const newMacs = new Set(pending.map(d => d.macAddress));

    // Remove items that are no longer in the data
    existingItems.forEach((item, mac) => {
        if (!newMacs.has(mac)) {
            item.style.opacity = '0';
            item.style.transform = 'translateX(-10px)';
            setTimeout(() => item.remove(), 200);
        }
    });

    // Update existing items or add new ones
    pending.forEach(device => {
        const existingItem = existingItems.get(device.macAddress);
        if (existingItem) {
            // Update device name if changed
            const nameEl = existingItem.querySelector('.device-details strong');
            if (nameEl) nameEl.textContent = device.name || 'Unknown Device';
        } else {
            // Create new item
            const newItem = createPendingItem(device);
            newItem.style.opacity = '0';
            newItem.style.transform = 'translateX(-10px)';
            container.appendChild(newItem);
            // Trigger animation
            requestAnimationFrame(() => {
                newItem.style.transition = 'opacity 0.2s ease, transform 0.2s ease';
                newItem.style.opacity = '1';
                newItem.style.transform = 'translateX(0)';
            });
        }
    });
}

// Modal Functions
window.openModal = function (macAddress) {
    selectedMacForRegistration = macAddress;
    document.getElementById('modal-mac').textContent = macAddress;
    document.getElementById('device-firstname').value = '';
    document.getElementById('device-lastname').value = '';
    document.getElementById('registration-modal').classList.add('active');
    document.getElementById('device-firstname').focus();
}

window.closeModal = function () {
    document.getElementById('registration-modal').classList.remove('active');
    selectedMacForRegistration = null;
}

// Submit Registration
window.submitRegistration = async function () {
    const firstName = document.getElementById('device-firstname').value.trim();
    const lastName = document.getElementById('device-lastname').value.trim();

    if (!firstName || !lastName) {
        showToast('Please enter first and last name', 'error');
        return;
    }

    if (!selectedMacForRegistration) return;

    // Show loading state on button
    const btn = document.querySelector('.modal-footer .btn-primary');
    const originalText = btn.textContent;
    btn.textContent = 'Registering...';
    btn.disabled = true;

    try {
        // Call the mutation
        await convexClient.mutation("devices:completeDeviceRegistration", {
            macAddress: selectedMacForRegistration,
            firstName: firstName,
            lastName: lastName
        });

        showToast('Device registered successfully!', 'success');
        closeModal();
    } catch (error) {
        console.error("Registration failed:", error);
        showToast('Registration failed: ' + error.message, 'error');
    } finally {
        btn.textContent = originalText;
        btn.disabled = false;
    }
}

// Edit Modal Functions
window.openEditModal = function (id, firstName, lastName) {
    document.getElementById('edit-device-id').value = id;
    document.getElementById('edit-firstname').value = firstName === 'undefined' ? '' : firstName;
    document.getElementById('edit-lastname').value = lastName === 'undefined' ? '' : lastName;
    document.getElementById('edit-modal').classList.add('active');

    // Fetch Logs
    const logsContainer = document.getElementById('edit-logs');
    if (logsContainer) {
        logsContainer.innerHTML = 'Loading logs...';
        convexClient.query("devices:getDeviceLogs", { deviceId: id }).then(logs => {
            if (logs.length === 0) {
                logsContainer.innerHTML = '<div style="color: var(--text-secondary); padding: 4px;">No logs found.</div>';
                return;
            }
            logsContainer.innerHTML = logs.map(log => {
                const date = new Date(log.timestamp).toLocaleString();
                return `
                    <div style="padding: 4px 0; border-bottom: 1px dashed var(--border-light);">
                        <div style="color: var(--text-secondary); font-size: 0.7rem;">${date}</div>
                        <div>${log.details}</div>
                    </div>
                `;
            }).join('');
        }).catch(err => {
            console.error(err);
            logsContainer.innerHTML = 'Error loading logs.';
        });
    }
}

window.closeEditModal = function () {
    document.getElementById('edit-modal').classList.remove('active');
}

window.submitEdit = async function () {
    const id = document.getElementById('edit-device-id').value;
    const firstName = document.getElementById('edit-firstname').value.trim();
    const lastName = document.getElementById('edit-lastname').value.trim();

    if (!firstName || !lastName) {
        showToast('First and Last name are required', 'error');
        return;
    }

    try {
        await convexClient.mutation("devices:updateDeviceDetails", {
            id: id,
            firstName: firstName,
            lastName: lastName
        });
        showToast('Device updated', 'success');
        closeEditModal();
    } catch (err) {
        console.error(err);
        showToast('Update failed: ' + err.message, 'error');
    }
}

window.forgetDevice = async function (deviceId, macAddress) {
    if (!confirm(`Are you sure you want to forget this device? This will remove it from the system and unpair it from Bluetooth.\n\nMAC: ${macAddress}`)) {
        return;
    }

    try {
        await convexClient.mutation("devices:deleteDevice", { id: deviceId });

        const response = await fetch('/api/forget-device', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ macAddress })
        });

        if (!response.ok) {
            throw new Error(`Backend error: ${response.statusText}`);
        }

        showToast('Device forgotten successfully', 'success');
    } catch (err) {
        console.error(err);
        showToast('Failed to forget device: ' + err.message, 'error');
    }
}

// Toast Notification
function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type} active`;

    setTimeout(() => {
        toast.classList.remove('active');
    }, 3000);
}

// Helper: Format Time
function formatTimeAgo(timestamp) {
    if (!timestamp) return 'Never';

    const diff = (Date.now() - timestamp) / 1000; // seconds

    if (diff < 60) return 'Just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

// Close modal on outside click
window.addEventListener('click', (e) => {
    const modal = document.getElementById('registration-modal');
    if (e.target === modal) {
        closeModal();
    }
});
