# Ledger schema: V002 local preparation

`V002__create_ledger` adds the ledger kernel through the existing
`MigrationSet` / `MigrationRunner`. It is a bounded preparation for
[#2](https://github.com/PenniLogic/api/issues/2), not an accepted product API,
a production database apply, or satisfaction of that issue's open dependencies.
There is no HTTP write path, reversal business helper, FX calculation, stored
balance, deployed runtime role, admitting RLS policy, or crypto implementation
in this unit.

## Binding inputs

The local composition includes the protected accepted Money verification
source from [#94](https://github.com/PenniLogic/api/pull/94), commit
`bdf1b9397b6eefa627e2291767ebf20857a39525`. That bounded source acceptance
does not complete [#1](https://github.com/PenniLogic/api/issues/1),
[#22](https://github.com/PenniLogic/api/issues/22), the independent oracle,
runtime consumers or this ledger issue. The fixed provider inputs and strict
Money gates remain unchanged.

The schema consumes the accepted decisions at Docs commit
`47986ef6bef986a3ba9214ccf652609347d73c66`, not the historical issue numbers
inside the original migrated specification:

- [ADR-017](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/adr/ADR-017.md),
  accepted by [PenniLogic/docs#37](https://github.com/PenniLogic/docs/issues/37).
- [ADR-018](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/adr/ADR-018.md),
  accepted by [PenniLogic/docs#38](https://github.com/PenniLogic/docs/issues/38).
- The [domain model](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/architecture/01-domain-model.md)
  and [DELIVERY](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/governance/DELIVERY.md)
  at that same accepted commit.

The two JSON resources in `src\test\resources\ledger` are **unchanged
extractions of the single normative JSON block in each ADR**, not new schemas
or an independent financial oracle. `LedgerContractPostgresTest` checks their
LF-normalized SHA-256 and consumes their column inventories, closed enums,
admitted currency and money bounds against the actual PostgreSQL catalog.

| Source/input | SHA-256 |
| --- | --- |
| Full ADR-017 source | `5cea7cd87eb53c99b6dadc9bdb725ba19647c9c7f08f4af683186e2638f2f3d5` |
| Full ADR-018 source | `6781fd6f9897508683397e6d659f212041b21fbc36579bb0eb91705410ec2aae` |
| `adr-017-parameters.json` | `f82aa3d76108825f6b7015af496447d9b069bebba1db9a0c4bf351f135a1cef6` |
| `adr-018-inventory.json` | `86056ca2e2c5c5eeb5faad10a65ef99111ed92de5e60b66f7f3ebbcccea3d7d6` |

Money arithmetic in the tests uses the existing pinned Contracts Money
provider and the existing deterministic seed `72131`. Currency-codec acceptance
of JPY is not ledger admission. The migration admits only INR, exponent 2.

## Tables and column protection

All application tables are schema-qualified under `pennilogic`; the
`migration_runner` registry/lock is unchanged. Constraint functions live in
`ledger`, have fixed search paths, qualify application objects, are
`SECURITY INVOKER`, and have PUBLIC execution revoked.

| Table | Classification and shape |
| --- | --- |
| `ledger_currencies` | Global reference: uppercase `CHAR(3)` code, integer exponent, finite millisecond admission instant. No RLS; no PUBLIC grants; append-only. |
| `accounts` | Constrained UUIDv7 key, opaque nonnull owner UUID, currency FK, type, system role, archive flag and creation instant. Six types including reserved CLEARING; role/type checks and unique owner/role/currency system accounts. |
| `transactions` | Constrained owner/currency, lifecycle, kind, effective/posting/creation dates, SMALLINT entry count and correction/snapshot/exchange links. PostgreSQL validates the application-supplied posting clock; it does not assign it. |
| `entries` | Entirely constrained: UUIDv7 key, owner/account/transaction UUIDs, nonnull currency and nonzero BIGINT minor units. Both endpoints of the allowed signed range are supported; `-2^63` is excluded so exact negation is representable. |
| `statement_snapshots` | Immutable constrained statement evidence: owner/account/currency, date, signed BIGINT statement figure (zero allowed; `-2^63` excluded), MANUAL/IMPORT source, opaque evidence UUID and capture/posting instants. Not an authoritative balance. |
| `exchange_groups` | Immutable constrained reservation: owner, two admitted currencies, positive BIGINT rate with scale 10, source/feed/instant/rounding side, effective/posting instants, opaque evidence UUID and unique whole-group reversing link. |

The following descriptive/encrypted fields are nullable `BYTEA`
**reservations that must remain NULL** until the accepted crypto provider is
available:

- Accounts: `name`, `institution`, `mask`; separate blind index `mask_bidx`.
- Transactions: `description`, `merchant_display`, `merchant_raw`, `note`,
  `external_ref`, immutable `reason_note`, and the descriptive
  `merchant_amount_minor`; separate `merchant_bidx` and `external_ref_bidx`.

The paired constrained `merchant_currency` must also remain NULL. It will
need the currency registry, not the admitted-ledger set, when the descriptive
merchant-figure provider lands (ADR-017 section 4.1). Arbitrary bytes are
rejected by named unavailable-provider CHECKs, not accepted as encryption.
Ledger `entries.amount_minor` is **not** this descriptive merchant figure and
is never application ciphertext.

No raw message, raw-derived digest, secret, key, credential, mutable balance
or cache column is introduced. PostgreSQL's built-in constraint errors can
carry row values; the runner keeps its existing redacted failure summary.
Tests retain only case IDs, SQLSTATEs, constraint names and structural counters
in diagnostics. Their fresh database alone disables statement/parameter error
logging and uses terse server errors; this is not production log configuration.

## Database invariants and serialization

`transaction_zero_sum` and `transaction_sealed` are deferred constraint
triggers. With complete writer SELECT visibility into the transaction, every
entry-bearing transaction balances by currency at commit using PostgreSQL's
exact NUMERIC result for `SUM(BIGINT)`, and its entry count equals its sealed
count. Composite foreign keys enforce transaction/account owner and currency
equality and prohibit orphan entries.

These invoker checks are not a universal guarantee under arbitrary RLS
policies. An incompatible partial SELECT policy plus a forged count matching
only the visible entries can admit an imbalanced physical entry set. Complete
same-transaction writer visibility is therefore a mandatory T-SEC-01
integration precondition, not an optional optimization or something proved by
the current default-deny fixture. No runtime writer is admitted here.

Empty STANDARD candidates/suppressed rows have count 0 and no posting instant.
A single guarded update posts a candidate, writes count and `booked_at`, and
inserts its entries in the same database transaction. A posted empty or
incomplete transaction cannot commit. A second count/clock write, even a
same-value assignment, is refused; a later balanced append cannot change
historical composition. Setting constraints IMMEDIATE only checks sooner.

Before inserting an entry or a correction, an invoker trigger writes the
referenced transaction's **unchanged status**. This creates an actual parent
row version and serializes writers without changing a ledger fact. A row lock
alone would not invalidate a stale repeatable-read snapshot. Tests observe the
second backend blocked through `pg_blocking_pids`, then demonstrate constraint
rejection under READ COMMITTED and serialization failure under REPEATABLE READ
and SERIALIZABLE. Whole-command rollback leaves the previously committed
composition intact.

Entries, snapshots and groups reject UPDATE, DELETE and TRUNCATE, including
from the synthetic bootstrap connection. Transactions cannot delete history
or mutate ledger facts; account ownership/type/currency/system role are fixed.
The only transaction-fact update exception is the one-time posting transition.

A reversal has unique same-owner/currency linkage, exactly negates its
target's account/amount **multiset**, and copies effective instant, value date
and count. Its target must be posted and non-reversal. Reversed status and
existence of a reversal are equivalent. A repost requires a reversed target
and a value date when the target has one; active reposts from a new correction
group cannot coexist with unreversed earlier reposts. Historical, already
reversed reposts do not invalidate their replacements. Application orchestration
of correction operations remains [#7](https://github.com/PenniLogic/api/issues/7).

`correction_id` links transactions but does not seal group membership: an
otherwise valid later repost with the same correction ID can commit. A fresh
ID per correction operation, atomic membership and replay enforcement require
the actual [#7](https://github.com/PenniLogic/api/issues/7) operation/replay
provider. Per-transaction entry sealing does not supply that missing boundary.

Snapshots have real account/owner/currency foreign keys; reconciliation
transactions have a unique same-owner/currency snapshot link. Computing the
statement discrepancy, enforcing administrative authority and scheduling
read-model reconciliation are not implemented here.

## Reserved exchange structures

With only INR admitted, two distinct admitted currencies cannot form a group;
FX_CLEARING and FX_FEE system-account inserts are also refused. No exchange or
clearing row is seeded by the migration.

The reservation checks nevertheless require exactly two single-currency legs,
one clearing entry with the appropriate sign and at least one non-system
account entry per leg, and the same owner/effective instant. Reversal is a
whole paired, terminal group with copied rate provenance; reversing a leg
outside that group is refused. Tests widen admission only inside synthetic
databases to exercise these structural checks, then verify restoration.
Those fixtures are not E30 admission or an FX write implementation.

`REFERENCE_FEED` cannot be used yet: `rate_feed` is explicitly gated to NULL
until the authoritative T-CON-05 feed identifiers are available. Quoted-rate
arithmetic, rounding, fee calculation, residual checks and E30 product paths
remain out of scope.

## RLS and absent providers

Every one of the five tenant tables is created with ENABLE and FORCE RLS and
no admitting policy. PUBLIC receives no table privilege. A non-bypass owner
is denied too. No `pennilogic_app` identity, permissive owner policy,
`BYPASSRLS`, definer bypass or trigger-disabling permission is introduced.

Tests use three deliberately distinguished identities:

1. The existing disposable-container bootstrap superuser `migration`, only
   for synthetic setup and invariant probes. It is not a production service
   or a qualified deployment role.
2. A separate random NOSUPERUSER/NOBYPASSRLS login with SELECT/INSERT grants,
   no admitting policy and no UPDATE/DELETE/TRUNCATE/DDL privilege. Its own
   connection receives real permission errors, rather than zero affected rows
   masquerading as UPDATE/DELETE rejection.
3. A synthetic non-bypass table owner, temporarily owning only the test
   database's objects, proving FORCE RLS and reversal fail closed. Ownership
   is restored and the role removed.

The only test policy is a negative-only, owner-specific INSERT policy without
SELECT. Its invisible candidate fails at commit; the policy is removed.
There is no positive runtime-policy substitute. Synthetic roles and all their
grants are removed in `finally`.

These provider boundaries are intentionally incomplete:

| Missing provider | Concrete integration boundary |
| --- | --- |
| Identity | No authoritative `users` table/migration exists here. `owner_id` is a nonnull opaque UUID; ledger tenant equality is enforced, **owner existence is not**. Root must obtain the accepted identity provider and its lifecycle/erasure-compatible FK contract before integration. |
| Ingestion/category/dedupe | No ad hoc `source`, `source_confidence`, `needs_review`, `category_id`, `source_event_id`, `dedupe_key` or `dedupe_key_version` shapes are created. Their authoritative contracts/relations, including fields used by grant predicates, must be supplied by their owning units. |
| Correction provenance | `reason_ref` must remain NULL; DUPLICATE_LINK and ADMINISTRATIVE reasons consequently remain unavailable. The missing providers are `duplicate_links.id` and ADR-020's authorized pending-ledger-request record/audit linkage. |
| Crypto and merchant provenance | T-SEC-01 must supply usable envelope/index-key providers, their validation and grants; merchant currency needs the full accepted registry. A later owned migration can activate the reservations only with those contracts. |
| Runtime RLS | [#37](https://github.com/PenniLogic/api/issues/37) owns real identities, MAC-bound transaction context, write checks, grant mediation and the admitting policies. Every admitted writer must see every row of its transaction and have the same-owner SELECT/UPDATE(status) needed by serialization. Do not solve missing visibility with a definer function. |
| Operations | Storage/backup encryption, no-standing-human-access controls, staging role paths, entitlements, production deployment and read-model reconciliation remain unverified. |

The original issue's role/helper "Scope" bullets conflict with its non-goals
and the accepted decisions. This implementation follows the accepted
ADR-017/018 division above, not a broader-role rollback or a fabricated
production identity. Its invoker functions are ledger constraints, not the
T-SEC context setters or grant predicates.

## Index review

| Named index | Query it serves |
| --- | --- |
| `entries_owner_account (owner_id, account_id, transaction_id, id)` | Derive one owner's account balance with `WHERE owner_id = ? AND account_id = ?`; traverse the owning transactions without an unscoped account lookup. |
| `entries_transaction_currency (transaction_id, currency)` | Deferred per-transaction/currency aggregation, composition counts and exact reversal comparisons. |
| `transactions_owner_occurred (owner_id, occurred_at DESC, id DESC)` | An owner's date-range history with stable descending keyset order. |
| `transactions_owner_booked (owner_id, booked_at, id) WHERE booked_at IS NOT NULL` | Recorded-time range and latest booking-position lookup; the latter orders descending with `LIMIT 1`. |
| `transactions_replaces (replaces, correction_id) WHERE replaces IS NOT NULL` | Find earlier reposts of a corrected fact. |
| `transactions_exchange_group (exchange_group_id) WHERE exchange_group_id IS NOT NULL` | Deferred leg/group shape and paired reversal checks. |
| `accounts_system_role_currency` | Unique owner/system-role/currency identity; ordinary accounts are outside its partial predicate. |

The seeded test commits 1,000 transactions / 4,000 entries across 16 synthetic
owners and 80 accounts, independently accumulates expectations with the actual
Money type, and recomputes every account from PostgreSQL. After ANALYZE it
checks unforced PostgreSQL EXPLAIN index selections for the account-balance,
user/date-range and latest-booking queries. This is not a load/SLO gate or
evidence for a scheduled reconciliation service.

## Migration and verification

Use the existing [convention](migrations/CONVENTION.md),
[registry](migrations/REGISTRY.md) and [recovery](migrations/RECOVERY.md).
There is no second apply route or manually inserted registry history.
V001 is unchanged; the discovered set is explicitly V001 then V002.

`migrate` applies each migration with its checksum and registry row under the
existing lock. Default `migrate-down` reverses V002 to V001; target 0 explicitly
continues through V001. Empty ledger objects can reverse and reapply while
unrelated seeded data is preserved.

**Committed ledger data is never erased to make a reverse pass.** V002's down
script first sets local `row_security = off` (which errors on protected reads,
not an RLS bypass), takes ACCESS EXCLUSIVE locks on every owned table, then
checks for any tenant row or additional currency admission. Populated history
causes `P0001` / `ledger_history_preserved`; a protected non-bypass owner gets
`42501` rather than a falsely empty result. The runner records the failure,
retains version 2 and releases the lock. Empty teardown uses RESTRICT only.
A concurrent account insertion is allowed to finish before the guard examines
the locked table; if it commits, reversal refuses and preserves it.

After a refused down, status reports `currentVersion: 2`, operator
`state: "failed"`, the recorded `lastAttempt.failure` and a released lock.
`lastApplied` still names V002: effective version derives from the latest
non-failed transition, independently of the latest-attempt label. A forward
no-op leaves the failure visible; it neither retries down nor erases history.
The library, native CLI and status file share this status contract.

The original ledger coverage comprises six Kotlin test/helper files: five
PostgreSQL suites plus `LedgerFixture`, not seven source files. Focused status
regressions additionally exercise failed up/down, reversed state and successful
retry using a separate controlled test migration, never by deleting committed
V002 history.

The existing owned integration fixture starts digest-pinned PostgreSQL 17.11
on a fresh loopback-only container and finalizes that container even on test
failure. Do not point these tests at a shared database. With the repository's
documented pinned toolchain and Money source inputs available:

```powershell
.\gradlew.bat --no-daemon --no-build-cache --console=plain '-Pkotlin.compiler.execution.strategy=in-process' integrationTest --tests 'com.pennilogic.ledger.*'
python scripts\check_repository.py
python scripts\quality.py build
python -m unittest discover -s scripts\tests
```

The tests cover raw unbalanced/mixed/foreign/orphan writes, empty and bulk
boundaries, sealed posting, rollback/disconnect, bounds and clock checks,
correction chains, real restricted connections, invisible-row refusal,
three isolation levels, table/runner locking, exact source/catalog contracts,
checksum drift, failed-forward restoration and populated reverse preservation.

Root still owns publication, affected separate non-author Core/Money/Security
plus QA review, real native CI and integration. Prior ledger/F01 reviews remain
dated evidence for their original commits, not approval of the composed source.
[#1](https://github.com/PenniLogic/api/issues/1) and
[PenniLogic/infra#28](https://github.com/PenniLogic/infra/issues/28) remain
open prerequisites. Passing this finite unit does not close
[#2](https://github.com/PenniLogic/api/issues/2),
[#3](https://github.com/PenniLogic/api/issues/3),
[#22](https://github.com/PenniLogic/api/issues/22),
[PenniLogic/infra#22](https://github.com/PenniLogic/infra/issues/22)
or any runtime security/deployment acceptance.
