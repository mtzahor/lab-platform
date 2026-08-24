# Release channels

Lab Platform uses semantic application versions and three explicit channels. A channel communicates
risk and support; it does not replace an immutable version or digest in a production change record.

| Channel | Intended use | Mutable image tag | Support expectation |
| --- | --- | --- | --- |
| `stable` | ordinary self-hosted production | `stable` | current supported non-prerelease |
| `preview` | beta/RC evaluation and design partners | `preview` | latest announced preview only |
| `nightly` | project testing and early feedback | `nightly` | unsupported, may break without notice |

The community edition defaults to `stable`. The `0.9.0-beta` development line is `preview`; it
must not be represented as a stable or `1.0` release.
Versioned deployment templates pin their matching exact image tag, including preview templates,
so initializing from one release cannot silently select a different mutable channel artifact.

## Versions and image tags

- Release tags are `v<SemVer>`, for example `v0.9.0-beta.1` or `v0.9.0`.
- Exact image tags omit the leading `v`, for example `:0.9.0-beta.1`.
- A prerelease tag publishes to `preview`; a version without a prerelease publishes to `stable`.
- Nightly automation publishes mutable `:nightly` and immutable
  `:nightly-YYYYMMDD-<12-character-commit>` tags.
- Release tags may not contain SemVer build metadata. The commit and build time are carried as OCI
  and application build metadata instead.

Python packaging maps SemVer prereleases to PEP 440 (`0.9.0-beta.1` becomes `0.9.0b1`). The product,
Python project, and dashboard versions must agree before a release tag is accepted.

Always deploy an exact tag and record the resolved digest:

```text
ghcr.io/mtzahor/lab-platform-control-plane@sha256:…
ghcr.io/mtzahor/lab-platform-agent@sha256:…
```

Mutable channel tags are discovery conveniences. They can point to a different image later and are
not sufficient rollback evidence.

## Published release contents

The release pipeline gates and builds:

- the Python wheel and source distribution;
- the static dashboard archive;
- `linux/amd64` and `linux/arm64` control-plane and Agent images;
- SPDX source and image SBOMs;
- `SHA256SUMS` for downloadable assets;
- a Sigstore bundle for the checksum file;
- keyless signatures for image digests; and
- GitHub build-provenance/SBOM attestations.

It runs the non-hardware Python/frontend gate, verifies the open-core boundary and wheel contents,
creates the GitHub release, publishes through PyPI trusted publishing, then installs and executes
the published wheel and checks that published images run as the non-root runtime user.

Security automation separately audits locked Python/npm dependencies, reviews changed
dependencies, scans source and images for high/critical vulnerabilities, secrets, and deployment
misconfiguration, generates SBOMs, and receives automated dependency updates.

## Verify a release

Download assets from the matching GitHub release and verify them before deployment:

```console
sha256sum --check SHA256SUMS
cosign verify-blob \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/mtzahor/lab-platform/' \
  SHA256SUMS
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/mtzahor/lab-platform/' \
  ghcr.io/mtzahor/lab-platform-control-plane@sha256:…
```

Match the identity expression to the repository actually producing the release. Verification
proves provenance and integrity; it does not replace release-note review, compatibility checks, or
a tested backup.

## Channel movement

There is no in-place promotion of a nightly artifact into stable. A candidate is rebuilt from a
reviewed release commit under an explicit version tag. Preview feedback and upgrade/restore tests
inform a later stable release, which receives its own immutable artifacts and signatures.

Moving from `stable` to `preview` or `nightly` is an explicit operator choice. Moving back follows
the target release's rollback class; it is not safe merely because the channel name looks older.
See [upgrading](UPGRADING.md).

## Runtime reporting

The control plane and Agent expose application version, API/protocol versions, release channel,
commit, and build time where known. `labctl version --all` combines that metadata with the Agent
fleet. Invalid explicit `LAB_RELEASE_CHANNEL` values fail closed instead of creating an unnamed
channel.
