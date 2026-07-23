# CLI

`labctl` is an HTTP client. It never constructs application services, SimLab, or physical target
adapters.

```text
labctl version
labctl health
labctl bench list
labctl bench list [--online] [--available] [--capability NAME] [--label KEY=VALUE]
labctl bench show ID
labctl bench timeline ID [--category CATEGORY]
labctl bench reserve ID --owner OWNER
labctl bench release ID --owner OWNER
labctl bench power-on ID --owner OWNER
labctl bench power-off ID --owner OWNER
labctl bench power-cycle ID --owner OWNER
labctl bench flash ID FILE --owner OWNER [--version VERSION]
labctl bench probe ID --owner OWNER
labctl bench reset ID --owner OWNER
labctl bench serial read ID --owner OWNER [--timeout SECONDS] [--until REGEX] [--max-lines N]
labctl operation show ID
labctl operation list [--bench-id ID] [--status STATUS] [--type TYPE]
labctl operation watch ID [--interval SECONDS]
labctl operation cancel ID --owner OWNER
labctl event list [--bench-id ID] [--event-type TYPE]
labctl reservation list [--bench-id ID] [--owner OWNER] [--status STATUS]
labctl reservation show RESERVATION_ID
labctl reservation create BENCH --owner OWNER --duration 30m [--start RFC3339]
labctl reservation extend RESERVATION_ID --owner OWNER --duration 15m
labctl reservation release RESERVATION_ID --owner OWNER
labctl reservation cancel RESERVATION_ID --owner OWNER
labctl reservation queue BENCH --owner OWNER --duration 20m
labctl reservation queue-list BENCH
labctl reservation queue-cancel QUEUE_ID --owner OWNER
labctl workflow list
labctl workflow show NAME
labctl workflow run NAME --bench BENCH --owner OWNER [--reserve 30m] [--release-after]
labctl workflow watch RUN_ID [--interval SECONDS]
labctl workflow cancel RUN_ID --owner OWNER
```

Read commands accept `--output table` (default) or `--output json`. Agent selection order is
`--server`, `LAB_PLATFORM_SERVER`, `~/.config/lab-platform/cli.yaml`, then
`http://127.0.0.1:8080`. A CLI config can be as small as:

```yaml
server: http://127.0.0.1:8080
```

Exit codes are 0 success, 1 unexpected/invalid data, 2 CLI usage, 3 not found, 4 state conflict,
5 ownership failure, 6 backend/connection unavailable, and 7 failed/cancelled operation or workflow.

Reservation and workflow durations accept `30m`, `2h`, and compound forms such as `1h30m`.
`--input key=value` may be repeated for declared workflow placeholders.

Before upload, `flash` checks that the file exists, is non-empty/readable, is at most 100 MB, and
calculates SHA-256. The Agent independently applies its configured size limit.

`bench probe` returns target health synchronously while holding the persistent operation lock; it
therefore requires the active reservation owner. `bench reset` returns an operation ID. `bench
serial read` submits an asynchronous operation, waits for it to finish, retrieves the retained
serial artifact, and prints its contents. Use `--output json` to receive the terminal operation plus
serial text as one JSON object.
