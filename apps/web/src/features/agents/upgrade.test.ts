import { describe, expect, it } from "vitest";
import { agentUpgradeAssessment, agentUpgradeBadgeStatus } from "./upgrade";

describe("Agent upgrade assessment", () => {
  it("reads the detailed compatibility assessment", () => {
    expect(
      agentUpgradeAssessment({
        upgrade_status: "upgrade_available",
        upgrade: {
          status: "upgrade_recommended",
          reason: "Upgrade before the next control-plane release.",
          target_version: "0.9.0-beta",
          minimum_supported_version: "0.8.0",
          work_allowed: true,
        },
      }),
    ).toEqual({
      status: "upgrade_recommended",
      reason: "Upgrade before the next control-plane release.",
      targetVersion: "0.9.0-beta",
      minimumSupportedVersion: "0.8.0",
      workAllowed: true,
    });
  });

  it("falls back to the flat status during mixed-version upgrades", () => {
    expect(agentUpgradeAssessment({ upgrade_status: "UPGRADE_REQUIRED" })).toEqual({
      status: "upgrade_required",
      reason: undefined,
      targetVersion: undefined,
      minimumSupportedVersion: undefined,
      workAllowed: undefined,
    });
    expect(agentUpgradeBadgeStatus("upgrade_required")).toBe("UPGRADE_REQUIRED");
  });
});
