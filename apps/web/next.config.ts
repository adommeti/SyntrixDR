import type { NextConfig } from "next";

// Dev/host runs hit the API on localhost:8000; the `apps` compose profile
// overrides API_INTERNAL_URL to http://api:8000 for container-to-container calls.
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Next.js 16 auto-generates AGENTS.md/CLAUDE.md on `next dev`. This repo's
  // own CLAUDE.md is hand-authored and gitignored per D-262 — never let the
  // framework write competing copies.
  agentRules: false,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_INTERNAL_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
