# Local Money mutation gate

This is the executable Money-only source slice of [#22](https://github.com/PenniLogic/api/issues/22),
stacked on unaccepted local preparation `8e79ddd25f4cebff550999ca37bfbe02cc0d18a9`
(tree `c843380583ef3a706967662ffc6d416ea1c8c145`). The API slice is not protected-source
acceptance, a native CI result, non-author approval, or completion of #1, #3 or #22.
Its current source dependency is the coordinator-confirmed protected accepted Contracts
commit `aa8d90cb98cec9b6dd08c91b3a4d869e47362662`; that provider acceptance is not API acceptance.

**This is a prospective measurement-scope proposal, not source or methodology acceptance.**
It restores the unmodified PIT 1.30.0 built-in FKOTLIN filter while keeping FLOGCALL disabled.
The frozen pre-proposal accepted-provider result at `0d386dfed7b98cadc54e1d05f7e90ea32627225f`
remains RED: 378 killed out of 441 mutants (85.714286%), 58 survivors and five uncovered mutants.
That result and all 63 obligations remain history; no old failure is relabelled or waived.
One ordinary current-AA8 candidate run passes the unchanged numerical gate at
**309/342 (90.350877%)**, with 28 survivors and five uncovered mutants. This result comes
from a different measured population, not stronger tests or additional kills.

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
The ordinary candidate gate remains blocking for below-floor, incomplete or error outcomes.
A historical scratch result cannot qualify it; it requires its own current-source native run.

No generated workflow, agent profile or policy command changes are needed for this gate:
the existing `python scripts/quality.py build` remains the normal command. Fresh-checkout
materialization and eventual native integration are still coordinator/Infra responsibilities.
Unchanged command/profile shapes do not make a stale canonical materialization catalogue ready:
its Contracts commit and the Money.kt/golden entries must adopt the exact AA8 pins documented
in the [provider guide](money-guard.md#commands-and-ownership).

## Engine and complete selection

Gradle locks and SHA-256 verification metadata pin Apache-2.0 PIT command-line/core/entry
1.30.0, the Apache-2.0 PIT JUnit Platform plugin 1.2.3, and the existing JUnit Platform launcher
6.1.3. The build's existing Java 21 toolchain and Kotlin 2.4.10 compile and execute the real
provider tests. No paid Kotlin/Arcmutate plugin, mutation service or custom pseudo-mutant
engine is used.

`scripts/PitCatalogue.java` enumerates the installed engine's `ALL` factories and interceptor
defaults. There are 125 distinct factory IDs, including every switch factory and operators
that produce zero mutants. The explicit native selection is now
`--features=-flogcall,+fkotlin`; every other interceptor keeps its installed default.
FKOTLIN is upstream default-on and FLOGCALL is independent. There are no configured
class, method, test or mutator exclusions. `run.json` lists all 27 interceptor defaults,
their effective state and the explicit selection. Native argument files are read back
against that selection; old disabled-FKOTLIN reports cannot serve as candidate evidence.

Both immutable provider source files and every compiled provider class remain inventoried.
The actual AA8 populations below differ by the declared native filter boundary:

| Provider class | Frozen `0d386df` mutants | Candidate mutants |
| --- | ---: | ---: |
| CurrencyEntry | 54 | 6 |
| CurrencyRegistry | 10 | 9 |
| Money | 81 | 78 |
| Money$Companion | 206 | 201 |
| MoneyReason | 1 | 1 |
| MoneySerializer | 32 | 29 |
| MoneyWire | 41 | 4 |
| MoneyWireException | 16 | 14 |
| Total | 441 | 342 |

Nineteen factory IDs produce candidate mutants, versus twenty before filtering; all
125 factory IDs remain in the catalogue, including zero-producing entries.

The target is `com.pennilogic.contracts.money.*`, not API own-main or an easier replacement.
The immutable provider's compiled directory replaces its duplicate JAR on the test classpath.
Only real `com.pennilogic.money.*` Jupiter tests can supply kill evidence.

**Unwaived tool limits:** PIT's native bytecode eligibility/default compiler and equivalence
filters still apply. Static initializers and non-lambda synthetic methods are not mutated.
In particular, passing registry-vector assertions is not a mutation kill of the registry's
static initializer. FKOTLIN removes `.kt` mutants with native mapped line zero, including
meaningful generated value operations and preconditions. This is a lossy compiler heuristic,
not a semantic equivalence check or complete Kotlin support. Generated line numbers may
also refer to Kotlin inline mappings; the report retains native class, method, descriptor
and instruction indexes instead of
pretending every mutant has a literal source line. These limits and the catalogue require
independent review; the author does not approve or discount them.

## Preserved EA56 progression

All rows below used the same engine, full selection, disabled-FKOTLIN scope and original accepted EA56 provider.
Native attempt directories are retained under `build/reports/money-mutation/`.

| Control tests | Killed | Survived | No coverage | Score | Attempt |
| ---: | ---: | ---: | ---: | ---: | --- |
| 16 | 284 | 68 | 87 | 64.692483% | `20261005T101015Z-215301db2b29` |
| 22 | 370 | 64 | 5 | 84.282460% | `20261005T101521Z-23cce6f968d0` |
| 25 | 372 | 62 | 5 | 84.738041% | `20261005T102350Z-23d1de244227` |
| 26 | 376 | 58 | 5 | 85.649203% | `20261005T104612Z-5f6a16387fcc` |
| 27 | 376 | 58 | 5 | 85.649203% | `20261005T110632Z-b73b3d5a6bde` |
| 29 | 378 | 56 | 5 | 86.104784% | `20261005T133436Z-6846f9b950e5` |

All shown runs have zero timeouts, non-viable mutants, memory/run errors, started-but-incomplete
and not-started outcomes. PIT exits 1 for the below-floor score; this is a real gate failure,
not a passing build. Earlier dependency, launch and report-parser failures and repeat runs are preserved too.
No failed attempt is relabelled as a successful baseline.

The additional tests exercise actual data-class value/copy semantics, collection behavior
(including collision-heavy valid keys), Java null boundaries, minimal diagnostics, the
existing JVM-visible internal parser/destruction seams, extra-member rejection and ADR-015
missing-before-extra rejection precedence. They never alter provider bytes or private state,
admit `Long.MIN_VALUE`, widen ledger currencies, or implement another codec.

The two latest tests adopt the genuine gaps demonstrated by the sealed independent bounded
assessment of frozen source `fbf57bf174fb2eb9880647735033415b2375e4d8`. That assessment is not
full Core/QA approval. The API adopted only these two methods, not its four exploratory tests:

| Test | Actual newly killed native identity | Required behavior |
| --- | --- | --- |
| `arrayAndObjectMembersAreShapeRejectionsWithTheirField` | `4da1ee067b2d665db0bc28340cc69efc1e527c19b3f3bc66a0626f03aaeade36` | Array/object amount and currency members reject with `MoneyWireException(SHAPE, field)`, not a cast exception, as ADR-015 section 1.5 requires. |
| `validUnorderedCollectionKeysDoNotInvokeMixedCurrencyArithmetic` | `aa5bc4d4baf98e331e31379882a2417bbaf975e217e2dea7a73d82bfdd0dc8ac` | A fixed valid 17-key unordered set accepts distinct values and retrieves freshly constructed equal values without requesting cross-currency ordering. No exact hash or collision-free distribution is asserted. |

At frozen API `931b0e2b77dbf3d776015797d5d762ddac706b37`, all 439 mutant identities,
125 factories, eight targets and EA56 provider bytes were unchanged.
That baseline's **61 obligations** are 56 survivors and five uncovered mutants. The bounded
assessment distinguishes runtime no-ops, contract-permitted hash alternatives, domain-conditioned
redundancy and blocked defensive observability; those classifications are not exemptions.
The five uncovered mutants remain in `Money.toWire` defensive paths. Arbitrary exact-hash
assertions, compiler-message snapshots, private-state corruption and fake kills are not remedies.
Every unresolved mutant retains its historical native status and denominator membership. Eighteen further
genuine kills would be needed at that denominator; no demonstrated valid-contract route was found
in the bounded assessment, and no speculative broad mutation loop or waiver is substituted.

The assessment also reproduced a separate failure in the **unmutated EA56** provider: another valid
cross-currency hash-set corpus reaches the provider's partial `Comparable` implementation and
throws during unordered insertion. These API tests require successful collection behavior and never
expect that defect. The Contracts-owned correction below removes the old hash-addition mutation;
the defect is not retained to preserve a kill.

## Accepted provider update

The coordinator confirmed [PenniLogic/contracts#35](https://github.com/PenniLogic/contracts/pull/35)
protected accepted commit `aa8d90cb98cec9b6dd08c91b3a4d869e47362662`,
tree `da0d17d9deaee9c049776d16c1511c5840fa16fe`, sole parent
`ea56c63d5c9b679537bd9205b04626049c20c572`. The API materializer and local source-JAR provenance
now pin that commit. Only Money.kt (SHA-256
`eddf78d0c9f694d660a7b13fd8e9e1154bc9b6c801ba0782b72973055d86c79d`) and its upstream golden
binding changed among the same ten verified inputs. The renderer, registry, fixtures, import
mapping, public ABI and accepted Docs strategy are unchanged. No provider implementation is
forked or edited by the API.

The existing unordered-key test retains its original corpus and also requires success for the
separate valid corpus that exposed the unmutated EA56 failure. Both insert 17 distinct valid
values and retrieve fresh equal keys. The test asserts no exact hash, collision-free
distribution or expected provider failure; the logical control count remains 29.

Exactly one new source baseline ran at the unchanged `ALL`/125-factory/eight-class selection,
with `-flogcall,-fkotlin` and the same strategy-derived floors and budgets:

| Attempt | Provider | Killed | Survived | No coverage | Total | Score |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `20261005T162549Z-c292caba4ecf` | `aa8d90c` | 378 | 58 | 5 | 441 | 85.714286% |

Native PIT and the mandatory Money gate both exited 1 solely for below-floor mutation coverage.
Timeouts, non-viable mutations, memory/run errors and incomplete outcomes are all zero.
No full ledger, Postgres or native CI run is claimed for this repin.

Compared with the frozen 439-member population, four native keys disappeared (two killed and
two survived), six appeared (three killed and three survived), and 435 keys are shared.
All four removals and six additions are in the changed Money.hashCode method. One shared key,
`b22eaf7b1fd27fb83c56ca3b893d60d662df53ad180b94d14531b610098af8c9`, changes from killed to survived:
its indexed target changed from removing String.hashCode to removing Long.hashCode.
Two shared keys have different mutation descriptions, and 244 shared keys have shifted source
lines. A matching native key across changed source is not proof of the same semantic mutation.
The old `aa5bc4...` addition/subtraction kill is absent, not credited against the new source.

The frozen AA8 **63 unresolved outcomes** and all original 61 obligation records are preserved
separately. No prior equivalence classification is automatically carried into a changed
hash implementation. Nineteen further genuine kills would be needed at the new denominator;
no speculative repeat loop or waiver was used. Fresh Core/QA and affected Money review remain
required for the new input and native catalogue.

The earlier scratch-only FKOTLIN-default experiment on frozen `931b0e2` is separate evidence:
its 309/340 ratio resulted from removing 99 line-zero identities, including 69 old kills,
without any new kill. It is not the current accepted-provider population or candidate result.
PIT's incomplete-Kotlin/plugin warning remains recorded; complete Kotlin support or an
accepted production configuration decision is not inferred.

Money coverage is 75/75 lines and 83/86 branches, across all eight classes, with 29 tests,
zero failures and zero skips. Actual successful loop completion emits 10,030 round-trip cases,
10,000 associativity cases and 384 collision keys. The consumer matches numeric suite counters
to named Money test cases and requires each property category. Independent-oracle status is
`not_implemented` and its disagreement count is `null`, never an invented zero.

## Prospective FKOTLIN measurement boundary

Root authorized one real proposal after reading the independent retained Core methodology
assessment of `931b0e2`. That assessment is a conditional technical recommendation, not
source approval or separate QA. The accepted strategy and original owner charter require
package mutation floors and a blocking gate; they do not prescribe raw counts of 439/441
or mutation of every Kotlin line-zero body. The extra `-fkotlin` override originated in
this unaccepted harness. Restoring the unmodified built-in filter is an explicit prospective
scope decision, not a waiver of a previously required numerical floor.

The 99 omissions in the old-provider diagnostic include 58 observable generated value
operations, two generated diagnostics and nine invalid-JVM boundary changes that existing
tests killed. Equality, copy and component bodies really lose mutation-sensitivity checks.
Keeping their functional assertions does not replace those checks or ensure future assertion
weakening would be detected. The other 30 omissions are not automatically harmless or
equivalent. No percentage comparison between different populations demonstrates stronger
tests, unchanged mutation sensitivity or a ratchet improvement.

Two interpretation corrections apply to that preserved diagnostic, without overwriting it:

- The 16 carrier-hash alternatives satisfy their own equality/hash law over their field
  domain, but a carrier's non-Comparability does not prove universal mixed-key safety.
  Independent static analysis of the old EA56 MoneyWire return-zero mutant `40ecba6...`
  places seven normal INR wires, then Money keys `(692734317, INR)` and `(4156417587, JPY)`,
  in a capacity-128 map. Changed bucket placement can trigger treeification and the old
  Money-to-Money cross-currency comparison. This was a static prediction, not an executed
  probe, a new native kill or a public-endpoint defect. The accepted AA8 provider correction
  remains intact; the old flaw is not retained for mutation credit.
- For guard-removal identity `eb1b1bc11aa3d1e1c0e8bd6b77aa39ba83535e2f5a0586a135521dfff71403c9`,
  static `parse("bad", null)` reasoning changes the entry NPE to `GRAMMAR`/`amount` rejection.
  Outside the declared non-null domain, exception type and precedence can change, not just
  message text or stack position. This is not a valid-wire financial defect or a newly
  executed test, and it supplies no kill or equivalence transfer.

The filter's `.kt && native line == 0` heuristic has false-positive and incompleteness
limits. It removes observable behavior and leaves other redundancies at nonzero lines.
The existing Kotlin/Arcmutate warning is not resolved by enabling it; no paid plugin is used.
ALL/125 factories, all eight classes, the accepted AA8 provider, all 29 functional/generative
controls, dependency integrity, strategy-derived floors, coverage ratchet and budgets remain
unchanged. No custom per-ID compensation, exclusion system or nonblocking raw lane is added.
Every current candidate outcome still counts according to the ordinary blocking gate.

The frozen old-provider 61 and corrected-provider 63 obligation records remain source- and
methodology-specific history, not cleared findings. Fresh Core and separate QA must assess
the actual candidate and any affected Money/CI risks before Root considers acceptance.
Original API #1/#3/#22, PenniLogic/infra#22, PenniLogic/contracts#1, release, oracle and ledger
requirements remain incomplete.

### One actual candidate result

The sole new ordinary `python scripts\quality.py money-mutation` run is
`20261005T173812Z-9a4cde8485ad`, against the accepted AA8 provider and the unchanged
29 functional/generative controls. Native PIT and the mandatory Money task both exit 0:
309 killed, 28 survived and five uncovered, total 342. Timeout, non-viable, memory/run-error
and incomplete outcomes are all zero. The fresh standalone report consumer also succeeds.

Complete native identity comparison against frozen AA8 finds exactly 99 removals
(69 killed, 30 survived), zero additions and 342 retained identities. Every removal is
a Kotlin native-line-zero identity; every retained identity keeps its status, description
and source context. Thirty-two first-killing-test names differ, so identical dynamic traces
are not claimed. No new kill occurred. Provider/test source and compiled classes, runtime
dependencies, factory catalogue, every other feature and the strategy/budgets are byte- or
value-identical. Of 216 input fingerprints, only the consumer script and two fresh JUnit
result files change. Test-source edits in this proposal affect only Python consumer probes.

The candidate's **33 unresolved outcomes** are not equivalence approvals. The raw AA8
63-obligation record remains intact: 33 are still measured and 30 are filtered, not cleared.
All eleven earlier official native attempts and the old-provider 61-obligation history
remain byte-preserved. This prospective pass does not turn 378/441 or 378/439 into a pass,
nor does it transplant the old 309/340 scratch result.

Actual coverage remains 75/75 lines and 83/86 branches; the unchanged committed baseline
and explicit comparison with frozen `0d386df` pass. All 29 controls pass with zero skips;
round-trip/associativity/collision-key completion stays 10,030/10,000/384, and the independent
oracle is still `not_implemented`. The 57 focused Money Python probes plus five coverage
qualification probes pass, including legacy-feature/argument refusal, below-floor and
missing-number failures, restoration, error accounting, coverage ratchet and process isolation.
These are consumer/guard tests, not additional native mutants or new financial controls.

The existing source/guard/provider checks and lint/compilation checks pass. The normal
`check` wiring is unchanged and its mandatory Money dependency chain actually ran.
No full build, ledger/Postgres test, gate-self-test or native CI run is claimed.

## Fail-closed reports, diagnostics and budgets

Every attempt updates `latest.json` before preflight, including missing-input failures.
Unique attempt directories retain `run.json`, XML, argument files and native logs. The record
binds SHA-256 and byte sizes for provider/strategy inputs, source and compiled/runtime
inventories, Python/Java executables, engine artifacts, build/lock/verification files and
test evidence. It also records exact expanded Java arguments, actual process-launch arguments,
exit codes, elapsed times, feature/factory catalogues and every mutant outcome.
Schema 2 identifies the prospective feature boundary. The consumer requires that schema,
the current signed feature selection, matching effective catalogue state, exactly one native
feature argument, and byte-exact catalogue/engine argument files bound to the actual recorded
Java command. Missing, legacy, duplicate or contradictory selections are refusals even when
synthetic report numbers would pass. The changed consumer source is itself fingerprinted.

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
mutation budget. The frozen 27-test normal build took 70.338562 seconds wall time and its mutation
harness 43.608505 seconds, failing solely on the score. The 29-test correction's focused
`python scripts\quality.py money-mutation` took 78.411215 seconds wall time, including
48.966025 seconds in the mutation harness, and also failed solely on the score.
The accepted-provider baseline took 86.501164 seconds wall time, including 50.169704 seconds
in the mutation harness and 47.698136 seconds in native PIT; it also failed solely on the score.
The prospective candidate command took 97.274915 seconds wall time, including 61.271694
seconds in the harness, 1.856929 seconds in the catalogue and 58.463764 seconds in native PIT.
Its 0 exit is a local numerical result within the declared boundary, not methodology acceptance.

The correction's earlier `moneyMutation build installDist` task-order attempt failed before PIT:
the shape test raised actual branch coverage, requiring the existing baseline to ratchet from
82/86 to 83/86. Despite the requested task order, Gradle also executed 22 migration tests, contrary
to this correction's Money-only execution intent. The failure and execution deviation are retained,
not relabelled as a Money-only normal build. After generating the higher baseline with the existing
coverage consumer, only the focused Money gate was rerun; no further ledger execution occurred.
The normal build wiring is unchanged, but a fresh post-correction full normal build is not claimed.

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
remain preserved for review. The focused Python probes include exact-floor success, just-below-floor
failure without rounding, missing-number failures, restoration, output/input tampering,
error/no-coverage/timeout accounting, and owned-process cleanup. Their explicitly synthetic
consumer fixtures are not counted as real PIT mutants or independent-model evidence.

## Original #22 acceptance and definition-of-done mapping

| Original acceptance criterion | Local evidence and remaining scope |
| --- | --- |
| Deliberate rounding error caught by properties | Not demonstrated. Real round-trip, integer arithmetic and associativity tests exist; debt/budget/allocation rounding modules and their deliberate-fault demonstration do not. |
| Independent-model disagreement fails and names the case | Not implemented. No independent oracle is substituted by provider round trips or made-up disagreement counts. |
| Below published package mutation floor fails the build | Implemented for the materialized `api.money` package: historical native failures and unchanged normal wiring are preserved; `MutationPolicyTest` and the standalone consumer prove numeric-floor refusal and restoration. One actual candidate run passes at 309/342 within the new scope; independent scope/source/QA approval is pending, and old RED runs are not qualified by it. |
| Duplicate idempotency key has one ledger effect | Not implemented. API #3 remains dependent on API #2's real ledger; migration tests are not financial idempotency evidence. |
| Serializable concurrent writes stay non-negative and balanced | Not implemented. No live ledger endpoint or financial concurrency suite exists in this slice. |
| Published numeric pipeline budget is enforced | Implemented locally for normal build and Money mutation processes; `ProcessBudgetTest` proves failure, owned-child containment and restoration. Native required-CI execution and container behavior on forced host termination remain unqualified. |
| Every money package meets published line/branch floors; missing numbers fail | The sole current materialized Money package passes actual full-class coverage and missing-number probes. This is not acceptance of future debt, budgeting, allocation or ledger packages. |

| Original definition-of-done item | Status |
| --- | --- |
| Current-head separate Core reviewer attestation before merge | Pending Root-arranged non-author review. The two author reviews are not independent approval. |
| Every acceptance criterion demonstrated by named tests/evidence | Incomplete as enumerated above. A local candidate mutation result does not complete the independent-model, rounding, idempotency or ledger criteria. |
| Ticket tests run in native required CI/branch protection | Not executed or changed by this local source owner. Existing normal-command wiring is not native acceptance. |
| Observability, rollout and rollback recorded on the issue | Local reports and this guide provide source evidence; no issue write or rollout occurred. Current instructions require a blocking gate with no skipped gate or lowered floor, so the historical non-blocking/threshold-lowering proposals were not enacted. |
| Dependencies mirrored as native blockedBy edges | Read-only frozen issue snapshot records #3 and PenniLogic/docs#22, PenniLogic/docs#35, PenniLogic/infra#3. No remote edge changes or current-state re-verification are claimed. |
| Independent second-reviewer approval or external review | Pending; the author does not satisfy it. Core, QA and the affected Money/reliability/diagnostic risks need Root's appropriate independent review. |

Root owns publication, canonical materialization handoff, integration and any future rollout.
No issue is closed or marked Ready, no failing check is bypassed, and no protected/released
source, real-ledger behavior or full API #22 acceptance is inferred from this local commit.
