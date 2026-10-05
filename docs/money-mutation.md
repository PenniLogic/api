# Local Money mutation gate

This is the executable Money-only source slice of [#22](https://github.com/PenniLogic/api/issues/22),
stacked on unaccepted local preparation `8e79ddd25f4cebff550999ca37bfbe02cc0d18a9`
(tree `c843380583ef3a706967662ffc6d416ea1c8c145`). It is not a released provider, protected-source
adoption, native CI result, non-author approval, or completion of #1, #3 or #22.

**The gate exists; qualification remains RED.** The actual result is 376 killed out of 439
mutants (85.649203%), below the numeric `api.money` mutation floor read from the accepted
Docs strategy. There are 58 survivors and five uncovered mutants, with no local waivers.

## Reproduce

First materialize the ten exact accepted Contracts inputs and the byte-bound Docs strategy
using the [existing provider command](money-guard.md#commands-and-ownership). Do not substitute
a branch, release claim, alternative codec, or an unaccepted Infra materializer.

```text
python scripts\quality.py build
python scripts\quality.py money-mutation
python scripts\quality.py money-mutation-report
python -m unittest discover -s scripts\tests
```

The normal build reaches `check -> moneyMutation -> moneyCoverageCheck -> moneyTest`.
`moneyTest` is deliberately re-executed rather than restored from the build cache. The focused
mutation command has the same dependencies and selection. The report command verifies the
latest attempt; it neither launches PIT nor falls back to an older passing attempt.
The current first three commands fail mutation qualification, not compilation or a missing tool.

No generated workflow, agent profile or policy command changes are needed for this gate:
the existing `python scripts/quality.py build` remains the normal command. Fresh-checkout
materialization and eventual native integration are still coordinator/Infra responsibilities.

## Engine and complete selection

Gradle locks and SHA-256 verification metadata pin Apache-2.0 PIT command-line/core/entry
1.30.0, the Apache-2.0 PIT JUnit Platform plugin 1.2.3, and the existing JUnit Platform launcher
6.1.3. The build's existing Java 21 toolchain and Kotlin 2.4.10 compile and execute the real
provider tests. No paid Kotlin/Arcmutate plugin, mutation service or custom pseudo-mutant
engine is used.

`scripts/PitCatalogue.java` enumerates the installed engine's `ALL` factories and interceptor
defaults. There are 125 distinct factory IDs, including every switch factory and operators
that produce zero mutants. Twenty operator IDs produce the observed 439 mutants.
`flogcall` and `fkotlin` are disabled, consistently from the first baseline, so logging calls
and Kotlin line-zero code are not silently dropped by those filters. There are no configured
class, method, test or mutator exclusions. `run.json` lists all 27 interceptor defaults and
their effective state; native engine filters are not disguised as complete source mutation.

Both immutable provider source files and every compiled provider class are inventoried:

| Provider class | Generated mutants |
| --- | ---: |
| CurrencyEntry | 54 |
| CurrencyRegistry | 10 |
| Money | 79 |
| Money$Companion | 206 |
| MoneyReason | 1 |
| MoneySerializer | 32 |
| MoneyWire | 41 |
| MoneyWireException | 16 |

The target is `com.pennilogic.contracts.money.*`, not API own-main or an easier replacement.
The immutable provider's compiled directory replaces its duplicate JAR on the test classpath.
Only real `com.pennilogic.money.*` Jupiter tests can supply kill evidence.

**Unwaived tool limits:** PIT's native bytecode eligibility/default compiler and equivalence
filters still apply. Static initializers and non-lambda synthetic methods are not mutated.
In particular, passing registry-vector assertions is not a mutation kill of the registry's
static initializer. Generated line numbers may be zero or refer to Kotlin inline mappings;
the report retains native class, method, descriptor and instruction indexes instead of
pretending every mutant has a literal source line. These limits and the catalogue require
independent review; the author does not approve or discount them.

## Actual progression and unresolved result

All rows below used the same engine, full selection and unchanged authoritative provider.
Native attempt directories are retained under `build/reports/money-mutation/`.

| Control tests | Killed | Survived | No coverage | Score | Attempt |
| ---: | ---: | ---: | ---: | ---: | --- |
| 16 | 284 | 68 | 87 | 64.692483% | `20261005T101015Z-215301db2b29` |
| 22 | 370 | 64 | 5 | 84.282460% | `20261005T101521Z-23cce6f968d0` |
| 25 | 372 | 62 | 5 | 84.738041% | `20261005T102350Z-23d1de244227` |
| 26 | 376 | 58 | 5 | 85.649203% | `20261005T104612Z-5f6a16387fcc` |
| 27 | 376 | 58 | 5 | 85.649203% | `20261005T110632Z-b73b3d5a6bde` |

All five shown runs have zero timeouts, non-viable mutants, memory/run errors, started-but-incomplete
and not-started outcomes. PIT exits 1 for the below-floor score; this is a real gate failure,
not a passing build. Earlier dependency, launch and report-parser failures and repeat runs are preserved too.
No failed attempt is relabelled as a successful baseline.

The additional tests exercise actual data-class value/copy semantics, collection behavior
(including collision-heavy valid keys), Java null boundaries, minimal diagnostics, the
existing JVM-visible internal parser/destruction seams, extra-member rejection and ADR-015
missing-before-extra rejection precedence. They never alter provider bytes or private state,
admit `Long.MIN_VALUE`, widen ledger currencies, or implement another codec.

The 58 survivors currently include 20 hash variants, 21 redundant/defensive void-call
removals, eight inline/default-argument constant changes, and nine remaining redundant
guard/unused-result candidates. These are investigation categories, **not proven equivalences**.
The five uncovered mutants are in defensive destruction failures unreachable through the
valid immutable construction paths exercised here. Arbitrary exact-hash assertions,
compiler-message snapshots, private-state corruption and fake kills are not remedies.
Every unresolved mutant remains in the native XML and normalized outcome catalogue, with
its original status. Twenty additional genuine kills would be needed for this denominator
to reach the currently published floor; no reduction or waiver is applied.

Money coverage is 75/75 lines and 82/86 branches, across all eight classes, with 27 tests,
zero failures and zero skips. Actual successful loop completion emits 10,030 round-trip cases,
10,000 associativity cases and 384 collision keys. The consumer matches numeric suite counters
to named Money test cases and requires each property category. Independent-oracle status is
`not_implemented` and its disagreement count is `null`, never an invented zero.

## Fail-closed reports, diagnostics and budgets

Every attempt updates `latest.json` before preflight, including missing-input failures.
Unique attempt directories retain `run.json`, XML, argument files and native logs. The record
binds SHA-256 and byte sizes for provider/strategy inputs, source and compiled/runtime
inventories, Python/Java executables, engine artifacts, build/lock/verification files and
test evidence. It also records exact expanded Java arguments, actual process-launch arguments,
exit codes, elapsed times, feature/factory catalogues and every mutant outcome.

The native console denominator must agree with complete XML. Missing/duplicate identities,
unknown targets/operators/statuses, truncated XML, false kill evidence, changed inputs,
modified outputs, failed/latest-stale attempts and unresolved engine errors are refusals.
Only `KILLED` counts in the numerator. Timeouts and uncovered mutants stay in the denominator
without kill credit. Exact fractions enforce the owner-published number, not a rounded score.
The XML `partial` attribute describes line coverage, not mutation-run completion.

`money_budget` reads `money_path_harness_minutes` and `pull_request_gate_minutes` from the same
exact strategy used for package floors and takes their minimum with the preserved 600-second
native process ceiling. Missing/non-integer/non-positive budgets fail. Normal build and focused
mutation commands enforce their elapsed deadline; PIT catalogue and engine share the remaining
mutation budget. The latest normal build took 70.338562 seconds wall time and the mutation
harness 43.608505 seconds, failing solely on the score.

Windows uses a private kill-on-close Job Object. A stdin-gated bootstrap cannot launch the
real command before attachment, closing the process-start race. POSIX uses an owned process
group. Timeout output is retained, children cannot outlive a successful parent, and no process
name or shared daemon is killed. Kotlin compilation stays inside the owned Gradle process
tree rather than creating a shared Kotlin daemon. The Windows startup/timeout/isolation
probes ran; POSIX/native Linux execution is not claimed.
Docker is not an OS child process: the existing ID-scoped Gradle finalizer removed the
disposable PostgreSQL container in the recorded build. A forcibly terminated host JVM can
still leave its recorded container for the existing stale-container cleanup on the next run;
OS process containment is not a Docker-daemon cleanup guarantee.

Control diagnostics use synthetic cases and stable operation/category IDs, never production
records or monetary expected/actual values. The Kotlin value-comparison probe proves a failed
comparison identifies its case without calling either value's renderer. Native mutation identities and operator descriptions
remain preserved for review. The 74 Python tests include exact-floor success, just-below-floor
failure without rounding, missing-number failures, restoration, output/input tampering,
error/no-coverage/timeout accounting, and owned-process cleanup. Their explicitly synthetic
consumer fixtures are not counted as real PIT mutants or independent-model evidence.

## Original #22 acceptance and definition-of-done mapping

| Original acceptance criterion | Local evidence and remaining scope |
| --- | --- |
| Deliberate rounding error caught by properties | Not demonstrated. Real round-trip, integer arithmetic and associativity tests exist; debt/budget/allocation rounding modules and their deliberate-fault demonstration do not. |
| Independent-model disagreement fails and names the case | Not implemented. No independent oracle is substituted by provider round trips or made-up disagreement counts. |
| Below published package mutation floor fails the build | Implemented for the currently materialized `api.money` package: the real normal build fails at `moneyMutation`; `MutationPolicyTest` and the standalone-consumer probe test numeric floors and restoration. The package itself remains below floor. |
| Duplicate idempotency key has one ledger effect | Not implemented. API #3 remains dependent on API #2's real ledger; migration tests are not financial idempotency evidence. |
| Serializable concurrent writes stay non-negative and balanced | Not implemented. No live ledger endpoint or financial concurrency suite exists in this slice. |
| Published numeric pipeline budget is enforced | Implemented locally for normal build and Money mutation processes; `ProcessBudgetTest` proves failure, owned-child containment and restoration. Native required-CI execution and container behavior on forced host termination remain unqualified. |
| Every money package meets published line/branch floors; missing numbers fail | The sole current materialized Money package passes actual full-class coverage and missing-number probes. This is not acceptance of future debt, budgeting, allocation or ledger packages. |

| Original definition-of-done item | Status |
| --- | --- |
| Current-head separate Core reviewer attestation before merge | Pending Root-arranged non-author review. The two author reviews are not independent approval. |
| Every acceptance criterion demonstrated by named tests/evidence | Incomplete as enumerated above; real mutation qualification is still red. |
| Ticket tests run in native required CI/branch protection | Not executed or changed by this local source owner. Existing normal-command wiring is not native acceptance. |
| Observability, rollout and rollback recorded on the issue | Local reports and this guide provide source evidence; no issue write or rollout occurred. Current instructions require a blocking gate with no skipped gate or lowered floor, so the historical non-blocking/threshold-lowering proposals were not enacted. |
| Dependencies mirrored as native blockedBy edges | Read-only frozen issue snapshot records #3 and PenniLogic/docs#22, PenniLogic/docs#35, PenniLogic/infra#3. No remote edge changes or current-state re-verification are claimed. |
| Independent second-reviewer approval or external review | Pending; the author does not satisfy it. Core, QA and the affected Money/reliability/diagnostic risks need Root's appropriate independent review. |

Root owns publication, canonical materialization handoff, integration and any future rollout.
No issue is closed or marked Ready, no failing check is bypassed, and no protected/released
source, real-ledger behavior or full API #22 acceptance is inferred from this local commit.
