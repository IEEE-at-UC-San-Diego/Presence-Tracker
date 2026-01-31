// Button injection removed -- handled in index.html

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
            <label>Display Name (Space Name)</label>
            <input type="text" id="discord-display-name" placeholder="Project Space" value="${discord?.config?.displayName || ''}">
        </div>
        <div class="form-group">
            <label>Webhook URL</label>
            <input type="text" id="discord-webhook" placeholder="https://discord.com/api/webhooks/..." value="${discord?.config?.webhookUrl || ''}">
        </div>
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="discord-use-embeds" ${discord?.config?.useEmbeds ? 'checked' : ''}>
                Use rich embeds (Discord only)
            </label>
        </div>
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="discord-show-absent" ${discord?.config?.showAbsentUsers ? 'checked' : ''}>
                Show "Currently OUT" users
            </label>
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
            <label>Display Name (Space Name)</label>
            <input type="text" id="slack-display-name" placeholder="Project Space" value="${slack?.config?.displayName || ''}">
        </div>
        <div class="form-group">
            <label>Bot User OAuth Token (xoxb-...)</label>
            <input type="text" id="slack-token" placeholder="xoxb-..." value="${slack?.config?.botToken || ''}">
        </div>
        <div class="form-group">
            <label>Channel ID</label>
            <input type="text" id="slack-channel" placeholder="C12345678" value="${slack?.config?.channelId || ''}">
        </div>
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="slack-show-absent" ${slack?.config?.showAbsentUsers ? 'checked' : ''}>
                Show "Currently OUT" users
            </label>
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
    const displayName = document.getElementById('discord-display-name').value.trim();
    const useEmbeds = document.getElementById('discord-use-embeds').checked;
    const showAbsentUsers = document.getElementById('discord-show-absent').checked;

    try {
        await window.convexClient.mutation("integrations:saveIntegration", {
            type: "discord",
            config: { 
                webhookUrl, 
                displayName: displayName || undefined,
                useEmbeds,
                showAbsentUsers
            },
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
    const displayName = document.getElementById('slack-display-name').value.trim();
    const showAbsentUsers = document.getElementById('slack-show-absent').checked;

    try {
        await window.convexClient.mutation("integrations:saveIntegration", {
            type: "slack",
            config: { 
                botToken, 
                channelId,
                displayName: displayName || undefined,
                showAbsentUsers
            },
            isEnabled
        });
        showToast('Slack settings saved');
    } catch (e) {
        showToast('Error saving Slack: ' + e.message, 'error');
    }
}
