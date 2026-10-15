# Configuration

## SENTICA_MIGRATION_TIMEOUT_SECONDS

How long the container entrypoint waits for database migrations, in seconds.
The default is 360.

This value must be greater than `DB_MIGRATION_LOCK_WAIT_SECONDS`
(the `MIGRATION_LOCK_WAIT_SECONDS` setting, default 300). The entrypoint
compares the two at startup and exits before it runs a migration when the
migration timeout is less than or equal to the lock wait.

## Gateway

Gateway mode is off unless `SENTICA_GATEWAY_MODE` is `1`, `true`, `yes`, or
`on`. The local stack and the upstream suite leave it unset and keep using
the provider SDKs.

When it is on, every model call needs:

- `SENTICA_TENANT_ID`
- `SENTICA_GATEWAY_BASE_URL`, or a function-specific
  `SENTICA_GATEWAY_DERIVER_BASE_URL`, `SENTICA_GATEWAY_SUMMARY_BASE_URL`,
  `SENTICA_GATEWAY_DIALECTIC_BASE_URL`, `SENTICA_GATEWAY_DREAM_BASE_URL`, or
  `SENTICA_GATEWAY_EMBED_BASE_URL`
- `SENTICA_GATEWAY_DERIVER_MODEL`, `SENTICA_GATEWAY_SUMMARY_MODEL`,
  `SENTICA_GATEWAY_DIALECTIC_MODEL`, `SENTICA_GATEWAY_DREAM_MODEL`, and
  `SENTICA_GATEWAY_EMBED_MODEL`

A missing value is refused. The call does not pick another model.

`SENTICA_GATEWAY_DAILY_TOKEN_BUDGET` is optional. Unset means no limit.
`0` means the budget is already spent. The counter is in memory, per
process, per tenant, per purpose, and resets at UTC midnight.

`SENTICA_GATEWAY_RETRY_ATTEMPTS` defaults to 3.
`SENTICA_GATEWAY_RETRY_WAIT_SECONDS` defaults to 0.05. A failed attempt
retries the same URL and model. It does not fall back to another provider.
