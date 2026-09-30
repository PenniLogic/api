# Migration registry and lock contract (T-MIG-01, api#55)

The registry is two tables in the runner-owned schema `migration_runner` —
`migration_runner.migration_registry` and `migration_runner.migration_lock` — bootstrapped
idempotently (`CREATE SCHEMA IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`) at the
start of every writing command. They are infrastructure, like a schema-history
table, not migrations: they must exist before the first migration so that even
V001 is applied under the lock and recorded with its checksum. `--dry-run` never
bootstraps them.

Every statement the runner issues names the schema explicitly, including the
`to_regclass('migration_runner.migration_registry')` existence probe, so the registry
is one fact per database: a role or database `search_path` that omits the
schema, or `?currentSchema=` / `options=-c search_path=` in `MIGRATION_JDBC_URL`,
changes nothing — the runner still finds the same registry and never bootstraps
a second one. The application schema (`pennilogic`, created by V001) never holds
registry tables, so V001's reverse can drop it with `RESTRICT`. The schema is
named `migration_runner` rather than `migration` so that it can never coincide
with the connecting role's name: `"$user"` leads the default `search_path`, and
a schema named after the role would receive every unqualified `CREATE TABLE` a
script performs, mixing application tables into the registry schema. Nothing
but the two tables below may live in `migration_runner`.

## `migration_registry` (append-only attempt history)

| Column | Meaning |
| --- | --- |
| `id` | Identity; the order of attempts. |
| `version`, `migration_id` | `1`, `V001__create_pennilogic_schema`. |
| `phase` | `expand`, `migrate` or `contract`. |
| `checksum` | SHA-256 of the up script at the time of the attempt. |
| `reversal_kind`, `reversal_file`, `reversal_reason`, `reversal_checksum` | The reversal evidence declared by the migration when this row was written; the checksum pins the reversal script until the migration is reversed. |
| `state` | `applied`, `reversed` or `failed`. |
| `direction` | `up` or `down`: which script the attempt ran. |
| `applied_by`, `host`, `pid` | The holder identity given to the runner and where it ran. |
| `attempted_at` | Server time (`now()`), reported in UTC. |
| `duration_ms` | Wall time of the attempt. |
| `failure_sqlstate`, `failure_schema`, `failure_table`, `failure_constraint`, `failure_column`, `failure_position` | Redacted failure description; null unless `state = 'failed'`. |

The runner only inserts. The state of a version is derived: its latest
non-failed row decides whether it is `applied` or `reversed`; a latest row that
is `failed` marks it `failed` for the operator without changing what is
applied. The applied versions must form the contiguous prefix `1..n` of the
migration files and each applied row's `migration_id` and `checksum` must match
the file; any other shape stops every command with
`migration_registry_problem` (`registry_not_contiguous`,
`applied_migration_missing`, `applied_migration_renamed`, `checksum_drift`,
`reversal_checksum_drift`).

## `migration_lock` (single row)

| Column | Meaning |
| --- | --- |
| `singleton` | Always `true`; the primary key that makes the row unique. |
| `holder` | Null when free. Otherwise the identity that claimed the lock. |
| `host`, `pid` | Where the holder runs, so a dead holder can be recognised. |
| `claimed_at` | Server time of the claim. |
| `command` | `migrate` or `migrate-down`. |
| `target_version` | The version the holder is moving towards. |

A claim is `SELECT ... FOR UPDATE` on the row followed by an `UPDATE ... WHERE
holder IS NULL` in one short transaction, so two runners starting together are
serialised by Postgres and the loser reads the winner's committed claim. The
claim is committed before any migration runs and released after the last one,
whether it succeeded or failed. Every release — the runner's own and the
operator's `release-lock` — matches holder, host **and** pid, so naming a
holder string alone clears nothing. A refused claim exits 1 with
`migration_lock_refused` carrying the holder's payload, and the exception
message reads `migration lock is held by <holder> on <host> (pid <pid>) since
<claimedAt> running <command> towards V###`. A runner whose own claim is gone
at release time reports `migration_lock_release_mismatch` and exits 1.

## Claim and release payload

This is the payload shared with the T-GOV-04 pull-request check, so the lock
the check reads is the lock the runner takes. It appears verbatim as the
`lock` field of `migration_status`, as the body of `migration_lock_claimed` and
`migration_lock_refused`, and in the status file. The JSON Schema is
[lock-payload.schema.json](lock-payload.schema.json).

```json
{
  "holder": "api#2@release-2026-10",
  "host": "ci-runner-17",
  "pid": 4242,
  "claimedAt": "2026-10-03T09:41:07.123456Z",
  "command": "migrate",
  "targetVersion": 2
}
```

- `holder`: 1-120 characters of `A-Z a-z 0-9 . _ @ # / : -`, starting with a
  letter or digit. Name the ticket and the job or person, for example
  `api#2@release-2026-10` or `basiltt@laptop`. It is the value a check
  compares against the pull request's plan item; together with `host` and
  `pid` it is what an operator passes to `release-lock`.
- `claimedAt`: ISO-8601 in UTC, taken from the database server clock.
- `targetVersion`: the integer version the holder is moving towards.

A release is the same identity (`holder`, `host`, `pid`) as event
`migration_lock_released`; an operator release adds `"releasedBy": "operator"`,
and a release that matched no claim is `migration_lock_not_held` with the
same three fields.

### Ownership boundary with T-GOV-04

T-GOV-04 (infra; listed in PenniLogic/docs `planning/source/tickets-e31-governance.json`,
not yet published as a public issue at the time of writing) owns the
pull-request check that fails a second in-flight migration and names the lock
holder, and the repository-level machine-readable registry it reads. This
repository owns the runner, the database registry above, the reversal-evidence
rule (`migrationConventionCheck`) and this payload. Until T-GOV-04 publishes,
this document is the authority for the payload; a change to it is a contract
change and follows the additive-first rule.

The machine-readable record of every migration that the check needs
(identifier, repository, state, owner, reversal evidence) is available without
a database from `./gradlew migrationConventionCheck` (event `migration_set`,
one record per migration: `version`, `id`, `name`, `phase`, `owner`, `expand`,
`file`, `checksum`, `reversal.kind`, `reversal.file`, `reversal.checksum`,
`reversal.reason`) and with database state from `status`.

## Events

Every line the runner prints is one JSON object with an `event` field:

| Event | When |
| --- | --- |
| `migration_set` | `validate` succeeded; carries every record. |
| `migration_validation_failed` | A convention rule failed; `file`, `rule`. |
| `migration_plan` | Before any write: `command`, `dryRun`, `currentVersion`, `targetVersion`, `steps[]` with `action` `apply`, `hold` or `reverse`. |
| `migration_lock_claimed`, `migration_lock_released`, `migration_lock_refused` | Lock lifecycle; payload above. |
| `migration_lock_release_mismatch` | The runner's own claim was gone at release time (someone force-released it while it ran); the run exits 1. |
| `migration_lock_not_held` | `release-lock` named a holder, host and pid that do not match the current claim. |
| `migration_applied`, `migration_reversed` | One migration finished; `version`, `id`, `phase`, `checksum`, `reversal`, `reason`, `durationMs`. |
| `migration_contract_held` | A contract migration was skipped without `--include-contract`; reported on every non-dry run that holds, including one that applies nothing. |
| `migration_slow` | A migration exceeded the threshold and is still running. |
| `migration_failed` | A statement failed; `version`, `id`, `direction`, `knownVersion` and the redacted failure fields. |
| `migration_registry_problem` | Registry and files disagree; `kind` (`registry_not_contiguous`, `applied_migration_missing`, `applied_migration_renamed`, `checksum_drift`, `reversal_checksum_drift`, `registry_changed_before_lock`) and details. |
| `migration_status` | `registryPresent`, `currentVersion`, `latestVersion`, `lastApplied`, `lock`, `migrations[]` (record plus `state` `pending`/`applied`/`failed`/`reversed` and `lastAttempt`). |
| `database_error` | A database error outside a migration (connection, bootstrap); codes only. |
| `runner_error` | An internal invariant failed (a missing lock row, a script that ended its own transaction) or a file could not be written (`--status-file`). |
| `usage_error` | Bad arguments or missing environment; exit 2. |

## Observability

`status --status-file <path>` (Gradle: `migrateStatus` then read the JSON) writes
the current version, last applied migration and lock holder to a file that a
dashboard or a deploy step can read without a database session. The file is
written to a sibling temporary file and renamed into place, so a reader never
sees a partial document. Registry timestamps are server time and always
reported in UTC.
