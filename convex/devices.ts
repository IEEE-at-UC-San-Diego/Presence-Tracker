import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { Doc } from "./_generated/dataModel";
import { internal } from "./_generated/api";

const GRACE_PERIOD_SECONDS = 300;

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
    console.log("[Convex getDevices] Raw devices from DB:", devices.map(d => ({
      _id: d._id,
      macAddress: d.macAddress,
      name: d.name,
      firstName: d.firstName,
      lastName: d.lastName,
      pendingRegistration: d.pendingRegistration,
    })));

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

    console.log("[Convex getDevices] Mapped devices sent to frontend:", mappedDevices.map(d => ({
      _id: d._id,
      macAddress: d.macAddress,
      name: d.name,
      firstName: d.firstName,
      lastName: d.lastName,
      pendingRegistration: d.pendingRegistration,
    })));

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
      // Optional: Log status changes. Might be too noisy.
      // await ctx.db.insert("deviceLogs", {
      //     deviceId: existingDevice._id,
      //     changeType: "status_change",
      //     timestamp: now,
      //     details: `Status changed from ${existingDevice.status} to ${args.status}`
      // });
    }

    await ctx.db.patch(existingDevice._id, {
      status: args.status,
      lastSeen: now,
      connectedSince: connectedSince,
    });

    if (!existingDevice.pendingRegistration) {
      await ctx.scheduler.runAfter(0, internal.notifications.updatePresenceNotifications, {});
    }

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
    console.log("[Convex registerPendingDevice] Received:", {
      macAddress: args.macAddress,
      name: args.name,
      nameType: typeof args.name,
      nameLength: args.name?.length,
    });

    const existingDevice = await ctx.db
      .query("devices")
      .withIndex("by_macAddress", (q) => q.eq("macAddress", args.macAddress))
      .first();

    if (existingDevice) {
      console.log("[Convex registerPendingDevice] Device already exists:", existingDevice._id);
      return existingDevice;
    }

    const now = Date.now();
    const gracePeriodEnd = now + GRACE_PERIOD_SECONDS * 1000;

    const deviceName = args.name || "";
    console.log("[Convex registerPendingDevice] Storing name:", {
      original: args.name,
      stored: deviceName,
      isEmpty: deviceName === "",
    });

    const deviceId = await ctx.db.insert("devices", {
      macAddress: args.macAddress,
      name: deviceName,
      status: "present",
      lastSeen: now,
      firstSeen: now,
      gracePeriodEnd,
      pendingRegistration: true,
    });

    console.log("[Convex registerPendingDevice] Created device:", deviceId, "with name:", deviceName);

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

    await ctx.scheduler.runAfter(0, internal.notifications.updatePresenceNotifications, {});

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
    // Deletion disabled - devices should never be deleted
    console.log(`[deleteDevice] Deletion attempted for ${args.id} but deletion is disabled`);
    return { success: false, message: "Device deletion is disabled" };
  },
});

export const cleanupExpiredGracePeriods = mutation({
  args: {},
  handler: async (ctx) => {
    // Cleanup disabled - devices should never be automatically deleted
    // Pending devices will remain for manual review/registration
    return { deletedCount: 0, deletedMacs: [] };
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
