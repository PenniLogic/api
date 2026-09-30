# Migration recovery (T-MIG-01, api#55)

Every path below is exercised by `MigrationRunnerPostgresTest`; the test named
in brackets is the evidence that the path works as written. All commands take
the connection from `MIGRATION_JDBC_URL`, `MIGRATION_DB_USER` and
`MIGRATION_DB_PASSWORD`, and `--migrations` names the directory of the reviewed
commit being deployed.

## 1. A migration failed part-way

Symptom: exit 1 with `migration_failed` naming `version`, `id`, `direction`,
`knownVersion` and the redacted failure (`sqlState`, `schema`, `table`,
`constraint`, `column`, `position`). Because each migration runs in one
transaction with its registry row, everything the failed script did was rolled
back and the database is at `knownVersion`. The lock was released.
[`a failing migration rolls back, records the failure without data and the
recovery path works`]

1. Confirm the known version and read the failure:
   `status` → `currentVersion` equals `knownVersion`, `lock` is `null`, the
   migration shows `"state":"failed"` with `lastAttempt.failure`.
2. Reproduce the statement in `psql` as the migration role if the redacted
   fields are not enough; the runner never prints the server's message because
   it can contain row values.
3. Fix the cause. If the script itself is wrong, edit it: a migration that has
   only ever failed was never applied, so its checksum is not yet pinned. If the
   cause was environmental (a pre-existing object, a privilege), fix the
   environment and leave the script alone.
4. Run `migrate` again. The failed row stays in the history; a new `applied`
   row follows it.

A reversal can fail the same way, for example `DROP SCHEMA ... RESTRICT` when a
later object still lives in the schema. The database stays at the version that
was applied; remove or reverse the dependent object through its own migration
and run `migrate-down` again. [`a reversal that would destroy dependent objects
fails and keeps the version`]

## 2. The lock is held by a process that no longer exists

Symptom: exit 1 with `migration_lock_refused` naming `holder`, `host`, `pid`,
`claimedAt`, `command` and `targetVersion`. This is also what a genuinely
concurrent run looks like, so check before releasing. [`a second runner is
refused while the lock is held and the message names the holder`, `syntax
errors report the position and a dead holder can be released by name`]

1. On `host`, confirm process `pid` is not running (or the job named by
   `holder` has finished). If it is running, wait: never release a live claim.
2. `status` shows whether the dead run recorded a `failed` row. If the process
   was killed mid-statement, Postgres rolled its transaction back when the
   connection closed; there is no row, and `currentVersion` is the version
   before the interrupted migration.
3. `release-lock --holder <holder>` with the exact holder string from the
   refusal. Releasing requires naming the holder so a stale claim is never
   cleared by accident; a wrong name exits 1 with `migration_lock_not_held`.
4. Run the original command again.

If a lock is force-released while a runner is still working, that runner ends
with `migration_lock_release_mismatch` instead of `migration_lock_released`.
Treat it as an incident: two writers may have overlapped.

## 3. Checksum drift, rename or removal of an applied migration

Symptom: exit 1 with `migration_registry_problem` and `kind` `checksum_drift`
(names `file`, `applied` and `current` checksums), `applied_migration_renamed`
or `applied_migration_missing`. No lock is taken and nothing is written.
[`a checksum that changed after apply fails the run and names the file`,
`renamed or removed applied migrations are refused`]

Applied migrations are immutable. Restore the file to the reviewed content
(`git log -- src/main/resources/db/migrations/<file>`) and express the intended
change as a new migration. Do not edit the registry row.

## 4. Registry integrity

Symptom: `migration_registry_problem` with `kind` `registry_not_contiguous`, or
`runner_error` with `migration_lock singleton row is missing`. The registry has
been modified outside the runner. [`status can be written to a file and reports
registry corruption`]

Stop deployments. Compare `migration_registry` with the actual schema
(`\dn`, `\dt pennilogic.*`) and with the migration files, and restore the
history rows from the last known-good backup or reinsert the missing `applied`
rows with the checksums printed by `validate`. The lock row is recreated by the
next writing command's bootstrap (`INSERT ... ON CONFLICT DO NOTHING`).

## 5. Rolling back a release

`migrate-down` reverses the latest applied migration; `migrate-down --target N`
reverses down to `N`. Compensating reversals run the `.compensating.sql` and the
`migration_reversed` event carries the declared `reason`, so the release record
shows what could not be restored. [`contract migrations are held until
consumers have migrated`, `shipped migrations round trip ...`]

## 6. Nothing to do

`migrate` when every migration is applied, or `migrate-down` at V000, prints a
`migration_plan` with `"steps":[]`, takes no lock and exits 0.

## 7. A script ended its own transaction

Symptom: exit 1 with `runner_error` reading `<id> ended its own transaction`.
The script contained `COMMIT` or `ROLLBACK`, which the convention forbids
because it breaks the guarantee that a change and its registry row commit
together. Whatever the script committed before that statement is in the
database with no registry row; nothing after it was recorded either. The lock
was released. [`a script that ends its own transaction is refused and reported
for inspection`]

Inspect the database, remove or keep the committed objects deliberately, remove
the transaction statement from the script (it has never been recorded as
applied, so it may be edited) and run `migrate` again.

## 8. The registry changed between planning and claiming

Symptom: exit 1 with `migration_registry_problem`, `kind`
`registry_changed_before_lock`, `planned` and `current` versions. Another
runner finished between this runner's plan and its claim; nothing was written
by this run and the lock was released. Run the command again to plan against
the new current version. [`a registry that changed between planning and
claiming is refused under the lock`]
