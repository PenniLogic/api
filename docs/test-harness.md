# Shared property fixtures and advisory flake classification

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

The cleanup test launcher uses explicit `ComSpec` on Windows and the
`scripts\quality.py` convention of `sh` for the non-executable POSIX wrapper.
Windows uses an outer-quoted `/d /v:off /s /c` command and quoted child-local
substitutions, preserving spaces and literal percent/exclamation expressions
without enabling AutoRun or delayed expansion. Its fixed-argument interface
explicitly refuses embedded double quotes, NUL and line breaks rather than
interpreting them as command syntax. The native regression exercises the shared
launcher with spaced and special-character wrapper directories, exact argument
output (including surrounding spaces and an empty value), exits 0 and 7, and
refused command characters. POSIX fixtures remain `0644`; tracked wrapper
bytes/modes and the parent environment are unchanged.

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

## Advisory flake classification

Every existing `checkProperty` call now executes through `AdvisoryCheck`, including
the shared Money codec and PostgreSQL ledger consumers. These existing consumers
are conservatively fixed to the accepted `money_path` change class: **zero
re-executions**, including their deliberately failing controls. Assertions,
shrinking, fixture seeds, coverage/mutation floors and process budgets are
unchanged.

`processTestResources` depends on the existing `prepareMoneyProvider` task. That
task uses `money_provider.read_strategy` to verify the accepted Docs bytes before
Gradle copies the strategy into the test classpath. `FlakePolicy` reads those
bytes, not a second table of policy constants. Missing, malformed, duplicate-key,
oversized or unsupported policy data fails; there is no permissive fallback.
The existing source acquisition and offline verification commands are unchanged.

For an explicitly ordinary synthetic property, `propertyCheck` accepts the
strategy's declared change-class IDs and a **fresh arbitrary factory**. Each
attempt uses the same seed, case count, fixture corpus and callback; the factory
restarts the arbitrary's seed prefix rather than continuing its mutable cursor.
The factory and callback belong to one runner instance. A mixed set containing
`money_path` still receives zero re-executions. Callers must classify their actual
change correctly; this is not a repository-wide changed-file classifier.

The accepted policy permits one ordinary re-execution, only after an assertion
failure. Configuring more than the permitted count fails before the fixture runs.
The result is never an automatic green retry:

| Complete attempt outcomes | Classification | Result of `run()` |
| --- | --- | --- |
| Pass | `stable_success` | Returns normally |
| Failure with no retry permitted, or failure then failure | `stable_failure` | Throws the first assertion; retains the second assertion when distinct |
| Failure then pass | `non_deterministic` | Records one flake event and still throws the first assertion |
| Interrupted execution, or a missing permitted retry result | `incomplete` | Blocking; not proof of a flake |
| Different source/configuration bindings | `not_comparable` or a pre-execution refusal | Blocking; not proof of a flake |

Non-assertion exceptions do not authorize a classification retry. Kotest also
wraps non-assertion callback exceptions as `AssertionFailedError`; the shared
property adapter tracks the callback outcome before that wrapping and refuses
to call it a completed assertion failure. Such interrupted properties fail with
`property-execution-incomplete`. JUnit aborts are converted to blocking failures,
not propagated as test skips. Assertions in fixture construction, sampling,
shrinking, result reporting or execution-count validation also do not establish
a completed callback failure. The engine's propagated assertion must retain an
actual callback assertion by identity in its bounded cause chain; an earlier
callback failure does not turn an unrelated shrinking interruption into a flake.
If the classification attempt is interrupted, its exception propagates and the
event retains both the original failed outcome and the incomplete outcome. A
clock moving backwards is an explicit history error, not a new observation.
This is a classifier for this shared assertion/property harness, not a universal
JUnit exception interceptor.

A runner owns an initially empty, in-memory history of at most 256 observations,
with at most the policy-permitted attempts per observation. It is not a shared
cache, a file import, or retained native CI history. Reusing that same runner
allows repeated observations; constructing another runner explicitly starts a
new history. Duplicate/out-of-order observation numbers, backwards timestamps,
missing results, retries after a pass/interruption, invalid identifiers and
capacity exhaustion fail. History snapshots cannot be mutated by the caller.
Overlapping use of a runner is refused, without corrupting its active execution.

The observation timestamp is completion time in UTC. A flake counts when its
timestamp is in the inclusive interval from the current completion time minus
the policy's 14 days (24-hour days) through the current completion time. Exactly
two failure-then-pass observations in that window set `quarantine_advisory: true`.
An observation one nanosecond before the lower boundary does not count. Stable
success does not erase still-current advice; expiry from the observation window
is not a declaration that a quarantined test has been fixed.

Each `flaky_test_observation` JSON line includes the safe test/seed identifiers,
category/task, observation number, policy hash, source/configuration digests,
every attempt's outcome and monotonic elapsed milliseconds, retry count, window
count, classification and advisory flag. The source digest covers the task's
main/test/accepted Money source inputs and Gradle configuration/lock/verification
files. The configuration digest binds the property, seed, case count, corpus,
policy and declared retry/change-class settings. Bindings may not change within
or between observations of the same runner. They are comparability metadata,
not an independent attestation or proof that external services are unchanged.
The events contain no input records, exception text or financial values.
Existing per-category suite durations remain the wall-clock attribution;
classification attempts also have individually attributable durations.

**No test is quarantined or skipped.** Advice is only a candidate flag, not an
entry in a quarantine inventory. Every event explicitly states
`quarantined: false`, `skipped: false`, and
`quarantine_inventory_gates: "not_implemented"`. The accepted 2 percent actual
quarantine-rate ceiling and 14-day actual quarantine maximum age are read and
reported, but **repository-wide ceiling/expiry enforcement is not implemented**.
There is no verified repository-wide denominator, quarantine inventory,
fix-only exception or reviewer-approved deletion flow in this increment. Those
policy obligations are not waived by calling this advisory. Native cross-run
retention/aggregation and any later exclusion mechanism also remain unimplemented.

### Advisory controls

After the existing source preparation, run:

```powershell
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' test --tests 'com.pennilogic.testing.Flake*'
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' test --tests 'com.pennilogic.testing.*'
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' -PflakeAdviceNegativeControl=true test --tests '*FlakeHarnessTest.the real varying fixture*'
.\gradlew.bat --no-daemon --console=plain '-Pkotlin.compiler.execution.strategy=in-process' integrationTest --tests 'com.pennilogic.ledger.LedgerPropertyPostgresTest'
```

The third command **must fail**, even though the permitted classification
re-execution passes. It exposes the same deliberately varying, synthetic-only
fixture used by the ordinary meta-test, through the actual Kotest engine.
Its input remains `Unit` with the same case ID and seed; only the deliberately
stateful assertion varies. The meta-test observes three failure/pass pairs,
flags the second pair under the accepted policy, confirms the third still runs,
and checks a separate stable recovery control. It catches its deliberate
assertions only to test the harness; the negative-control command propagates
the next original assertion to JUnit/Gradle. It does not mutate ledger logic.

The other controls cover exact threshold/window boundaries, zero-money and mixed
change-class retries, persistent real failure, incomplete/changed executions,
policy/history/input refusal, bounded ASCII output and concurrent-instance
refusal/recovery. They are not whole-suite timing or native CI qualification.

## Remaining original requirements

Live API/OpenAPI and currently generated-client contract stages, Testcontainers
adoption (the existing harness uses the pinned Docker CLI), repository-wide
quarantine inventory/rate/expiry gates and native history retention, broader
realistic product generators and full original
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
