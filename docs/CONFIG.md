# Configuration

## SENTICA_MIGRATION_TIMEOUT_SECONDS

How long the container entrypoint waits for database migrations, in seconds.
The default is 360.

This value must be greater than `DB_MIGRATION_LOCK_WAIT_SECONDS`
(the `MIGRATION_LOCK_WAIT_SECONDS` setting, default 300). The entrypoint
compares the two at startup and exits before it runs a migration when the
migration timeout is less than or equal to the lock wait.
