import { mutation, query, internalQuery, internalMutation } from "./_generated/server";
import { v } from "convex/values";

export const getIntegrations = query({
    handler: async (ctx) => {
        return await ctx.db.query("integrations").collect();
    },
});

export const saveIntegration = mutation({
    args: {
        type: v.union(v.literal("discord"), v.literal("slack")),
        config: v.object({
            webhookUrl: v.optional(v.string()),
            botToken: v.optional(v.string()),
            channelId: v.optional(v.string()),
            displayName: v.optional(v.string()),
            useEmbeds: v.optional(v.boolean()),
            showAbsentUsers: v.optional(v.boolean()),
        }),
        isEnabled: v.boolean(),
    },
    handler: async (ctx, args) => {
        const existing = await ctx.db
            .query("integrations")
            .withIndex("by_type", (q) => q.eq("type", args.type))
            .first();

        if (existing) {
            await ctx.db.patch(existing._id, {
                config: args.config,
                isEnabled: args.isEnabled,
            });
        } else {
            await ctx.db.insert("integrations", {
                type: args.type,
                config: args.config,
                isEnabled: args.isEnabled,
            });
        }
    },
});

export const toggleIntegration = mutation({
    args: {
        id: v.id("integrations"),
        isEnabled: v.boolean(),
    },
    handler: async (ctx, args) => {
        await ctx.db.patch(args.id, { isEnabled: args.isEnabled });
    },
});

export const deleteIntegration = mutation({
    args: {
        id: v.id("integrations"),
    },
    handler: async (ctx, args) => {
        await ctx.db.delete(args.id);
    },
});

export const getIntegrationMessage = internalQuery({
    args: { platform: v.union(v.literal("slack"), v.literal("discord")) },
    handler: async (ctx, args) => {
        return await ctx.db
            .query("integrationMessages")
            .withIndex("by_platform", (q) => q.eq("platform", args.platform))
            .first();
    },
});

export const updateIntegrationMessage = internalMutation({
    args: {
        platform: v.union(v.literal("slack"), v.literal("discord")),
        messageId: v.string(),
        channelId: v.optional(v.string()),
    },
    handler: async (ctx, args) => {
        const existing = await ctx.db
            .query("integrationMessages")
            .withIndex("by_platform", (q) => q.eq("platform", args.platform))
            .first();

        const timestamp = Date.now();

        if (existing) {
            await ctx.db.patch(existing._id, {
                messageId: args.messageId,
                channelId: args.channelId,
                lastUpdateTimestamp: timestamp,
            });
        } else {
            await ctx.db.insert("integrationMessages", {
                platform: args.platform,
                messageId: args.messageId,
                channelId: args.channelId,
                lastUpdateTimestamp: timestamp,
            });
        }
    },
});

export const updateIntegrationMessageId = internalMutation({
    args: {
        id: v.id("integrations"),
        messageId: v.string(),
    },
    handler: async (ctx, args) => {
        await ctx.db.patch(args.id, {
            messageId: args.messageId,
        });
    },
});

