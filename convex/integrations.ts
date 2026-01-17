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
        }),
        isEnabled: v.boolean(),
    },
    handler: async (ctx, args) => {
        const existing = await ctx.db
            .query("integrations")
            .filter((q) => q.eq(q.field("type"), args.type))
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

export const getActiveMessage = internalQuery({
    args: { integrationId: v.id("integrations") },
    handler: async (ctx, args) => {
        return await ctx.db
            .query("activeMessages")
            .withIndex("by_integrationId", (q) => q.eq("integrationId", args.integrationId))
            .first();
    },
});

export const updateActiveMessage = internalMutation({
    args: {
        integrationId: v.id("integrations"),
        messageId: v.string(),
        channelId: v.optional(v.string()),
    },
    handler: async (ctx, args) => {
        const existing = await ctx.db
            .query("activeMessages")
            .withIndex("by_integrationId", (q) => q.eq("integrationId", args.integrationId))
            .first();

        if (existing) {
            await ctx.db.patch(existing._id, {
                messageId: args.messageId,
                channelId: args.channelId,
            });
        } else {
            await ctx.db.insert("activeMessages", {
                integrationId: args.integrationId,
                messageId: args.messageId,
                channelId: args.channelId,
            });
        }
    },
});
