# Schema migration convention (T-MIG-01, api#55)

Every schema change in this repository is applied by the migration runner in
`com.pennilogic.migration` and by nothing else. A second apply path (an ORM
auto-DDL, a hand-run `psql` script, a `CREATE TABLE` in application start-up)
is a review failure. This document is the convention every schema ticket
references, starting with the ledger schema in api#2. The registry tables and
the lock payload are described in [REGISTRY.md](REGISTRY.md); what to do when a
run fails is in [RECOVERY.md](RECOVERY.md).

The protocol this implements is the published parallel development protocol
(PenniLogic/docs `product/04-execution-readiness-review.md`, section 8, rules 4
and 5) and `governance/DELIVERY.md`: contract-first expand-migrate-contract,
one migration in flight, reversible or compensating evidence for every change.

## Files

Migrations live in `src/main/resources/db/migrations/`. Each migration is a
pair of files in that directory:

| File | Purpose |
| --- | --- |
| `V###__snake_case_name.up.sql` | The forward change plus the header block. Required. |
| `V###__snake_case_name.down.sql` | The rehearsed reverse. Present when `reversal: down`. |
| `V###__snake_case_name.compensating.sql` | The named compensating change. Present when `reversal: compensating`. |

Rules, each enforced by `./gradlew migrationConventionCheck` (part of `check`,
so CI fails a pull request that breaks one) and by the runner before it touches
a database:

1. The name matches `V(\d{3,})__([a-z][a-z0-9]*(?:_[a-z0-9]+)*)\.(up|down|compensating)\.sql`.
   Any other file in the directory fails the check, so a typo cannot be skipped
   silently.
2. Versions are integers starting at `001` and contiguous. A gap or a duplicate
   fails. Two pull requests that both add the same next version conflict at
   the second merge, which is the intended serialization point.
3. Every version has exactly one up script and exactly one reversal script.
   A version with neither a down nor a compensating script fails with
   "reversal evidence is required". A script with no statement outside `--`
   line comments and `/* */` block comments (nested or multi-line) fails with
   "no SQL statements": an empty reverse is not evidence. This rule is
   syntactic — `SELECT 1;` passes it — so the CI round trip (apply, then
   reverse, on every pull request) is the semantic evidence that the reverse
   undoes the change.
4. The up script starts with a header block of `-- key: value` lines (a
   lowercase word, a colon, a value; other comment lines are ignored; the block
   ends at the first SQL line). Required: `phase`, `owner`, `reversal`.
   Optional: `reason` (required for a compensating reversal, forbidden
   otherwise) and `expand` (required for `contract`, allowed for `migrate`,
   forbidden for `expand`).
5. `reversal` must agree with the script that exists (`down` with `.down.sql`,
   `compensating` with `.compensating.sql`).
6. `expand: V###` must name an earlier migration of the set whose phase is
   `expand`.
7. A reversal script never `DROP`s or `TRUNCATE`s with `CASCADE` (enforced;
   `ON DELETE CASCADE` inside a constraint definition is a referential action
   and is allowed). A reverse must fail rather than destroy objects or rows it
   does not own — V001's `DROP SCHEMA pennilogic RESTRICT` is the model.
8. Rows of an append-only table are never `UPDATE`d or `DELETE`d by any
   script, forward, migrate or reversal: the ledger is corrected by reversing
   transactions (CONSTITUTION.md, "ledger history append-only"). api#2 names
   its append-only tables when it creates them, and a pull request whose
   scripts touch their rows fails review. This rule is reviewed, not machine
   checked, until those tables exist.

Example (the shipped baseline):

```sql
-- phase: expand
-- owner: api#55
-- reversal: down
CREATE SCHEMA pennilogic;
```

A compensating migration states why the exact reverse is impossible:

```sql
-- phase: contract
-- owner: api#NN
-- reversal: compensating
-- reason: the dropped legacy values cannot be restored
-- expand: V010
ALTER TABLE pennilogic.widgets DROP COLUMN legacy;
```

with `V012__drop_legacy.compensating.sql` restoring the column shape.

## Checksums

The checksum of a migration is the SHA-256 of its up script with `\r\n`
normalised to `\n`, so it is identical on every platform. The header block is
part of the script, so editing the owner or phase after apply is also an edit.
The runner stores the checksum in the registry when it applies the migration
and compares it on every later run: a mismatch stops the run before any lock is
taken, as `migration_registry_problem` with `kind: checksum_drift`, naming the
file and both checksums. A renamed or deleted applied migration is refused the
same way (`applied_migration_renamed`, `applied_migration_missing`).

A migration is immutable once it has been applied anywhere. Fix a mistake with
a new migration. A migration that only ever *failed* (a `failed` registry row,
never `applied`) may be edited and retried, because nothing was written.

The reversal script is pinned the same way: its checksum is recorded with the
applied row, and a reversal script that differs from the recorded one stops
every command with `reversal_checksum_drift`, naming the file, so the reverse
that runs is exactly the reverse that was recorded as evidence. A reversal
that turns out to be wrong after apply is corrected by a new forward migration,
not by editing the pinned script.

Scripts name their objects with the schema (`pennilogic.accounts`, not
`accounts`): the runner does not set `search_path`, and the registry itself
is always addressed as `migration.migration_registry` / `migration.migration_lock`
so that one database has exactly one registry whatever the session or role
`search_path` is.

## Phases

`phase` is one of `expand`, `migrate`, `contract`:

- `expand` adds shape additively (new schema, table, column, index) and may be
  deployed before the code that uses it.
- `migrate` moves data into the new shape and names the `expand` it serves.
- `contract` removes the old shape and must name the `expand` it completes.

`migrate` (the command) applies pending `expand` and `migrate` migrations and
**holds** at the first `contract` migration, reporting `migration_contract_held`
and applying nothing after it. The hold is reported on every run that holds,
including a run in which the hold is the only thing that happens (such a run
takes no lock); a dry run shows it in the plan as action `hold`. Pass
`--include-contract` (`-PincludeContract` for the Gradle task) only once every
consumer has migrated. Ordering guarantees the expand a contract names is
applied before it; the validator guarantees the reference exists.

## Transactions and failure

Each migration runs in its own transaction on the migration role's connection,
together with the registry row that records it: the change and its record
commit or roll back as one. Scripts therefore must not contain `BEGIN`,
`COMMIT` or `ROLLBACK` as statements (PL/pgSQL bodies are fine); the runner
checks the session's transaction state after the script and refuses a script
that ended its own transaction. A failure rolls the migration back, records a
`failed` row (SQLSTATE, schema, table, constraint, column, position: never the
server's free text, which can carry row values), releases the lock and exits 1.
The database is then at the last version whose latest row is `applied`; the
next `status` prints it. Statements that cannot run inside a transaction
(`CREATE INDEX CONCURRENTLY`, `VACUUM`, `ALTER SYSTEM`) are not supported by
this runner and fail cleanly in the same way.

Reversal scripts never `DROP` or `TRUNCATE` with `CASCADE` (rule 7, enforced
by the convention check). If a later migration left objects behind, the
reverse must fail (as V001's `DROP SCHEMA pennilogic RESTRICT` does) rather
than destroy data that the reverse does not own.

The runner holds the single lock from before the first statement until after
the last registry row. If its claim is gone at release time — someone
force-released it while the run was in progress — the run ends with
`migration_lock_release_mismatch` and exit 1 even though every migration it
ran was recorded, because it can no longer attest that no second writer
overlapped; RECOVERY.md §2 treats it as an incident.

## Running

Connection settings come only from the environment and are never printed:
`MIGRATION_JDBC_URL` (`jdbc:postgresql://...`), `MIGRATION_DB_USER`,
`MIGRATION_DB_PASSWORD`. The runner pins its JVM zone to UTC so the session
does not depend on the operator machine's zone database.

| Command | Gradle task | Effect |
| --- | --- | --- |
| `validate` | `migrationConventionCheck` | Checks the directory, prints the machine-readable set, needs no database. |
| `status [--status-file P]` | `migrateStatus` | Prints current version, last applied migration, lock holder and per-migration state; optionally writes it atomically (temp file plus rename) to a file that can be read without a database session. |
| `migrate --holder ID [--target N] [--include-contract] [--dry-run]` | `migrate` (`-Ptarget`, `-PincludeContract`), `migrateDryRun` | Applies pending migrations in order. |
| `migrate-down --holder ID [--target N] [--dry-run]` | `migrateDown` (`-Ptarget`) | Reverses the latest applied migration, or down to `N`. |
| `release-lock --holder ID --host NAME --pid N` | (operator, see RECOVERY.md) | Clears a lock whose holder process has died; all three values must match the claim exactly as `migration_lock_refused` reports them. |

`--dry-run` prints the `migration_plan` event and writes nothing: no bootstrap,
no lock, no row. The Gradle tasks pass `--holder` from `-PmigrationHolder`
(default `<user>@gradle`); a deployment names its ticket and job, for example
`api#2@release-2026-10`. Exit codes: 0 success; 1 refused, failed, or ended
without its own lock claim (one event names why); 2 usage error. Every line of output is one JSON event; the event
names are listed in [REGISTRY.md](REGISTRY.md).

Every migration is expected to finish inside the published threshold of
60 seconds (`--slow-threshold-seconds`). A migration still running at the
threshold raises `migration_slow` while it runs, so a long migration is
noticed by the runner rather than by a timeout elsewhere.

## Roles

The runner connects as the migration role, which owns the application schema
(`pennilogic`) and the registry schema (`migration`, created by the runner's
bootstrap). The runtime application role is a different role and
is never granted `CREATE` on a schema or ownership of a table; the ledger ticket
(api#2) and T-SEC-01 define that role's grants. `applied_by` in the registry
records the holder identity given to the runner, and Postgres records the
connecting role, so a change applied by the wrong role is visible.

## Tests every schema ticket must keep green

`./gradlew check` runs, against a disposable digest-pinned `postgres:17`
container:

- the round trip of every shipped migration on an empty database and again on
  a populated one (`shipped migrations round trip ...`),
- checksum drift of the up and of the reversal script, rename and removal
  detection, and the documented restore-and-rerun remediation,
- concurrent lock refusal naming the holder, a two-thread race, and release
  of a dead claim by holder, host and pid,
- part-way failure with rollback, redaction and the documented recovery,
- dry-run with no bootstrap, no lock and no row,
- the registry found under a foreign `search_path` with no second registry,
- the slow-migration alert, the contract hold (including an all-held run),
  the stolen-lock incident, the status file and registry-corruption recovery.

A schema ticket adds its own populated-data step to the round trip (insert
representative rows after applying its migration, then reverse) and any
invariant test the ticket names. Without Docker the Postgres tests do not run
and Gradle says so (`integrationTest SKIPPED: Docker is not available ...`);
in CI (where the `CI` environment variable is set) a missing Docker fails the
build instead, and the task itself fails unless its JUnit results show at
least one test executed and none skipped, so the Postgres tests cannot be
silently absent from a pull request.
