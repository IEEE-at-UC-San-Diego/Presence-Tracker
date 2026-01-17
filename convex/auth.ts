import { query } from "./_generated/server";
import { v } from "convex/values";

/**
 * Validate the provided password against the stored AUTH_PASSWORD environment variable.
 * Returns true if the password matches, false otherwise.
 */
export const validatePassword = query({
    args: { password: v.string() },
    handler: async (ctx, args) => {
        // Get the password from environment variable
        const storedPassword = process.env.AUTH_PASSWORD;

        if (!storedPassword) {
            console.error("AUTH_PASSWORD environment variable is not set");
            return { success: false, error: "Authentication not configured" };
        }

        // Compare passwords (simple string comparison)
        const isValid = args.password === storedPassword;

        return { success: isValid };
    },
});
