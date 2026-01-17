document.addEventListener('DOMContentLoaded', () => {
    // Add "Settings" button to header if not present
    const header = document.querySelector('.main-header');
    if (!document.getElementById('settings-btn')) {
        const btn = document.createElement('button');
        btn.id = 'settings-btn';
        btn.className = 'btn btn-secondary';
        btn.textContent = 'Settings';
        btn.style.marginLeft = 'auto';
        btn.onclick = openIntegrationsModal;
        header.appendChild(btn);
    }
});

let integrations = [];

window.openIntegrationsModal = function () {
    const modal = document.getElementById('integrations-modal');
    if (modal) {
        modal.classList.add('active');
        fetchIntegrations();
    }
}

window.closeIntegrationsModal = function () {
    document.getElementById('integrations-modal').classList.remove('active');
}

async function fetchIntegrations() {
    const list = document.getElementById('integrations-list');
    list.innerHTML = 'Loading...';

    try {
        integrations = await window.convexClient.query("integrations:getIntegrations");
        renderIntegrations();
    } catch (e) {
        list.textContent = 'Error loading integrations.';
        console.error(e);
    }
}

function renderIntegrations() {
    const list = document.getElementById('integrations-list');
    list.innerHTML = '';

    const discord = integrations.find(i => i.type === 'discord');
    const slack = integrations.find(i => i.type === 'slack');

    // Discord Section
    const discordDiv = document.createElement('div');
    discordDiv.className = 'integration-card';
    discordDiv.innerHTML = `
        <h4>Discord</h4>
        <div class="form-group">
            <label>Webhook URL</label>
            <input type="text" id="discord-webhook" placeholder="https://discord.com/api/webhooks/..." value="${discord?.config?.webhookUrl || ''}">
        </div>
        <div class="form-actions">
           <label class="switch">
              <input type="checkbox" id="discord-enabled" ${discord?.isEnabled ? 'checked' : ''}>
              <span class="slider"></span> Enabled
           </label>
           <button class="btn btn-primary" onclick="saveDiscord()">Save</button>
        </div>
    `;
    list.appendChild(discordDiv);

    // Slack Section
    const slackDiv = document.createElement('div');
    slackDiv.className = 'integration-card';
    slackDiv.innerHTML = `
        <h4>Slack</h4>
        <div class="form-group">
            <label>Bot User OAuth Token (xoxb-...)</label>
            <input type="text" id="slack-token" placeholder="xoxb-..." value="${slack?.config?.botToken || ''}">
        </div>
        <div class="form-group">
            <label>Channel ID</label>
            <input type="text" id="slack-channel" placeholder="C12345678" value="${slack?.config?.channelId || ''}">
        </div>
        <div class="form-actions">
           <label class="switch">
              <input type="checkbox" id="slack-enabled" ${slack?.isEnabled ? 'checked' : ''}>
               <span class="slider"></span> Enabled
           </label>
           <button class="btn btn-primary" onclick="saveSlack()">Save</button>
        </div>
    `;
    list.appendChild(slackDiv);
}

window.saveDiscord = async function () {
    const webhookUrl = document.getElementById('discord-webhook').value.trim();
    const isEnabled = document.getElementById('discord-enabled').checked;

    try {
        await window.convexClient.mutation("integrations:saveIntegration", {
            type: "discord",
            config: { webhookUrl },
            isEnabled
        });
        showToast('Discord settings saved');
    } catch (e) {
        showToast('Error saving Discord: ' + e.message, 'error');
    }
}

window.saveSlack = async function () {
    const botToken = document.getElementById('slack-token').value.trim();
    const channelId = document.getElementById('slack-channel').value.trim();
    const isEnabled = document.getElementById('slack-enabled').checked;

    try {
        await window.convexClient.mutation("integrations:saveIntegration", {
            type: "slack",
            config: { botToken, channelId },
            isEnabled
        });
        showToast('Slack settings saved');
    } catch (e) {
        showToast('Error saving Slack: ' + e.message, 'error');
    }
}
