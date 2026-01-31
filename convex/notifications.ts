"use node";
import { internalAction } from "./_generated/server";
import { api, internal } from "./_generated/api";

export const updatePresenceNotifications = internalAction({
    args: {},
    handler: async (ctx) => {
        // 1. Get present users
        const presentUsers = await ctx.runQuery(api.devices.getPresentUsers);

        // 2. Get integrations
        const integrations = await ctx.runQuery(api.integrations.getIntegrations);

        // 3. Process each integration
        for (const integration of integrations) {
            if (!integration.isEnabled) continue;

            const config = integration.config;
            const displayName = config.displayName || "Project Space";
            const useEmbeds = config.useEmbeds ?? false;
            const showAbsentUsers = config.showAbsentUsers ?? false;

            // Get absent users if needed
            let absentUsers: any[] = [];
            if (showAbsentUsers) {
                absentUsers = await ctx.runQuery(api.devices.getAbsentUsers);
            }

            try {
                if (integration.type === "discord" && config.webhookUrl) {
                    await handleDiscord(ctx, integration, presentUsers, absentUsers, displayName, useEmbeds, showAbsentUsers);
                } else if (integration.type === "slack" && config.botToken && config.channelId) {
                    await handleSlack(ctx, integration, presentUsers, absentUsers, displayName, showAbsentUsers);
                }
            } catch (e) {
                // Error handling for integration failures
            }
        }
    },
});

function formatUserName(user: any): string {
    if (user.firstName && user.lastName) {
        return `${user.firstName} ${user.lastName}`;
    }
    return user.name || "Unknown";
}

function formatBulletList(names: string[]): string {
    if (names.length === 0) return "None";
    return names.map(n => `- ${n}`).join("\n");
}

function formatSlackTimestamp(date: Date): string {
    // MM/DD/YYYY 2:00PM format
    const formatter = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Los_Angeles",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
    });
    const parts = formatter.formatToParts(date);
    const month = parts.find(p => p.type === "month")?.value || "";
    const day = parts.find(p => p.type === "day")?.value || "";
    const year = parts.find(p => p.type === "year")?.value || "";
    const hour = parts.find(p => p.type === "hour")?.value || "";
    const minute = parts.find(p => p.type === "minute")?.value || "";
    const dayPeriod = parts.find(p => p.type === "dayPeriod")?.value || "";
    return `${month}/${day}/${year} ${hour}:${minute}${dayPeriod}`;
}

async function handleDiscord(
    ctx: any,
    integration: any,
    presentUsers: any[],
    absentUsers: any[],
    displayName: string,
    useEmbeds: boolean,
    showAbsentUsers: boolean
) {
    const webhookUrl = integration.config.webhookUrl;
    const presentNames = presentUsers.map(formatUserName);
    const absentNames = absentUsers.map(formatUserName);

    const now = new Date();
    const unixTimestamp = Math.floor(now.getTime() / 1000);
    // Discord native timestamp format for relative time (e.g., "Today at 3:15 PM")
    const discordTimestamp = `<t:${unixTimestamp}:R>`;

    let payload: any;

    if (useEmbeds) {
        // Build Discord embed
        const fields: any[] = [
            {
                name: "Currently IN",
                value: formatBulletList(presentNames) || "None",
                inline: false,
            },
        ];

        if (showAbsentUsers) {
            fields.push({
                name: "Currently OUT",
                value: formatBulletList(absentNames) || "None",
                inline: false,
            });
        }

        payload = {
            embeds: [{
                title: `${displayName} Status`,
                description: `Currently ${presentNames.length} people IN`,
                color: 3066993,
                fields: fields,
                footer: {
                    text: `Last updated: ${discordTimestamp}`,
                },
            }],
        };
    } else {
        // Plain text message
        let content = `**${displayName} Status**\n\n`;
        content += `**Currently IN**\n`;
        content += formatBulletList(presentNames) || "None";
        content += "\n";

        if (showAbsentUsers) {
            content += `\n**Currently OUT**\n`;
            content += formatBulletList(absentNames) || "None";
            content += "\n";
        }

        content += `\n_Last updated: ${discordTimestamp}_`;
        payload = { content };
    }

    let messageSent = false;

    if (integration.messageId) {
        // Try to edit existing message
        try {
            const editUrl = `${webhookUrl}/messages/${integration.messageId}`;
            const res = await fetch(editUrl, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
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
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const errorText = await res.text();
            console.error("Discord post error:", res.status, errorText);
            return;
        }
        const data = await res.json();
        if (data?.id) {
            await ctx.runMutation(internal.integrations.updateIntegrationMessageId, {
                id: integration._id,
                messageId: data.id,
            });
        }
    }
}

async function handleSlack(
    ctx: any,
    integration: any,
    presentUsers: any[],
    absentUsers: any[],
    displayName: string,
    showAbsentUsers: boolean
) {
    const token = integration.config.botToken;
    const channel = integration.config.channelId;
    const presentNames = presentUsers.map(formatUserName);
    const absentNames = absentUsers.map(formatUserName);

    const now = new Date();
    const timestamp = formatSlackTimestamp(now);

    // Build Slack message
    let text = `*${displayName} Status*\n\n`;
    text += `*Currently IN*\n`;
    text += formatBulletList(presentNames) || "None";
    text += "\n";

    if (showAbsentUsers) {
        text += `\n*Currently OUT*\n`;
        text += formatBulletList(absentNames) || "None";
        text += "\n";
    }

    text += `\n_Last updated: ${timestamp}_`;

    const payload = { text };
    let messageSent = false;

    if (integration.messageId) {
        // Try update
        const res = await fetch("https://slack.com/api/chat.update", {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                channel: channel,
                ts: integration.messageId,
                text: text
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
                text: text
            })
        });
        const data = await res.json();
        if (data.ok && data.ts) {
            await ctx.runMutation(internal.integrations.updateIntegrationMessageId, {
                id: integration._id,
                messageId: data.ts,
            });
        }
    }
}
