# Shared property fixtures: bounded API3 source preparation

This increment belongs to the existing [#3](https://github.com/PenniLogic/api/issues/3).
It is not whole-issue acceptance or a readiness change. The original dependency
on [#2](https://github.com/PenniLogic/api/issues/2) remains open. The accepted
[ledger kernel](ledger-schema.md) and [Contracts Money source](money-guard.md)
supply this increment's inputs, not runtime identity, crypto, deployed roles,
product endpoints or released generated clients.

## One test-only fixture contract

`SharedFixtures` is the executable, versioned fixture schema. Both
`SharedFixtureContractTest` and `SharedGenerators` read the single resource
`src\test\resources\testing\shared-fixtures.v1.json`. This is not another Money
wire format, public Account/Transaction DTO, or independent financial oracle.
The input records are synthetic subsets of the accepted V002 schema.

| Element | Exact v1 shape |
| --- | --- |
| Root | `schema: "pennilogic.shared-test-fixtures/1"`, integer `seed`, nonempty arrays `values`, `accounts`, `transactions`; no other fields |
| Every case | Unique safe `id`, typed `input`, `expected_invariant`, integer `tolerance: 0`; no other fields |
| Value input | Signed integer-string `minor_units`, string `currency`; valid values become the actual accepted `Money`; invalid inputs retain an explicit reason |
| Account input | UUID `id`, `type`; valid and deliberately invalid UUID versions/variants are labeled |
| Transaction input | UUID `id`, millisecond-precision `occurred_at`, nullable ISO `value_date`; the posting clock is supplied by the existing fixture at execution |

Tolerance is **exactly zero minor units**, never a percentage, decimal or
rounding allowance. Unknown versions, fields, missing members, duplicate case
IDs and non-integer/nonzero tolerance fail. This typed contract is not a
general-purpose JSON Schema interpreter. It does not add a public parser.
The existing ADR-017/018 fixture resources and accepted upstream Money fixtures
remain unchanged; there is no copy of their large generated vector corpus.

The shared seeds cover zero, both accepted signed endpoints, negative values,
values beyond JavaScript's exact integer range, all actual registry currencies,
the excluded `Long.MIN_VALUE`, unknown currency, UUIDv7 version/variant
boundaries and labeled invalid IDs. Dates include epoch, leap day, year rollover,
the signed epoch-second boundary and year 9999. These are supported examples,
not a new product date-range policy or PostgreSQL's complete date domain.
No account number, institution export, raw message or production record is used.

## Generation, consumption and diagnostics

The pinned dependency is `io.kotest:kotest-property-jvm:6.2.5`, the official
Maven Central release reported on 2026-10-07 (metadata updated 2026-09-10).
Its official POM declares Kotlin 2.2.21; resolution preserves this repository's
locked Kotlin 2.4.10, JUnit 6.1.3 and existing dependency versions. Its required
transitive framework modules are libraries only: no Kotest JUnit runner or
second test-discovery engine is registered. Existing JUnit Jupiter tests call
Kotest `checkAll` directly through `PropertyChecks`.

Each fresh arbitrary consumes its versioned seed prefix, then Kotest-generated
values with the fixed corpus seed. `SharedGenerators.values` and
`SharedGenerators.ledger` share the actual Money construction helper.
The ledger generator also produces schema-derived accounts and transactions;
all five supported date seeds execute before random cases. Nonzero INR is the
balanced-entry domain. Zero and invalid identifiers run in the separately
labeled adversarial property, not assumptions, silent coercions or discards.
JPY/KWD codec round trips and mixed-currency arithmetic refusal are non-ledger
checks; they never widen ledger admission.

`MoneyWirePropertyTest` performs canonical byte and value round trips through
the actual accepted `MoneySerializer`. `LedgerPropertyPostgresTest` imports
the same module and evaluates the accepted constraints in the existing
`LedgerFixture` on real disposable PostgreSQL. Each shrinking trial rolls back
its own transaction. `SET CONSTRAINTS ALL IMMEDIATE` executes the real deferred
checks before rollback, so a failed/shrunk attempt cannot contaminate its next
attempt. The recovery control also performs a genuine balanced commit.
Money arithmetic and expected balances use the accepted Money operations.

Only a safe case envelope is passed to Kotest. Assertion comparisons carry
case/operation IDs and booleans, not expected/actual financial values.
The actual library's initial-failure printing, shrink printing, final error,
OpenTest4J expected/actual values and exception chain are exercised by a
controlled failure. The case ID (including shrink path), seed and operation
identify the minimal reproducer without printing its input. Shrinking uses
Kotest's bounded mode and integer shrinker; ledger shrinks preserve the valid
nonzero entry domain and relational/date context. It is not record-wide
minimization, blanket numeric redaction or a universal taint-proof claim.

The accepted provider's arbitrary-extra-key `MoneyWireException` and malformed
JSON `JsonDecodingException` can carry input payloads. The harness projects
these two exception families before Kotest sees them: closed reason and known
field labels, or a static JSON-decoding failure, with no payload-bearing cause.
Real extra-key/control-character and malformed-JSON canaries exercise both
paths in memory. Controlled captures emit only typed execution counters and
hashes, never captured text. This is a finite **test-only** boundary, not a fix
or acceptance of the separately owned API1 production diagnostics. The capture
suites are isolated and hold the JUnit process-stream resource locks.

Database aggregates are range-checked without a payload-bearing
`NumberFormatException`. A real PostgreSQL diagnostic control observes an
out-of-range sum before deferred constraints run, checks actual property
failure/shrinking diagnostics, and rolls back every trial. It does not commit
an invalid ledger transaction or relax any constraint.

## Execution and negative control

Use the repository's existing source materialization/admission preparation
first. No setup command or hook replacement is needed when its managed hook is
already installed. On Windows:

```powershell
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' test --tests 'com.pennilogic.testing.*'
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' integrationTest --tests 'com.pennilogic.ledger.LedgerPropertyPostgresTest'
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' -PledgerPropertyNegativeControl=true integrationTest --tests '*LedgerPropertyPostgresTest.an isolated broken constraint*'
python scripts\quality.py build
python -m unittest discover -s scripts\tests
```

The third command **must fail**. It propagates the normally captured meta-test
failure after replacing only the zero-sum trigger function in that test's own
fresh disposable database. The exact original function definition is restored
in `finally`, every property trial is rolled back, and the existing container
finalizer removes that specific container and its unshared anonymous volumes.
No migration source, admission
input, registry history, runtime grant or production database is modified.
The ordinary integration command captures and checks that failure, verifies
restoration, reruns rejecting cases and commits a balanced pair successfully.
Never point this harness at a shared database.

Both stale-container cleanup and the ordinary/failure finalizer use the same
checked `docker rm -f -v` path. It accepts only the complete ID in this worktree's
regular marker file and requires Docker to acknowledge that exact removal:
`docker rm -f` can return zero without removing an already-missing container.
The marker is retained on missing acknowledgment, Docker failure or timeout,
and the Gradle task fails rather than silently clearing evidence. Diagnostics
stay in `build\migration-test\docker-cleanup.log`. Named volumes and volumes referenced
by another container are not removed; there is no volume sweep or prune.
`PostgresCleanupPostgresTest` executes the actual finalizer from unchanged build
files in a temporary project, covering anonymous removal, named/shared resource
preservation, absent-marker idempotence and malformed/failed-removal refusals.
Its additional containers are create-only storage fixtures, not a second
database harness.

The cleanup test launcher follows `scripts\quality.py`: explicit `ComSpec` on
Windows and `sh` for the non-executable POSIX wrapper. A native portability
regression checks argument and exit-code preservation using its own wrapper
fixture (mode `0644` on POSIX), without changing the tracked wrapper or its mode.

Existing `test` and `integrationTest` selections execute all the new tests;
`build` still invokes both. The existing `moneyTest`/PIT qualification inventory
is unchanged: the new non-ledger consumer is in `com.pennilogic.testing`, not a
replacement for the 39 existing Money tests, 10,030 round trips, 10,000
associativity cases, 384 keys or 66,851 finite model comparisons.

`shared_property_cases` JSON events in JUnit XML record actual primary
attempts, passes, total callback evaluations, shrink evaluations, seed,
actual invariant-family counts, elapsed milliseconds and outcome. Shrink evaluations are not credited as
successful primary cases. The replay self-test's generated samples are not
reported as property executions. `test_category_suite` console events record
each top-level class suite's wall-clock span and test/failure/skip counts,
separating property, contract, integration and unit suites. Nested and
parameterized containers are included once in their parent class, not counted
again. These are per-suite measurements, not end-to-end workflow timing;
property-backed PostgreSQL classes retain their `integrationTest` task
attribution.

## Remaining original requirements

Live API/OpenAPI and currently generated-client contract stages, Testcontainers
adoption (the existing harness uses the pinned Docker CLI), flaky-test advisory
and quarantine controls, broader realistic product generators and full original
DoD remain pending. The fixture-contract test is not a live API contract test.
The existing Money coverage/mutation gates retain every denominator, floor and
budget, but do not establish whole-API3 acceptance.

The original **full local and CI suite under five minutes** requirement remains
unchanged and must be demonstrated on the actual complete executions. A
targeted run or sum of class durations does not prove it. Native job and
whole-workflow under 600 seconds are additional outer ceilings, not substitutes.
Independent Core/QA/Money-risk review, current-base native CI, publication and
protected integration remain the coordinator's work. This increment closes
none of #1, #2, #3 or #22.
