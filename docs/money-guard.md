# Local Money and guard preparation

This is bounded local source preparation for [API #1](https://github.com/PenniLogic/api/issues/1),
not issue acceptance, a published codec or authorization to integrate.
[PenniLogic/infra#22](https://github.com/PenniLogic/infra/issues/22) and
[PenniLogic/contracts#1](https://github.com/PenniLogic/contracts/issues/1) remain integration blockers.
The subsequent [API #22 mutation slice](money-mutation.md) is also local and unaccepted.

## Provider boundary

The accepted [ADR-015](https://github.com/PenniLogic/docs/blob/a700e639585c61a4610e7b99dbd02b2dab28bdcc/adr/ADR-015.md)
requires one Contracts-owned Kotlin wrapper and one serialization seam. The coordinator confirmed
the protected accepted Contracts source at `aa8d90cb98cec9b6dd08c91b3a4d869e47362662`
([PenniLogic/contracts#35](https://github.com/PenniLogic/contracts/pull/35)) is reusable locally
despite the absence of release tags. It supersedes `ea56c63d5c9b679537bd9205b04626049c20c572`
only as this source dependency, not as API acceptance. `scripts/money_provider.py` checks the size and SHA-256 of
every required source input, invokes the unchanged upstream registry renderer, and checks both
Kotlin outputs against the upstream golden hashes. The API packages those exact outputs as a
separate, local source dependency. No second Money class, codec, registry format, currency fallback,
time/idempotency shape or deduplication outcome is defined here.

`AcceptedMoneySourceTest` uses the actual wrapper and serializer. It exercises the unchanged
valid/invalid/parsing fixtures, 10,000 seeded plus 30 boundary vectors, shared registry completeness,
exponents, checked arithmetic, associativity, mixed-currency refusal and the accepted symmetric
range. `Long.MIN_VALUE` is explicitly rejected; the minimum is `-Long.MAX_VALUE`.
Registry acceptance of INR/JPY/KWD is codec support, not ledger admission: the MVP ledger remains
INR-only and no ledger endpoint or migration is changed.

These are Kotlin source-seam tests, not generated-client transport tests or three-language
API interoperability. Full functional contract/release adoption and protected integration remain
pending. The separately executed three-language Contracts scaffold is not an API cross-language
consumer test. No published-client acceptance is inferred from local fixture hashes.

## Commands and ownership

```text
python scripts/money_provider.py --source-root <owned-immutable-contracts-snapshot> --strategy-file <accepted-docs-test-strategy>
python scripts/money_provider.py --verify
python scripts/check_money.py
python scripts/quality.py money-guard
python -m unittest discover -s scripts/tests
python scripts/quality.py build
python scripts/quality.py coverage --base <full-trusted-base-commit-SHA>
python scripts/quality.py money-coverage
python scripts/quality.py money-mutation
python scripts/quality.py money-mutation-report
python scripts/quality.py gate-self-test --artifact-dir <session-artifacts-directory>
```

The Contracts snapshot contains only the ten size/hash-bound files listed in
`scripts/money_provider.py`, fetched from the accepted full commit, never a branch.
The additional Docs input is the unchanged `governance/test-strategy.json` from
`a700e639585c61a4610e7b99dbd02b2dab28bdcc` (80,953 bytes, SHA-256
`a0322e0337c7c0e496711f21a1aea01136c6047b787318fd805a0788c2239bb2`).
The snapshots are not other writable repository checkouts.
The script performs no downloads, credential operations, full client generation or publication.
Prepared Contracts inputs live in `build/contracts-money/source`, and the exact Docs input in
`build/contracts-money/strategy/governance/test-strategy.json`; missing or changed inputs/output stop,
without overwriting a mismatched cache or synthesizing a registry.
On an authorized provider repin, preserve the old `build/contracts-money` bundle before
materializing the new snapshot; the preparer intentionally refuses to overwrite stale
inputs or generated source. This repin changes only Money.kt and its upstream golden
binding among the ten inputs. The renderer, registry, fixtures, import mapping and
accepted Docs strategy remain byte-identical.

The existing canonical materialization catalogue must also move from the EA56 commit to
`aa8d90cb98cec9b6dd08c91b3a4d869e47362662` (tree
`da0d17d9deaee9c049776d16c1511c5840fa16fe`) before fresh CI inputs can satisfy these pins.
Root must route that catalogue update even though command/profile shapes are unchanged:

| Catalogue entry | Required accepted AA8 value |
| --- | --- |
| `runtime/kotlin/src/main/kotlin/com/pennilogic/contracts/money/Money.kt` | 8,489 bytes; SHA-256 `eddf78d0c9f694d660a7b13fd8e9e1154bc9b6c801ba0782b72973055d86c79d` |
| `generator/golden.json` | 7,609 bytes; SHA-256 `62ea59630dfb1e1d028b82c411f7efce575c2403ba277ff807333cdc0423a328` |
| Rendered Money.kt output | Same accepted Money.kt SHA-256 above |

The other eight input sizes/hashes, registry output
`10d491ae90bc8f077afe5d45be671af52a5b7891c88eacfccd437a10f4956b21`, and Docs strategy
are unchanged. Neither stale EA56 pins nor an unaccepted Contracts branch are valid substitutes.
No canonical Infra catalogue or generated consumer is edited by this API source owner.

The manual `prepareMoneyProvider` and `moneyGuard` Gradle tasks run before Kotlin production/test
compilation and as part of `check`, including direct Gradle builds. The guard is an unconditional
hard failure, with no warn-only switch. The existing native CI command still invokes
`python scripts/quality.py build`; a fresh checkout without materialized source inputs fails
explicitly. Canonical CI source materialization is a coordinator/Infra handoff, not a local CI
exception or a generated-workflow edit.
The existing test/lint checks, coverage floors and integration-test requirements are unchanged.
The additional `moneyMutation` task is mandatory in `check`; it cannot be silently skipped by
the normal build. Missing materialization or a red mutation score still prevents qualification.
Build, full `coverage` and focused `money-mutation` verify the latest mutation record against
the final input/output bytes before success. Full coverage refreshes mutation through the
existing Gradle dependency graph after its forced Money tests: build followed by coverage
performs two real PIT runs, within each command's remaining at-most-600-second budget.
The coverage-only commands do not qualify mutation and can stale an earlier result;
`money-mutation-report` only reads and verifies, never repairs it. See the
[terminal-freshness correction](money-mutation.md#terminal-freshness-after-full-coverage).

## Actual Money-package qualification

The existing JaCoCo 0.8.15 tool now measures the authoritative dependency, not unchanged API
own-main code. `moneyTest` executes the source-seam tests; `moneyCoverageReport` includes all
eight compiled classes from both `Money.kt` and `CurrencyRegistry.kt`, without class, method or
branch exclusions. `moneyCoverageCheck` fails a normal build below the accepted `api.money`
97% line or 93% branch floor. It reads the numbers directly from the byte-bound Docs strategy;
there are no local fallback thresholds. Missing target tests, skips/failures, omitted classes,
omitted source files, unrelated package totals or inconsistent counters are refusals.

The initial twelve tests measured 74/75 lines and 76/86 branches, and the branch gate failed.
Additional equality, descriptor, rendering and non-JSON encoder/decoder tests improved the
actual complete-package result to 75/75 lines (100%) and 81/86 branches (94.19%) across sixteen
tests. The additive API #22 JVM/value/diagnostic tests reached 75/75 lines and 82/86 branches
across 27 tests. Two independently demonstrated regression cases now raise that result to 75/75
lines and 83/86 branches across 29 tests. All three remaining branches stay in the denominator; no fabricated invalid
instance, registry mutation or provider-source change was used to erase them.
The accepted `aa8d90c` hash correction preserves that same measured coverage and 29-test
inventory. The existing unordered-key control now also covers the former unmutated failure;
the generated baseline changes its provider reference, not its counters or ratchet.

`quality/money-coverage-baseline.json` is generated from this actual report. The native package
gate refuses a decline from that recorded baseline even above the floors. The documented
reviewed-base `coverage --base <full SHA>` command additionally compares the committed base's
Money baseline, when it exists. This is the first Money-package baseline; `04f9701` had none,
which is reported explicitly rather than using its unrelated own-main coverage.
Regeneration uses `money-coverage --write-baseline --base <full reviewed SHA>` and still refuses
undercoverage or a decline; changing report counters or the accepted strategy is not a remedy.

**Mutation is executable; the proposed measurement scope is not accepted.**
[The API #22 gate](money-mutation.md) runs pinned PIT against the actual provider, with the
complete selected factory/class catalogue. The first fully parsed
baseline killed 284/439 mutants; meaningful additional tests kill 378/439 (86.104784%).
That EA56 history is preserved. The separately accepted provider correction produces a new
baseline of 378/441 (85.714286%): 58 survivors and five uncovered mutants, with no credit for
either category. Those disabled-FKOTLIN runs remain RED history. The current ordinary gate
still blocks below the mutation floor read from the same accepted strategy. Coverage is
reported separately, never substituted for mutation evidence.
The prospective restoration of built-in FKOTLIN changes measurement scope, not those old
results: Kotlin line-zero mutants, including meaningful generated bodies, are omitted.
It does not prove stronger tests, unchanged sensitivity, equivalence or a better mutation
ratchet. FLOGCALL stays disabled; all 125 factories, eight classes, tests, provider pins,
numerical floors, coverage ratchet and budgets remain unchanged. Legacy reports cannot
qualify the candidate because source fingerprints and native feature/argument readback
must match. There is no nonblocking raw lane or per-mutant compensation.
The one current-AA8 candidate run, `20261005T173812Z-9a4cde8485ad`, passes the same numerical
floor at 309/342 (90.350877%): 28 survivors and five uncovered, no timeouts or mutant errors.
It removes 99 identities (69 prior kills and 30 survivors) and adds no kills; 33 outcomes
remain unresolved in the measured population. All original 61 and raw AA8 63 obligations
remain historical evidence, not waived findings. The 29 controls and 75/75-line, 83/86-branch
coverage are unchanged. This local prospective result still requires fresh Core and separate
QA review and does not establish whole-API, native CI, ledger or release acceptance.
Independent QA rejected frozen `35267fc` for accepting a rebound, survivor-omitting report
despite contradictory native counts. The [Q1 correction](money-mutation.md#q1-native-summary-consistency-correction)
requires the bound native log and exact Generated/Killed-to-XML agreement in both execution
and readback. The correction has its own current-source native 309/342 result and still
needs separate admission; it does not change the original genuine record, mutation scope,
or the prior negative source decision.
The tests require shape/field rejection and successful valid unordered-key behavior, not exact
hashes. The old hash-addition mutation no longer exists in the corrected provider and earns no
current credit. The former unmutated collection defect is covered as successful behavior,
never an expected failure. New source/catalogue evidence still requires fresh independent review.

`build.gradle.kts`, `scripts/quality.py`, its tests and this guide are manual API source;
they are not outputs of the accepted Infra `governance/generate.py` artifact inventory.
Generated workflow, checker, setup, policy, AGENTS, README and CONTRIBUTING files are untouched.
Any changes to generated command/profile wiring belong to Infra and must be serialized by the
coordinator, not hand-edited here.

## Source scope and rules

The guard scans every `.kt`, `.kts` and `.java` file under the checkout, including test code,
serializer directories and new/untracked or git-ignored source outside `src/main`.
Only root-level build/tool metadata directories are excluded: `.git`, `.gradle`, `.kotlin`,
`.idea`, `.venv`, `node_modules` and `build`. A nested source directory called `build` is scanned.
Own JVM sources are guarded across that scope. The two upstream Money dependency sources are
compiled in the separate `contractsMoney` source set: their exact inventory and bytes are verified
before compilation, like a pinned dependency, rather than exempting an editable local value type.
Any additional generated/application JVM source adoption must bind its inventory explicitly.

Each execution emits a `money_guard` JSON event with `source_files_scanned` and `violations`;
`dependency_sources_verified` separately reports the two immutable upstream sources.
Empty inventories, unreadable/invalid UTF-8 files, source symlinks/Windows junctions and unterminated tokens fail
closed. Diagnostics contain only file/line/column, a stable rule, and a bounded escaped field
name, never an initializer, amount, account value or source line.

| Rule | Refusal |
| --- | --- |
| `MG001` | Money-named Kotlin/Java declarations using Double, Float or BigDecimal, including nullable/container/import/type aliases and inferred floating values |
| `MG002` | Arithmetic on raw minor-unit members or their local aliases, including integer arithmetic methods and numeric aggregation |
| `MG003` | Floating/decimal conversion in monetary expressions |
| `MG004` | Floating descriptor fields or numeric monetary serializer calls |
| `MG005` | A money-named declaration whose type cannot be established, including unresolved cross-file aliases and inferred factory results |
| `MG000` | An incomplete/unreadable source inventory or tokenization |

The shared Java declaration type applies to each comma-separated declarator, including names
after initialized variables. Commas nested in calls, arrays, generic arguments or initializer
bodies do not start another declarator, and a later typed declaration establishes its own type.
Only a complete type at a declaration boundary supplies that shared type: shift operands and
generic method calls in an initializer cannot replace it. Type-use annotation arguments are
excluded from the type without losing the primitive, qualified or container type around them.
Contextual keywords follow Java identifier rules: they may name variables even when they
cannot name a simple type. Segments qualifying a type follow package-name identifier rules,
so a contextual package name does not discard the declaration's shared type.
Local `var` declarations, including `final var`, use initializer inference rather than a
concrete shared type. Known Money aliases remain allowed; numeric and unresolved monetary
initializers retain the existing refusals. Legal variable or package names `var` stay supported.
Raw minor-unit aliases still feed the arithmetic, conversion and numeric-serialization rules.

Recognized Java declaration type positions are separate from money-named value identifiers.
The same declaration boundaries distinguish imported `Money` type tokens in fields, locals,
parameters and method returns, including annotated arrays, containers and generic methods.
This is a positional distinction, not an exemption for the name `Money`: actual money-named
values, initializers and annotation arguments retain their refusals and raw-alias tracking.
The imported-type regression controls use the genuine Contracts type, not a replacement class.

Java class/interface/record and recognized generic-method type parameters retain their
declaration scopes and individual bounds. A simple type parameter shadowing `Money` is not
the imported wrapper: floating bounds refuse monetary declarations with `MG001`, and
unbounded or unresolved monetary types retain `MG005`. Referenced bounds follow local
chains, including forward references, in the bound's declaration environment; unrelated
parameters do not taint a genuine Money return. Nested shadows end with their declarations.
Qualified type names remain distinct from local parameters, and varargs dots are not a
package qualifier. Shared declarators, arrays, parameters and raw aliases keep their rules.
The regression controls include real compiler-valid shadows beside genuinely imported
Money fields and safe bounds; recursive bounds cannot recover the wrapper by spelling alone.

Money value-type operator dispatch is allowed: `left + right` when both values are Money is not
raw integer arithmetic. The authoritative wrapper's own implementation remains the exact
upstream source dependency, not a locally invented exception or duplicate type.

This is a conservative lexical guard, not JVM compiler type resolution or complete
interprocedural taint analysis. Generic constructors and full inheritance/member or
value-identifier scope resolution are outside this model; the recognized declaration
shapes do not certify every Java grammar. Naming rules and local aliases are tested; arbitrary indirect
factory return types and externally defined serializers still require the accepted provider,
compiler checks and independent review. Neither scanned-file counts nor planted local failures
prove native protected CI enforcement, cross-language interoperability or completion of API #1.

The isolated gate self-test plants a failed assertion and bad formatting, then monetary Double,
Float and BigDecimal fields, a floating conversion and raw integer arithmetic. Each monetary case
must fail specifically at `moneyGuard` with its expected rule; the fixture is removed and a final
normal build must recover. These fixtures are synthetic code, not a substitute Money implementation.
If the real mutation result is below its floor, that final normal build remains red; the
self-test must not bypass mutation to manufacture a recovery. No full self-test/ledger run
is claimed for the bounded feature proposal. The separate mutation-consumer
tests demonstrate below-floor/missing-number rejection and restoration without claiming a
passing real mutation run.
