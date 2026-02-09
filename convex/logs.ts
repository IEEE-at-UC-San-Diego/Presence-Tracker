import { v } from "convex/values";
import { mutation } from "./_generated/server";

/**
 * Log a device change event (create, update, etc.)
 * This is for device metadata changes only (registration, name edits, etc.),
 * NOT for connection/disconnection status changes.
 */
export const logDeviceChange = mutation({
  args: {
    deviceId: v.id("devices"),
    changeType: v.string(),
    details: v.string(),
  },
  handler: async (ctx, args) => {
    const now = Date.now();
    await ctx.db.insert("deviceLogs", {
      deviceId: args.deviceId,
      changeType: args.changeType,
      timestamp: now,
      details: args.details,
    });
    return { success: true };
  },
});
