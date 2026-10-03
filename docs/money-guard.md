# Local Money and guard preparation

This is bounded local source preparation for [API #1](https://github.com/PenniLogic/api/issues/1),
not issue acceptance, a published codec or authorization to integrate.
[Infra #22](https://github.com/PenniLogic/infra/issues/22) and
[Contracts #1](https://github.com/PenniLogic/contracts/issues/1) remain integration blockers.

## Provider boundary

The accepted [ADR-015](https://github.com/PenniLogic/docs/blob/a700e639585c61a4610e7b99dbd02b2dab28bdcc/adr/ADR-015.md)
requires one Contracts-owned Kotlin wrapper and one serialization seam. The coordinator confirmed
the accepted Contracts source at `ea56c63d5c9b679537bd9205b04626049c20c572` is reusable locally
despite the absence of release tags. `scripts/money_provider.py` checks the size and SHA-256 of
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
interoperability. Full functional contracts, Kotlin/TypeScript/Python generated-client compilation
and cross-language round trips, contract release/adoption, mutation qualification and protected
integration remain pending. No published-client acceptance is inferred from local fixture hashes.

## Commands and ownership

```text
python scripts/money_provider.py --source-root <owned-immutable-contracts-snapshot>
python scripts/money_provider.py --verify
python scripts/check_money.py
python scripts/quality.py money-guard
python -m unittest discover -s scripts/tests
python scripts/quality.py build
python scripts/quality.py gate-self-test --artifact-dir <session-artifacts-directory>
```

The snapshot contains only the size/hash-bound files listed in `scripts/money_provider.py`, fetched
from the accepted full commit, never a branch. It is not another writable repository checkout.
The script performs no downloads, credential operations, full client generation or publication.
Prepared inputs live in `build/contracts-money/source`; missing or changed inputs/output stop,
without overwriting a mismatched cache or synthesizing a registry.

The manual `prepareMoneyProvider` and `moneyGuard` Gradle tasks run before Kotlin production/test
compilation and as part of `check`, including direct Gradle builds. The guard is an unconditional
hard failure, with no warn-only switch. The existing native CI command still invokes
`python scripts/quality.py build`; a fresh checkout without materialized source inputs fails
explicitly. Canonical CI source materialization is a coordinator/Infra handoff, not a local CI
exception or a generated-workflow edit.
The existing test/lint checks, coverage floors and integration-test requirements are unchanged.

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

Money value-type operator dispatch is allowed: `left + right` when both values are Money is not
raw integer arithmetic. The authoritative wrapper's own implementation remains the exact
upstream source dependency, not a locally invented exception or duplicate type.

This is a conservative lexical guard, not Kotlin compiler type resolution or complete
interprocedural taint analysis. Naming rules and local aliases are tested; arbitrary indirect
factory return types and externally defined serializers still require the accepted provider,
compiler checks and independent review. Neither scanned-file counts nor planted local failures
prove native protected CI enforcement, cross-language interoperability or completion of API #1.

The isolated gate self-test plants a failed assertion and bad formatting, then monetary Double,
Float and BigDecimal fields, a floating conversion and raw integer arithmetic. Each monetary case
must fail specifically at `moneyGuard` with its expected rule; the fixture is removed and a final
normal build must recover. These fixtures are synthetic code, not a substitute Money implementation.
