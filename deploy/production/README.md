# Production Compose template

Run `lab-platform init <directory>` to copy this template and generate the three mounted secret
files. Review `.env`, DNS, firewall rules, storage capacity, and the production security checklist
before starting it.

The only published ports are Caddy’s 80/443. PostgreSQL and the plaintext control-plane hop stay on
the private `172.30.0.0/24` bridge; only the fixed proxy address `172.30.0.5/32` is trusted to set
forwarded headers. If that subnet overlaps an existing Docker network, change the subnet, all
static addresses, and `LAB_TRUSTED_PROXY_NETWORKS` together.

Never commit `.env`, `secrets/`, database data, artifact data, or Caddy state. Validate before the
first start:

```console
docker compose run --rm control-plane config validate --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane production-check --config /etc/lab-platform/control-plane.yaml
docker compose up -d
```

Continue with `docs/PRODUCTION_DEPLOYMENT.md`, `docs/BACKUP_RESTORE.md`, and
`docs/PRODUCTION_SECURITY.md`.
