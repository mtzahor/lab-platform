# Disposable demo — NOT FOR PRODUCTION

This deployment uses a fixed demo login, PostgreSQL trust authentication, plaintext transport on a
shared loopback namespace, and disposable named volumes. It must never be exposed beyond the local
host.

```console
docker compose -f deploy/demo/compose.yaml up --build
```

Open `http://localhost:8080` and sign in as `demo-admin` with
`LabPlatform-Demo-Only!`. Two virtual ESP32 benches and the `demo-smoke-test` workflow are created
without physical hardware or manual database edits.

Remove the entire disposable state with:

```console
docker compose -f deploy/demo/compose.yaml down --volumes
```
