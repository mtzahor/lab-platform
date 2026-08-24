import { booleanValue, nested, stringValue } from "../../api/client";

export type AgentUpgradeAssessment = {
  status?: string;
  reason?: string;
  targetVersion?: string;
  minimumSupportedVersion?: string;
  workAllowed?: boolean;
};

export function agentUpgradeAssessment(agent: unknown): AgentUpgradeAssessment {
  const upgrade = nested(agent, "upgrade");
  const status = (
    stringValue(upgrade, "status") ?? stringValue(agent, "upgrade_status")
  )?.toLowerCase();
  const rawWorkAllowed = upgrade?.work_allowed;
  return {
    status,
    reason: stringValue(upgrade, "reason"),
    targetVersion: stringValue(upgrade, "target_version"),
    minimumSupportedVersion: stringValue(upgrade, "minimum_supported_version"),
    workAllowed:
      typeof rawWorkAllowed === "boolean" ? booleanValue(upgrade, "work_allowed") : undefined,
  };
}

export function agentUpgradeBadgeStatus(status?: string): string | undefined {
  return status?.toUpperCase();
}
