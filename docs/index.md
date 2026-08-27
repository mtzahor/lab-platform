# Lab Platform documentation

Lab Platform provides one safe inventory and workflow API for simulated and physical hardware
across distributed Agents. Start in SimLab; physical devices are an optional next step.

## Getting started

- [Project quickstart and local demos](../README.md)
- [Self-hosted installation](SELF_HOSTING.md)
- [Production deployment](PRODUCTION_DEPLOYMENT.md)
- [Docker images and Compose](DOCKER.md)
- [Phase 9 scope and current release boundary](PHASE_9.md)

## Concepts and architecture

- [Architecture and authority boundaries](../ARCHITECTURE.md)
- [Distributed control plane and Agent protocol](PHASE_5.md)
- [Multiple backends](MULTI_BACKEND.md)
- [Reservations](RESERVATIONS.md), [scheduling](SCHEDULING.md), and [queueing](QUEUEING.md)
- [Artifacts](ARTIFACTS.md) and [results/JUnit](JUNIT_RESULTS.md)
- [Open-core and commercial boundary](OPEN_CORE_MODEL.md)

## Agent and hardware setup

- [Hardware setup and troubleshooting](HARDWARE_SETUP.md)
- [Hardware compatibility matrix/status](HARDWARE_COMPATIBILITY.md)
- [ESP32 reference setup](ESP32_SETUP.md)
- [Real backend behavior](REAL_BACKEND.md)
- [Hardware test safety/evidence](HARDWARE_TESTING.md)
- [Serial troubleshooting](TROUBLESHOOTING_SERIAL.md)
- [SimLab integration](../SIMLAB_INTEGRATION.md)

## Workflows and CI

- [Workflows and versioned document format](WORKFLOWS.md)
- [CI sessions](CI_SESSIONS.md), [cleanup](CI_CLEANUP.md), and [troubleshooting](CI_TROUBLESHOOTING.md)
- [GitHub Actions](GITHUB_ACTIONS.md), [GitLab CI](GITLAB_CI.md), and [Jenkins](JENKINS.md)
- [Artifact storage](ARTIFACT_STORAGE.md) and [live updates](LIVE_UPDATES.md)

## Web dashboard

- [Dashboard routes and usage](WEB_DASHBOARD.md)
- [Deployment](WEB_DEPLOYMENT.md), [authentication](WEB_AUTHENTICATION.md), and [permissions](WEB_PERMISSIONS.md)
- [Accessibility](WEB_ACCESSIBILITY.md) and [troubleshooting](WEB_TROUBLESHOOTING.md)

## Security and administration

- [Production security](PRODUCTION_SECURITY.md) and [security reporting](../SECURITY.md)
- [Identity](IDENTITY.md), [organisations](ORGANISATIONS.md), [teams](TEAMS.md), and [roles](ROLES_AND_PERMISSIONS.md)
- [Local authentication](LOCAL_AUTH.md), [OIDC](OIDC.md), and [service accounts](SERVICE_ACCOUNTS.md)
- [API credentials](API_CREDENTIALS.md), [legacy API tokens](API_TOKENS.md), and [audit log](AUDIT_LOG.md)
- [Security model and managed-service boundary](SECURITY_MODEL.md)

## Plugins and compatibility

- [Plugin API 1.0](../PLUGIN_API.md)
- [Plugin developer quickstart, contracts, and contribution evidence](PLUGIN_DEVELOPMENT.md)
- [Hardware compatibility](HARDWARE_COMPATIBILITY.md)
- [Stable public interfaces and deprecation](STABILITY_POLICY.md)
- [Release/LTS support policy](SUPPORT_POLICY.md)

## Operations, backup, and upgrades

- [Production reliability/load/soak/recovery validation](RELIABILITY_VALIDATION.md)
- [Health, backup, verification, and restore](BACKUP_RESTORE.md)
- [Disaster recovery](DISASTER_RECOVERY.md)
- [Retention](RETENTION.md)
- [Upgrading](UPGRADING.md) and [version compatibility](VERSION_COMPATIBILITY.md)
- [Release channels](RELEASE_CHANNELS.md)
- [Phase 9 / 1.0 readiness gate](PHASE_9_READINESS.md)

## API and CLI reference

- [REST API guide](../API.md)
- [Control-plane OpenAPI](control-plane-openapi.json)
- [Standalone Agent OpenAPI](openapi.json)
- [CLI reference](../CLI.md)

## Contributing and project history

- [Development](../DEVELOPMENT.md) and [contributing](../CONTRIBUTING.md)
- [License and trademarks](LICENSING.md)
- [Roadmap](../ROADMAP.md)
- Historical phase records: [Phase 2](PHASE_2.md), [Phase 3](PHASE_3.md),
  [Phase 4](PHASE_4.md), [Phase 5](PHASE_5.md), [Phase 6](PHASE_6.md),
  [Phase 7](PHASE_7.md), and [Phase 8](PHASE_8.md)

There is no Phase 10. Work after the Phase 9 cut line belongs to the post-1.0 backlog and is driven
by real user, maintainer, community, or commercial needs.
