"use node";
import { action } from "./_generated/server";
import { api, internal } from "./_generated/api";

export const updatePresenceNotifications = action({
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

            try {
                if (integration.type === "discord" && integration.config.webhookUrl) {
                    await handleDiscord(ctx, integration._id, integration.config.webhookUrl, message);
                } else if (integration.type === "slack" && integration.config.botToken && integration.config.channelId) {
                    await handleSlack(ctx, integration._id, integration.config.botToken, integration.config.channelId, message);
                }
            } catch (e) {
                console.error(`Failed to handle integration ${integration._id} (${integration.type}):`, e);
            }
        }
    },
});

async function handleDiscord(ctx: any, integrationId: any, webhookUrl: string, content: string) {
    // Get active message
    const activeMsg = await ctx.runQuery(internal.integrations.getActiveMessage, { integrationId });

    let messageSent = false;

    if (activeMsg) {
        // Try to edit
        try {
            // Discord Webhook Edit URL: webhook_url/messages/message_id
            const editUrl = `${webhookUrl}/messages/${activeMsg.messageId}`;
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
        // Send new message
        // Append ?wait=true to get message object back
        const res = await fetch(`${webhookUrl}?wait=true`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content }),
        });

        if (res.ok) {
            const data = await res.json();
            if (data.id) {
                await ctx.runMutation(internal.integrations.updateActiveMessage, {
                    integrationId,
                    messageId: data.id,
                });
            }
        }
    }
}

async function handleSlack(ctx: any, integrationId: any, token: string, channel: string, content: string) {
    const activeMsg = await ctx.runQuery(internal.integrations.getActiveMessage, { integrationId });
    let messageSent = false;

    if (activeMsg) {
        // Try update
        const res = await fetch("https://slack.com/api/chat.update", {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                channel: channel,
                ts: activeMsg.messageId, // timestamp is the ID in slack
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
        if (data.ok) {
            await ctx.runMutation(internal.integrations.updateActiveMessage, {
                integrationId,
                messageId: data.ts,
                channelId: data.channel
            });
        }
    }
}
