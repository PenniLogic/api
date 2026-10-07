# Generated-model Money interoperability

`moneyClientInterop` is an API-owned **test-only** gate for the original
[api#1](https://github.com/PenniLogic/api/issues/1) generated-client round-trip
criterion. It uses accepted Contracts commit
`aa8d90cb98cec9b6dd08c91b3a4d869e47362662`, tree
`da0d17d9deaee9c049776d16c1511c5840fa16fe`, and ADR-001 / ADR-015. This is exact-commit
source consumption, not adoption of a released client, a product endpoint, or an
independent arithmetic oracle. It does not close api#1, api#22, or their other
acceptance and operational prerequisites.

## What actually executes

The accepted default OpenAPI 0.1.0 has no operations or Money-bearing generated
DTO. Its Money files are hand-written seams, not evidence of generated-model
interoperability. The gate first runs the accepted
`scripts/generate_clients.py --verify`, retaining the default golden check. It
then adds the same `SyntheticEnvelope` used by accepted
`scripts/tests/test_generate.py::SeamWiringTest` to an owned scratch specification.
The canonical Money, Instant and LocalDate definitions remain unchanged. The
real generator 7.25.0 emits all three models; scratch manifests and hashes are
recorded separately from default golden output.

The API test-classpath bridge constructs the **existing backend Money
dependency**, uses its real `MoneySerializer` to emit wire data, and consumes
each client's result using that same backend deserializer. Between those two
backend boundaries, the gate executes:

| Client | Actually emitted boundary |
| --- | --- |
| Kotlin | `SyntheticEnvelope.serializer()` with the accepted `PennilogicJson` configuration |
| TypeScript | `SyntheticEnvelopeFromJSON` and `SyntheticEnvelopeToJSON`; the field must be a Money wrapper with internal `bigint` |
| Python | `SyntheticEnvelope.from_dict`, `model_validate`, `to_dict`, `to_json` and Pydantic serialization |

All 10,030 accepted rows execute in their original order: 10,000 seeded rows and
30 boundaries covering INR, JPY and KWD. Ordered case IDs map directly to the
hash-bound canonical fixture, including its original names. The backend checks
exact wire equality, minor-unit equality and currency, and recomputes the
accepted line digest
`6d3172f611bc69f7d529912f68539a1e66101a1c188710ff2400ceab4fbb1ef6`.
No alternate Money parser, formatter, exponent table or arithmetic is introduced.
The symmetric Long range still excludes Long.MIN_VALUE.

The 41 accepted invalid Money objects also execute through the backend and all
three generated models. Backend reasons and fields must match the accepted
vectors. Each client additionally plants a valid-but-different Money object in
the first **generated model's field**, serializes it through its emitted
converter, and requires the backend to refuse that exact case for wire
disagreement. A fresh, full clean conversion must then recover successfully.
No failed, absent or skipped leg can count as a pass.

## Inputs and commands

The new API-owned `scripts/tests/fixtures/money_client_interop/source-inputs.json`
binds 39 immutable Contracts files: generator/configuration/templates, all three
runtime seams, canonical fixture/registry data, and the accepted compiler and
dependency pins. It deliberately does **not** change the older native
materializer's 11-input catalog, its generated helper, the provider, policy,
workflows, production OpenAPI, or dependency verification metadata.

Prepare the existing API Money provider using the repository's documented
commands first. Then:

```text
python scripts/money_client_interop.py prepare
.\gradlew.bat --no-daemon --console=plain -Pkotlin.compiler.execution.strategy=in-process moneyClientInterop
python scripts/money_client_interop.py verify
python -m unittest discover -s scripts/tests -p test_money_client_interop.py
python scripts/quality.py build
python scripts/quality.py gate-self-test
```

On Linux, use `sh ./gradlew` in place of `.\gradlew.bat`. The normal `check`
graph depends on `moneyClientInterop`, which depends on `testClasses`. The
interop task is never up-to-date or build-cache skipped; it does not depend on
`check`, `test`, or `moneyMutation`, and does not launch the API build recursively.
The separate accepted Kotlin smoke project compiles the generated client with
its own existing, checksum-verified dependencies. An API-owned init script adds
only test bridge/source/output wiring; it edits neither that project's sources
nor generated files and adds no dependencies.

Fresh normal builds have a reproducible preparation path. Acquisition reuses ten
already-verified native provider inputs, then uses the existing native
`ReadOnlyClient`, `GitSnapshot`, hash/blob, path and budget primitives for the
remaining 29 files. Two fixed batches make 35 bounded, anonymous public Git-object
GETs; repository/organization IDs, immutable commit/tree, byte count, SHA-256 and
Git blob identity must agree. Each native request/response budget is unchanged,
and an outer native deadline bounds the whole acquisition. This transport is
distinct from personal authenticated metadata research; inherited tokens are
not used. A refusal is not an invitation to bypass an authentication guard.
Helper modules execute from source bytes, without reading or writing unbound
Python bytecode caches beside immutable inputs.

An exact, owned offline snapshot can be supplied explicitly:

```text
python scripts/money_client_interop.py prepare --offline-source "<owned exact 39-file snapshot>"
```

It must contain exactly the cataloged files, with no links or extra executables.
Offline provenance is recorded as offline, not as a native network fetch. Partial,
changed or linked installations are preserved and refused, never blindly replaced.
There is no workstation-specific path in the normal build.

Tool readiness is checked before restoring missing dependencies. Restores use
only the accepted generator/toolchain downloader, hashed binary-only Python
requirements, `npm ci --ignore-scripts` with the accepted lock, and the accepted
Gradle wrapper and verification metadata. Cached tool mismatches are refusals,
not instructions to download an unpinned replacement. Existing default generated
trees are checked against the accepted golden files before regeneration; partial
or modified output is not silently healed. Node/npm must already be available
and satisfy the accepted package's runtime requirements; this gate does not
install an unpinned system runtime. No service, paid runner,
release, CI dispatch, publication or deployment is involved.

The separate `quality.py gate-self-test` command copies
`scripts/money_client_interop.py`, `scripts/tests/fixtures/money_client_interop`,
and `build/source-materialization` into its isolated working directory. Its final
clean `build` runs this gate after the planted test, lint and Money-guard refusals
are restored. Generated outputs, client tool caches and passing interop reports
are not copied; the scratch build uses the same verified preparation path.

## Evidence and limits

Each invocation creates a fresh run under `build/money-client-interop/runs/`.
An exclusive `run.lock` prevents concurrent writers; an abandoned lock is
refused rather than deleted or silently reused.
`current.json` is invalidated before execution. A successful report binds the API
bridge/build/source inputs, backend and generated-client classpaths, generated
trees, scratch specification, commands, separate output streams, counts and
all run outputs. Offline `verify` checks those bindings again, rechecks actual
runtime receipts against their counts and run identity, and validates every
returned transport against the canonical corpus. A recomputed file digest alone
cannot make an empty, skipped or stale receipt qualify. Missing,
truncated, duplicate-key, wrong-version/source, stale, modified, zero-case,
unexecuted and skipped evidence fails closed. Old success cannot rescue a
failed new attempt.

Wire files contain **synthetic fixtures only**. Runtime stdout contains bounded
metadata receipts; errors use static causes and case IDs, never amounts or raw
payloads. Unexpected runtime output is retained only as byte counts and hashes,
not copied into logs or reports. The owned process-tree budget reuses the API's
existing process-budget helper and never stops a shared service or daemon.
The report's `elapsed_seconds` covers execution and binding collection before
final verification; the success event and external command timing include that
verification. Neither is hosted job/workflow timing.

The older provider's `generated_clients_tested: false` remains truthful for its
own narrower source-only qualification. This separate gate does not upgrade that
flag or the broader `independent_oracle: not_implemented` state. Existing Money
coverage/mutation floors and the whole hosted job **and** workflow budget of
less than 600 seconds remain unchanged. Local timing is not native CI proof;
separate exact-head Core, Money, QA and applicable acquisition-risk Security
review, followed by protected native CI/integration, remain required.
