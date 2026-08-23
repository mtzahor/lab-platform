import { defineConfig, devices } from "@playwright/test";

const configuredBase = process.env.E2E_BASE_URL;
const localBase = "http://127.0.0.1:4173";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "**/real-control-plane.spec.ts",
  timeout: 45_000,
  expect: { timeout: 8_000 },
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: configuredBase ?? localBase,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: configuredBase
    ? undefined
    : {
        command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
        url: localBase,
        reuseExistingServer: false,
        timeout: 60_000,
      },
});
