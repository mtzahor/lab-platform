# Self-hosting Lab Platform

The supported first production topology is one Linux VM or small server running Docker Compose,
with Agents on the machines that can physically reach lab hardware. PostgreSQL, the control plane,
artifact data, and Caddy run on the server; only ports 80/443 are published.

## Requirements

- a current Docker Engine and Docker Compose v2;
- an `amd64` or `arm64` Linux host with at least 2 CPU cores and 4 GiB RAM for evaluation;
- persistent storage sized for PostgreSQL, artifacts, audit history, backups, and Caddy state;
- a DNS name resolving to the server and inbound 80/443 for automatic public certificates, or an
  organisation-approved TLS proxy/certificate arrangement;
- outbound HTTPS to pull signed images and, when using Caddy ACME, issue certificates; and
- remote Agent hosts that can reach the public HTTPS/WSS URL.

Kubernetes and HA are not supported production modes in Phase 8. A single server is intentionally
the boring, documented path.

## Initialize an inspectable deployment

Install the matching CLI, then generate a deployment directory:

```console
lab-platform init /opt/lab-platform
cd /opt/lab-platform
```

Initialization copies `compose.yaml`, `control-plane.yaml`, `Caddyfile`, `.env`, and `README.md`.
It creates three mounted files under `secrets/`: the PostgreSQL password, complete control-plane
database URL, and application secret key. Secrets are mode `0600`; the directory is `0700`. The
command always refuses to overwrite an initialized directory. Move the old deployment aside or
choose a fresh directory, then copy reviewed non-secret settings deliberately.

Edit only the non-secret values in `.env`:

```dotenv
LAB_VERSION=0.9.0-beta
LAB_PUBLIC_HOST=lab.example.com
LAB_PUBLIC_URL=https://lab.example.com
LAB_ACME_EMAIL=lab-operators@example.com
```

The generated template pins the exact version shipped with the CLI. Keep an immutable version tag
or digest for controlled change windows. Mutable `stable` or `preview` tags are convenient for
discovery, but must not replace a recorded deployment version in an upgrade plan.

## Validate, migrate, and start

```console
docker compose run --rm control-plane config validate \
  --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane production-check \
  --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane db status \
  --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane db migrate \
  --config /etc/lab-platform/control-plane.yaml
docker compose up -d
docker compose ps
```

Do not bypass a failed production check. Inspect `docker compose logs control-plane`, then run
`doctor` after the service is reachable.

## Create the first owner without editing PostgreSQL

```console
docker compose exec control-plane lab-control-plane bootstrap-admin \
  --config /etc/lab-platform/control-plane.yaml \
  --organisation-slug engineering \
  --organisation-name "Engineering Lab" \
  --username owner \
  --display-name "Lab Owner"
```

The command prompts securely. Direct database editing is unsupported. Sign in at the public URL,
create an Agent enrollment token, and follow the generated Agent configuration. The first useful
acceptance run should use SimLab before a physical bench.

## Enroll a remote Agent

Pull the exact Agent image matching your supported compatibility window. Mount non-secret YAML and
the Agent credential separately; expose USB devices only to Agents that require them. Enrollment
uses the same `lab-agent connect --control-plane https://…` flow whether the control plane is
self-hosted or managed.

## Day-two minimum

- watch `/health/ready`, metrics, structured logs, background failures, queue depth, disk/object-
  store usage, and Agent connectivity;
- schedule PostgreSQL+artifact backups and verify restore into a fresh deployment;
- configure artifact and audit retention;
- record the installed image digests, release channel, schema version, and rollback class;
- run `doctor` and `production-check` after configuration or infrastructure changes; and
- follow [upgrading](UPGRADING.md), [backup/restore](BACKUP_RESTORE.md), [disaster recovery](DISASTER_RECOVERY.md),
  and [production security](PRODUCTION_SECURITY.md).
