# Published-image deployment acceptance

This Compose file is a CI fixture for Phase 8 sections 50–52, not a user deployment template. It
uses tagged control-plane and Agent images without `build:` directives or application-source
mounts. PostgreSQL, the embedded dashboard, and a connected SimLab Agent run with no physical
hardware. Plaintext transport, PostgreSQL trust authentication, and the fixed demo login are
confined to the loopback-only fixture.

Run the complete lifecycle through the orchestrator rather than invoking this Compose file by
hand:

```console
python scripts/deployment_acceptance.py \
  --control-plane-image ghcr.io/example/lab-platform-control-plane:0.9.0-beta \
  --agent-image ghcr.io/example/lab-platform-agent:0.9.0-beta
```

The test runs a workflow and uploads an artifact, creates and deep-verifies a PostgreSQL plus
artifact backup, verifies the supported schema-11-to-current migration with persistent data from
every required entity family, removes the control-plane database and artifact volumes, restores
into fresh volumes, verifies history and artifact bytes, reconnects the preserved external Agent,
and runs another workflow. On failure, the orchestrator writes Compose logs and an evidence JSON
file under its reported work directory.
