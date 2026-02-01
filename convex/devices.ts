import { v } from "convex/values";
import { action, mutation, query, internalMutation } from "./_generated/server";
import { Doc, Id } from "./_generated/dataModel";
import { internal } from "./_generated/api";

const GRACE_PERIOD_SECONDS = 300;
const DEVICE_EXPIRATION_MS = GRACE_PERIOD_SECONDS * 1000;

type CleanupResult = { deletedCount: number; deletedMacs: string[] };

type DeleteResult = { success: boolean; macAddress?: string | null };

const deleteDeviceAndLogs = async (
  ctx: any,
  deviceId: Id<"devices">
): Promise<DeleteResult> => {
  const device = await ctx.db.get(deviceId);
  if (!device) {
    return { success: false };
  }

  const relatedLogs = await ctx.db
    .query("deviceLogs")
    .withIndex("by_deviceId", (q: any) => q.eq("deviceId", deviceId))
    .collect();

  for (const log of relatedLogs) {
    await ctx.db.delete(log._id);
  }

  await ctx.db.delete(deviceId);

  return { success: true, macAddress: device.macAddress };
};

const cleanupExpiredDevicesCore = async (ctx: any): Promise<CleanupResult> => {
  const now = Date.now();

  const devices = await ctx.db.query("devices").collect();
  const expiredDevices = devices.filter((device: Doc<"devices">) => {
    if (!device.pendingRegistration) {
      return false;
    }

    const gracePeriodEnd = device.gracePeriodEnd ?? device.firstSeen + DEVICE_EXPIRATION_MS;
    return gracePeriodEnd <= now;
  });

  const deletedMacs: string[] = [];

  for (const device of expiredDevices) {
    try {
      const result = await deleteDeviceAndLogs(ctx, device._id);
      if (result.success && result.macAddress) {
        deletedMacs.push(result.macAddress);
      }
    } catch (error) {
      console.error("Failed to delete expired device", {
        deviceId: device._id,
        error,
      });
    }
  }

  return { deletedCount: deletedMacs.length, deletedMacs };
};

export const getOrganizationName = query({
  args: {},
  handler: async (ctx) => {
    // @ts-ignore - process.env is available in Convex functions
    return process.env.ORGANIZATION_NAME || "Presence Tracker";
  },
});

export const getDeviceLogs = query({
  args: { deviceId: v.id("devices") },
  handler: async (ctx, args) => {
    return await ctx.db
      .query("deviceLogs")
      .withIndex("by_deviceId", (q) => q.eq("deviceId", args.deviceId))
      .order("desc")
      .take(20);
  },
});

export const getDevices = query({
  args: {},
  handler: async (ctx) => {
    const devices = await ctx.db.query("devices").collect();
    const mappedDevices = devices.map(
      (device: Doc<"devices">) => ({
        _id: device._id,
        macAddress: device.macAddress,
        firstName: device.firstName,
        lastName: device.lastName,
        name: device.firstName && device.lastName ? `${device.firstName} ${device.lastName}` : device.name,
        status: device.status,
        lastSeen: device.lastSeen,
        connectedSince: device.connectedSince,
        pendingRegistration: device.pendingRegistration,
      }),
    );
    return mappedDevices;
  },
});

export const upsertDevice = mutation({
  args: {
    macAddress: v.string(),
    name: v.string(),
    status: v.string(),
  },
  handler: async (ctx, args) => {
    const existingDevice = await ctx.db
      .query("devices")
      .withIndex("by_macAddress", (q) => q.eq("macAddress", args.macAddress))
      .first();

    const now = Date.now();

    if (existingDevice) {
      await ctx.db.patch(existingDevice._id, {
        name: args.name,
        status: args.status,
        lastSeen: now,
      });
      await ctx.db.insert("deviceLogs", {
        deviceId: existingDevice._id,
        changeType: "update",
        timestamp: now,
        details: `Name updated to: ${args.name}`
      });
      return { ...existingDevice, name: args.name, status: args.status, lastSeen: now };
    } else {
      // Fix: New devices should be pending by default
      const gracePeriodEnd = now + GRACE_PERIOD_SECONDS * 1000;
      const deviceId = await ctx.db.insert("devices", {
        macAddress: args.macAddress,
        name: args.name,
        status: args.status,
        lastSeen: now,
        firstSeen: now,
        gracePeriodEnd,
        pendingRegistration: true,
      });
      await ctx.db.insert("deviceLogs", {
        deviceId,
        changeType: "create",
        timestamp: now,
        details: `Device created: ${args.name}`
      });
      return {
        _id: deviceId,
        macAddress: args.macAddress,
        name: args.name,
        status: args.status,
        lastSeen: now,
        firstSeen: now,
        gracePeriodEnd,
        pendingRegistration: true,
      };
    }
  },
});

export const updateDeviceStatus = mutation({
  args: {
    macAddress: v.string(),
    status: v.string(),
  },
  handler: async (ctx, args) => {
    const existingDevice = await ctx.db
      .query("devices")
      .withIndex("by_macAddress", (q) => q.eq("macAddress", args.macAddress))
      .first();

    if (!existingDevice) {
      throw new Error(`Device with MAC address ${args.macAddress} not found`);
    }

    const now = Date.now();

    // Note: We no longer delete pending devices when they go absent
    // They will remain in the database for manual review

    // Logic for connectedSince
    let connectedSince = existingDevice.connectedSince;
    if (args.status === "present" && existingDevice.status !== "present") {
      // Just connected
      connectedSince = now;
    }
    // If staying present, keep connectedSince. 
    // If absent, we can keep it or clear it. Usually we keep it for "Connected at X", but if absent "Last Seen Y".
    // When showing "Connected at", we use connectedSince.

    // Log status change if meaningful (e.g. absent <-> present)
    if (existingDevice.status !== args.status) {
      await ctx.db.insert("deviceLogs", {
        deviceId: existingDevice._id,
        changeType: "status_change",
        timestamp: now,
        details: `Status changed from ${existingDevice.status} to ${args.status}`
      });
    }

    await ctx.db.patch(existingDevice._id, {
      status: args.status,
      lastSeen: now,
      connectedSince: connectedSince,
    });

    return {
      ...existingDevice,
      status: args.status,
      lastSeen: now,
      connectedSince: connectedSince,
    };
  },
});

export const registerDevice = mutation({
  args: {
    macAddress: v.string(),
    name: v.string(),
  },
  handler: async (ctx, args) => {
    const existingDevice = await ctx.db
      .query("devices")
      .withIndex("by_macAddress", (q) => q.eq("macAddress", args.macAddress))
      .first();

    if (existingDevice) {
      return existingDevice;
    }

    const now = Date.now();
    const deviceId = await ctx.db.insert("devices", {
      macAddress: args.macAddress,
      name: args.name,
      status: "absent",
      lastSeen: now,
      firstSeen: now,
      gracePeriodEnd: now,
      pendingRegistration: false,
    });
    await ctx.db.insert("deviceLogs", {
      deviceId,
      changeType: "create",
      timestamp: now,
      details: `Device registered: ${args.name}`
    });

    return {
      _id: deviceId,
      macAddress: args.macAddress,
      name: args.name,
      status: "absent",
      lastSeen: now,
      firstSeen: now,
      gracePeriodEnd: now,
      pendingRegistration: false,
    };
  },
});

export const registerPendingDevice = mutation({
  args: {
    macAddress: v.string(),
    name: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const existingDevice = await ctx.db
      .query("devices")
      .withIndex("by_macAddress", (q) => q.eq("macAddress", args.macAddress))
      .first();

    if (existingDevice) {
      return existingDevice;
    }

    const now = Date.now();
    const gracePeriodEnd = now + GRACE_PERIOD_SECONDS * 1000;

    const deviceName = args.name || "";

    const deviceId = await ctx.db.insert("devices", {
      macAddress: args.macAddress,
      name: deviceName,
      status: "present",
      lastSeen: now,
      firstSeen: now,
      gracePeriodEnd,
      pendingRegistration: true,
    });
    await ctx.db.insert("deviceLogs", {
      deviceId,
      changeType: "create",
      timestamp: now,
      details: `Pending device created: ${deviceName || args.macAddress}`
    });

    return {
      _id: deviceId,
      macAddress: args.macAddress,
      name: deviceName,
      status: "present",
      lastSeen: now,
      firstSeen: now,
      gracePeriodEnd,
      pendingRegistration: true,
    };
  },
});

export const completeDeviceRegistration = mutation({
  args: {
    macAddress: v.string(),
    firstName: v.string(),
    lastName: v.string(),
  },
  handler: async (ctx, args) => {
    const existingDevice = await ctx.db
      .query("devices")
      .withIndex("by_macAddress", (q) => q.eq("macAddress", args.macAddress))
      .first();

    if (!existingDevice) {
      throw new Error(`Device with MAC address ${args.macAddress} not found`);
    }

    const now = Date.now();
    await ctx.db.patch(existingDevice._id, {
      firstName: args.firstName,
      lastName: args.lastName,
      pendingRegistration: false,
      lastSeen: now,
      connectedSince: now,
    });

    // Log creation
    await ctx.db.insert("deviceLogs", {
      deviceId: existingDevice._id,
      changeType: "create",
      timestamp: now,
      details: `Device registered: ${args.firstName} ${args.lastName}`
    });

    return {
      ...existingDevice,
      firstName: args.firstName,
      lastName: args.lastName,
      pendingRegistration: false,
      lastSeen: now,
    };
  },
});

export const updateDeviceDetails = mutation({
  args: {
    id: v.id("devices"),
    firstName: v.string(),
    lastName: v.string(),
  },
  handler: async (ctx, args) => {
    const device = await ctx.db.get(args.id);
    if (!device) throw new Error("Device not found");

    const now = Date.now();

    await ctx.db.patch(args.id, {
      firstName: args.firstName,
      lastName: args.lastName,
    });

    // Log update
    await ctx.db.insert("deviceLogs", {
      deviceId: args.id,
      changeType: "update",
      timestamp: now,
      details: `Updated details -> Name: ${args.firstName} ${args.lastName}`
    });

    return { success: true };
  }
});

export const deleteDevice = mutation({
  args: {
    id: v.id("devices"),
  },
  handler: async (ctx, args) => {
    const result = await deleteDeviceAndLogs(ctx, args.id);
    if (!result.success) {
      return { success: false, message: "Device not found" };
    }

    return { success: true };
  },
});

export const cleanupExpiredGracePeriods = action({
  args: {},
  handler: async (ctx): Promise<CleanupResult> => {
    return ctx.runMutation(internal.devices.cleanupExpiredGracePeriodsInternal, {});
  },
});

export const cleanupExpiredGracePeriodsInternal = internalMutation({
  args: {},
  handler: async (ctx): Promise<CleanupResult> => {
    return cleanupExpiredDevicesCore(ctx);
  },
});

export const getPresentUsers = query({
  args: {},
  handler: async (ctx) => {
    const devices = await ctx.db
      .query("devices")
      .withIndex("by_status", (q) => q.eq("status", "present"))
      .collect();

    return devices
      .filter((d) => !d.pendingRegistration)
      .map((d) => ({
        firstName: d.firstName,
        lastName: d.lastName,
        name: d.name,
      }));
  },
});

export const getAbsentUsers = query({
  args: {},
  handler: async (ctx) => {
    const devices = await ctx.db
      .query("devices")
      .withIndex("by_status", (q) => q.eq("status", "absent"))
      .collect();

    return devices
      .filter((d) => !d.pendingRegistration)
      .map((d) => ({
        firstName: d.firstName,
        lastName: d.lastName,
        name: d.name,
      }));
  },
});

export const logAttendance = mutation({
  args: {
    userId: v.string(),
    userName: v.string(),
    status: v.union(v.literal("present"), v.literal("absent")),
    deviceId: v.string(),
  },
  handler: async (ctx, args) => {
    const now = Date.now();
    await ctx.db.insert("attendanceLogs", {
      userId: args.userId,
      userName: args.userName,
      status: args.status,
      timestamp: now,
      deviceId: args.deviceId,
    });
    return { success: true };
  },
});

export const getAttendanceLogs = query({
  args: {
    adminPassword: v.string(),
  },
  handler: async (ctx, args) => {
    // @ts-ignore - process.env is available in Convex functions
    const environment = process.env;
    const adminPassword = environment.ADMIN_PASSWORD;
    if (args.adminPassword !== adminPassword) {
      throw new Error("Invalid admin password");
    }

    const fourteenDaysAgo = Date.now() - 14 * 24 * 60 * 60 * 1000;
    const logs = await ctx.db
      .query("attendanceLogs")
      .withIndex("by_timestamp", (q) => q.gte("timestamp", fourteenDaysAgo))
      .order("desc")
      .collect();

    return logs;
  },
});

export const cleanupOldLogs = internalMutation({
  args: {},
  handler: async (ctx) => {
    const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
    const oldLogs = await ctx.db
      .query("attendanceLogs")
      .withIndex("by_timestamp", (q) => q.lt("timestamp", thirtyDaysAgo))
      .collect();

    for (const log of oldLogs) {
      await ctx.db.delete(log._id);
    }

    return { deletedCount: oldLogs.length };
  },
});
