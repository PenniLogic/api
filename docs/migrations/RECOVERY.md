# Migration recovery (T-MIG-01, api#55)

Every detection below, and every remediation that is a runner command or a SQL
statement, is exercised by `MigrationRunnerPostgresTest`; the test named in
brackets is the evidence. The two steps that are not automated are marked
*manual*: confirming that a lock holder's process is dead (§2) and restoring
registry rows from a backup (§4). All commands take the connection from
`MIGRATION_JDBC_URL`, `MIGRATION_DB_USER` and `MIGRATION_DB_PASSWORD`, and
`--migrations` names the directory of the reviewed commit being deployed.

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

1. *Manual:* on `host`, confirm process `pid` is not running (or the job named
   by `holder` has finished). If it is running, wait: never release a live
   claim.
2. `status` shows whether the dead run recorded a `failed` row. If the process
   was killed mid-statement, Postgres rolled its transaction back when the
   connection closed; there is no row, and `currentVersion` is the version
   before the interrupted migration.
3. `release-lock --holder <holder> --host <host> --pid <pid>` with the three
   values exactly as the refusal reports them. Releasing requires the whole
   claim, not the holder string, so a stale or a look-alike claim is never
   cleared by accident; any mismatch exits 1 with `migration_lock_not_held`
   and leaves the lock as it was.
4. Run the original command again.

If a lock is force-released while a runner is still working, that runner ends
with `migration_lock_release_mismatch` instead of `migration_lock_released` and
exits 1, even though every migration it ran is recorded, because it cannot
attest that no second writer overlapped. Treat it as an incident: compare the
registry rows of both writers with `status`, and do not release the foreign
claim until its owner is understood. [`a lock released underneath the runner
is an incident that exits 1`]

## 3. Checksum drift, rename or removal of an applied migration

Symptom: exit 1 with `migration_registry_problem` and `kind` `checksum_drift`
(names `file`, `applied` and `current` checksums), `reversal_checksum_drift`
(the same for the down or compensating script), `applied_migration_renamed`
or `applied_migration_missing`. No lock is taken and nothing is written.

Applied migrations, including their reversal scripts, are immutable. Restore
the file to the reviewed content (`git log -- src/main/resources/db/migrations/<file>`)
and run the command again: the set is consistent as soon as the checksums match
and the next `migrate` plans `"steps":[]`. Express the intended change as a new
migration. Do not edit the registry row. [`a checksum that changed after apply
fails the run and names the file` — detection of both drifts and the rejected
`CASCADE` reverse, then restore and rerun; `renamed or removed applied
migrations are refused` — detection, then restore and rerun]

## 4. Registry integrity

Symptom: `migration_registry_problem` with `kind` `registry_not_contiguous`, or
`runner_error` with `migration_lock singleton row is missing`. The registry has
been modified outside the runner. [`status can be written to a file and reports
registry corruption`]

Stop deployments. Compare `migration_runner.migration_registry` with the actual schema
(`\dn`, `\dt pennilogic.*`) and with the migration files. Then either restore
the history rows from the last known-good backup (*manual*) or reinsert each
missing `applied` row with the values `validate` prints for that migration:

```sql
INSERT INTO migration_runner.migration_registry
    (version, migration_id, phase, checksum, reversal_kind, reversal_file,
     reversal_checksum, state, direction, applied_by, host, pid, duration_ms)
VALUES
    (1, 'V001__create_pennilogic_schema', 'expand', '<checksum from validate>',
     'down', 'V001__create_pennilogic_schema.down.sql',
     '<reversal.checksum from validate>', 'applied', 'up',
     '<operator>@recovery', '<console host>', 0, 0);
```

(`reversal_reason` is added for a compensating reversal.) `status` reports the
expected `currentVersion` again once the applied prefix is contiguous. The lock
row is recreated by the next writing command's bootstrap
(`INSERT ... ON CONFLICT DO NOTHING`); a `migrate` with nothing pending is a safe
way to trigger it and plans `"steps":[]`. [`status can be written to a file and
reports registry corruption` — detection of the gap and of the missing lock
row, the reinsertion above, and the lock row recreated by the next bootstrap]

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
