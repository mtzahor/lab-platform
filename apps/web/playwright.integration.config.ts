import { defineConfig, devices } from "@playwright/test";

const configuredBase = process.env.E2E_BASE_URL;
const controlPlanePort = process.env.PHASE7_E2E_CONTROL_PLANE_PORT ?? "18127";
const agentPort = process.env.PHASE7_E2E_AGENT_PORT ?? "18128";
const readinessAgentPort = String(Number(agentPort) + 1);
const localBase = `http://127.0.0.1:${controlPlanePort}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/real-control-plane.spec.ts",
  timeout: 240_000,
  actionTimeout: 15_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: configuredBase ?? localBase,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium-real-control-plane", use: { ...devices["Desktop Chrome"] } }],
  webServer: configuredBase
    ? undefined
    : {
        command:
          `../../.venv/bin/python ../../scripts/phase7_e2e_environment.py ` +
          `--control-plane-port ${controlPlanePort} --agent-port ${agentPort}`,
        url: `http://127.0.0.1:${readinessAgentPort}/api/v1/health`,
        reuseExistingServer: false,
        gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
        timeout: 120_000,
      },
});
