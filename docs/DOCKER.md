# Docker images

Official multi-architecture images are:

```text
ghcr.io/mtzahor/lab-platform-control-plane
ghcr.io/mtzahor/lab-platform-agent
```

Release builds target `linux/amd64` and `linux/arm64`. Every immutable release tag has OCI source,
version, revision, creation time, documentation, and Apache-2.0 license labels; BuildKit provenance
and an SPDX SBOM are attached. Both images run as `10001:10001` and drop privileges in the
production Compose template.

## Tags

- an exact semantic version is immutable, for example `0.9.0-beta.1`;
- `stable` tracks the latest supported non-prerelease;
- `preview` tracks the latest alpha/beta/RC;
- `nightly` is mutable and unsupported; `nightly-YYYYMMDD-SHA` is its immutable counterpart.

Never use `preview` or `nightly` accidentally in production. Record the resolved image digest in
the change ticket and backup manifest.

## Verify before use

```console
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/mtzahor/lab-platform/' \
  ghcr.io/mtzahor/lab-platform-control-plane@sha256:…
```

Download the release `SHA256SUMS`, Sigstore bundle, and SPDX files from GitHub. Verification details
and trust assumptions are in [release channels](RELEASE_CHANNELS.md).

## Build inspectably from source

```console
docker build -f docker/control-plane/Dockerfile \
  --build-arg LAB_VERSION=0.9.0-beta.1 \
  --build-arg LAB_REVISION="$(git rev-parse HEAD)" \
  --build-arg LAB_BUILD_DATE="$(git show -s --format=%cI HEAD | sed 's/+00:00$/Z/')" \
  --build-arg LAB_RELEASE_CHANNEL=preview \
  -t lab-platform-control-plane:local .
```

The build consumes `uv.lock` with `uv sync --frozen`; the final stage has no compiler, package
manager operation, source checkout, or root runtime user. The dashboard is the committed and
release-verified production bundle.

## Control-plane container

Mount `/etc/lab-platform/control-plane.yaml` read-only, application/database secrets as files, and
persistent artifact storage at `/var/lib/lab-platform/artifacts`. The entry point is
`lab-control-plane`; the default command serves on port 8443. The image health check targets
`/health/live`; readiness remains an orchestrator traffic check because it intentionally reflects
required dependencies.

The production Compose root filesystem is read-only with a bounded `/tmp`. Do not bake site
configuration, TLS keys, database URLs, or enrollment credentials into an image.

## Agent container

Mount Agent YAML read-only and persistent SQLite/data/artifact directories under
`/var/lib/lab-platform`. Set `LAB_AGENT_CREDENTIAL_FILE` to a mounted secret; the entry point loads
it into the environment name referenced by Agent configuration, then immediately execs
`lab-agent`.

Physical Agents require deliberate device and group mapping, for example a reviewed
`devices: [/dev/ttyUSB0:/dev/ttyUSB0]`; never use `privileged: true` as a shortcut. Device paths,
udev ownership, USB reset behavior, and host-driver requirements vary, so the generic production
Compose does not start a hardware Agent. SimLab needs no device mapping.

The local Agent API health probe defaults to `/api/v1/health`. Agent/control-plane transport must
use WSS outside the disposable loopback demo.
