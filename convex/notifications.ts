"use node";
import { internalAction } from "./_generated/server";
import { api, internal } from "./_generated/api";

export const updatePresenceNotifications = internalAction({
    args: {},
    handler: async (ctx) => {
        // 1. Get present users
        const users = await ctx.runQuery(api.devices.getPresentUsers);

        // 2. Prepare user list
        const userList = users.map((u: any) =>
            (u.firstName && u.lastName) ? `${u.firstName} ${u.lastName}` : (u.name || "Unknown")
        );

        // 3. Get integrations
        const integrations = await ctx.runQuery(api.integrations.getIntegrations);

        // 4. Process integrations
        for (const integration of integrations) {
            if (!integration.isEnabled) continue;

            let message = "";
            if (userList.length === 0) {
                message = "Project Space is currently empty.";
            } else {
                const header = integration.type === "discord"
                    ? "**Currently in Project Space:**"
                    : "Currently in Project Space:";

                message = `${header}\n` + userList.map(n => `• ${n}`).join("\n");
            }

            // Append timestamp
            const now = new Date();
            const formatter = new Intl.DateTimeFormat('en-US', {
                timeZone: 'America/Los_Angeles',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false,
                timeZoneName: 'short'
            });
            const parts = formatter.formatToParts(now);
            const year = parts.find(p => p.type === 'year')?.value || '';
            const month = parts.find(p => p.type === 'month')?.value || '';
            const day = parts.find(p => p.type === 'day')?.value || '';
            const hour = parts.find(p => p.type === 'hour')?.value || '';
            const minute = parts.find(p => p.type === 'minute')?.value || '';
            const second = parts.find(p => p.type === 'second')?.value || '';
            const tzName = parts.find(p => p.type === 'timeZoneName')?.value || 'PST';
            const timestamp = `${year}-${month}-${day} ${hour}:${minute}:${second} ${tzName}`;
            message = `${message}\n\n_Last updated: ${timestamp}_`;

            try {
                if (integration.type === "discord" && integration.config.webhookUrl) {
                    await handleDiscord(ctx, integration.type, integration.config.webhookUrl, message);
                } else if (integration.type === "slack" && integration.config.botToken && integration.config.channelId) {
                    await handleSlack(ctx, integration.type, integration.config.botToken, integration.config.channelId, message);
                }
            } catch (e) {
                // Error handling for integration failures
            }
        }
    },
});

async function handleDiscord(ctx: any, platform: "discord", webhookUrl: string, content: string) {
    const integrationMessage = await ctx.runQuery(api.integrations.getIntegrationMessage, { platform });
    let messageSent = false;

    if (integrationMessage?.messageId) {
        // Try to edit existing message
        try {
            const editUrl = `${webhookUrl}/messages/${integrationMessage.messageId}`;
            const res = await fetch(editUrl, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content }),
            });
            if (res.ok) {
                messageSent = true;
            }
        } catch (e) {
            console.error("Discord edit error:", e);
        }
    }

    if (!messageSent) {
        // Send new message, use ?wait=true to get message object back
        const res = await fetch(`${webhookUrl}?wait=true`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content }),
        });
        if (!res.ok) {
            const errorText = await res.text();
            console.error("Discord post error:", res.status, errorText);
            return;
        }
        const data = await res.json();
        if (data?.id) {
            await ctx.runMutation(internal.integrations.updateIntegrationMessage, {
                platform: "discord",
                messageId: data.id,
            });
        }
    }
}

async function handleSlack(ctx: any, platform: "slack", token: string, channel: string, content: string) {
    const integrationMessage = await ctx.runQuery(api.integrations.getIntegrationMessage, { platform });
    let messageSent = false;

    if (integrationMessage?.messageId) {
        // Try update
        const res = await fetch("https://slack.com/api/chat.update", {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                channel: channel,
                ts: integrationMessage.messageId,
                text: content
            })
        });
        const data = await res.json();
        if (data.ok) {
            messageSent = true;
        }
    }

    if (!messageSent) {
        // Post new
        const res = await fetch("https://slack.com/api/chat.postMessage", {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                channel: channel,
                text: content
            })
        });
        const data = await res.json();
        if (data.ok && data.ts) {
            await ctx.runMutation(internal.integrations.updateIntegrationMessage, {
                platform: "slack",
                messageId: data.ts,
                channelId: channel,
            });
        }
    }
}
