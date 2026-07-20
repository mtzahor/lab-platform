# CLI

`labctl` is an HTTP client. It never constructs application services, SimLab, or physical target
adapters.

```text
labctl version
labctl health
labctl bench list
labctl bench show ID
labctl bench reserve ID --owner OWNER
labctl bench release ID --owner OWNER
labctl bench power-on ID --owner OWNER
labctl bench power-off ID --owner OWNER
labctl bench power-cycle ID --owner OWNER
labctl bench flash ID FILE --owner OWNER [--version VERSION]
labctl bench probe ID
labctl bench reset ID --owner OWNER
labctl bench serial read ID --owner OWNER [--timeout SECONDS] [--until REGEX] [--max-lines N]
labctl operation show ID
labctl operation list [--bench-id ID] [--status STATUS] [--type TYPE]
labctl operation watch ID [--interval SECONDS]
labctl operation cancel ID --owner OWNER
labctl event list [--bench-id ID] [--event-type TYPE]
```

Read commands accept `--output table` (default) or `--output json`. Agent selection order is
`--server`, `LAB_PLATFORM_SERVER`, `~/.config/lab-platform/cli.yaml`, then
`http://127.0.0.1:8080`. A CLI config can be as small as:

```yaml
server: http://127.0.0.1:8080
```

Exit codes are 0 success, 1 unexpected/invalid data, 2 CLI usage, 3 not found, 4 state conflict,
5 ownership failure, 6 backend/connection unavailable, and 7 failed or cancelled operation.

Before upload, `flash` checks that the file exists, is non-empty/readable, is at most 100 MB, and
calculates SHA-256. The Agent independently applies its configured size limit.

`bench probe` returns target health synchronously and does not require a reservation. `bench reset`
returns an operation ID. `bench serial read` submits an asynchronous operation, waits for it to
finish, retrieves the retained serial artifact, and prints its contents. Use `--output json` to
receive the terminal operation plus serial text as one JSON object.
