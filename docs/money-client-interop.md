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
python -I -S -B scripts/money_client_interop.py prepare
.\gradlew.bat --no-daemon --console=plain -Pkotlin.compiler.execution.strategy=in-process "-PmoneyClientInteropPython=<approved absolute Python executable>" moneyClientInterop
python -I -S -B scripts/money_client_interop.py verify
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

The Gradle entry selects the approved **absolute** Python executable from
`moneyClientInteropPython`, `PENNILOGIC_PYTHON` (set by the shared process-budget
helper), or the hosted setup-python `pythonLocation`, in that order. It does not
search the ambient PATH. Its environment is filtered before Python starts with
`-I -S -B`. Node comes from the standard host installation or the explicit
`moneyClientInteropNode` Gradle property (`--node` for a direct `run --java ...`).
The paired npm CLI is resolved beside that installation or from the standard
Linux npm package location. The accepted Node/npm versions must already be
installed; the gate neither downloads a system runtime nor changes hosted setup.
For a standalone workstation `quality.py gate-self-test` without setup-python's
`pythonLocation`, set the process-local `PENNILOGIC_PYTHON` to that approved
absolute executable first. The budgeted normal `quality.py build` supplies it
through the shared helper; this requires no global PATH, hook or configuration
change.

For an explicitly provisioned SDK, the manual quality CLI accepts the same
approved Node executable without relying on ambient Gradle properties:

```text
python scripts/quality.py build --money-client-interop-node "<approved absolute Node executable>"
python scripts/quality.py build --base "<full trusted base SHA>" --money-client-interop-node "<approved absolute Node executable>"
python scripts/quality.py coverage --base "<full trusted base SHA>" --money-client-interop-node "<approved absolute Node executable>"
python scripts/quality.py gate-self-test --artifact-dir "<owned parent>" --money-client-interop-node "<approved absolute Node executable>"
```

The optional argument defaults to omission and preserves standard-installation
behavior. An explicit path reuses the collector's absolute-file, `node`/`node.exe`
basename and existing paired-npm validation before Gradle starts; validation
does not execute the candidate or search PATH/tool caches. Every Gradle-producing
quality command forwards one unsplit `-PmoneyClientInteropNode=...` argument after
the compiler property and before task names. All nine isolated self-test calls
retain it, including the seven plants and clean restoration. Report-only commands
and the direct Money guard reject the option; collector `prepare`/`verify` and the
separate Kotlin smoke build do not gain new flags or tasks.

On Windows, Python's argv quoting does not make `gradlew.bat` operands literal
CMD data. The quality boundary therefore refuses `"`, `&`, `|`, `<`, `>`, `(`,
`)`, `^`, `%`, `!`, ASCII control characters and DEL before batch dispatch.
This covers the complete wrapper path, explicit Node property and task arguments
in both budgeted and unbudgeted calls, including every isolated self-test call.
The CLI also rejects an unsafe explicit Node path before starting any other
command. Refusals have a static diagnostic and do not run the wrapper or runtime.
Adding quotes or caret escapes does not bypass this conservative restriction.
Plain spaces remain supported in SDK and checkout paths; parenthesized paths
such as `Program Files (x86)` require an approved alternative without CMD syntax.
POSIX `sh` dispatch, omitted-Node behavior and direct executable-only collector
runtime validation are unchanged. No ambient environment or PATH is admitted
to work around the restriction.

Windows dispatch resolves relative roots against the caller's current directory
and checks both the effective absolute working directory and resolved wrapper
target, in addition to the supplied argv. The wrapper is then invoked by its
absolute path under that validated working directory, without PATH lookup.
Using `.`, `..` or a drive-relative root cannot hide an unsafe checkout name from
the check. POSIX root and wrapper spellings remain unchanged.

Each owned baseline/candidate copy must run its own quality script with its own
working directory and self-test artifact parent. Only the explicit SDK path is
shared, not launch files, generated output or runtime evidence. `coverage --base`
reads the named Git baseline; it does not build another checkout, and forwarding
this option does not add interoperability to the coverage task graph.
The canonical CI adoption from accepted Infra `6b1e4baf403f25e6c4c695a5676e995f1ecb259e`
keeps the isolated SDK preparation and passes its path to a single `build --base`
invocation. That command runs the existing build/install graph and test metrics,
then both general and Money reviewed-base comparisons before terminal mutation
verification, without starting another Gradle/PIT graph. Omitting `--base`
preserves the manual build behavior. After the normal CLI and optional Node
guards, explicitly empty, malformed, missing or non-commit bases fail before
provider/budget reads, Gradle or test metrics, rather than disabling comparisons.
Standalone `coverage` and explicit baseline regeneration remain available.

The budgeted quality caller also admits the standard Windows Docker Desktop CLI
directory on the approved system drive (`Program Files\Docker\Docker\resources\bin`).
It does not inherit arbitrary ambient PATH entries or startup/credential options.
If Windows Job attachment fails before release, the helper terminates and reaps
the exact owned bootstrap and closes its pipes; the interop launcher records a
refusal rather than leaving completion `running`.

Retained command exit metadata uses exact integers, including readiness probes.
Only a genuinely unexecuted optional probe may have a null actual exit; its
expected exit must still be an integer. A fresh missing-path probe's actual
exit 3 with expected exit 0 remains valid and triggers preparation, not success.

The accepted package requires Node `>=24.14.0 <25` and npm `>=11`.
Configuring only Python/JDK, or prepending an arbitrary Node executable to PATH,
does not satisfy the explicit runtime boundary above. The published
`ubuntu24/20260927.320` runner inventory lists default Node 22.23.3/npm 10.9.9
and a separately cached Node 24.21.0. Selecting or provisioning the supported
SDK and handing off its approved absolute path is a canonical CI/setup
responsibility; this gate does not auto-select another tool cache, install a
system runtime, or weaken the package engines. Infra-owned generated workflow
changes must be made at their canonical source, not hand-edited here.

A failed `typescript-install` emits a bounded
`money_client_dependency_failure` event before the existing
`command-failed-typescript-install` refusal. It contains only the actual exit
code, a finite allowlisted npm error code (or `unclassified`), and each stream's
byte count/SHA-256. It never prints the raw package-manager output, paths,
configuration or credentials. Unknown, ambiguous and malformed code lines are
not promoted to a specific diagnosis; success and other command protocols are
unchanged. Complete bounded command streams remain in the owned private
execution evidence. The historical 7b native run has no uploaded command
streams, so its precise npm error is not retroactively established by this
diagnostic addition.

The original cold path exhausted the shared anonymous REST quota: Money
preparation makes 17 requests and database preparation makes 28; adding 35
per-blob interop requests required 80, exceeding one 60-request pool. The native
[PR #102 failure](https://github.com/PenniLogic/api/actions/runs/37620907002)
was a failure, not cross-language or full-build qualification.

Acquisition now reuses ten already-verified native provider inputs, then makes
three REST GETs with the existing `ReadOnlyClient` and `GitSnapshot` to verify
the exact numeric repository/organization identities, immutable commit and
reconstructed complete Git tree. One additional, credential-free GET to
`https://codeload.github.com/PenniLogic/contracts/tar.gz/aa8d90cb98cec9b6dd08c91b3a4d869e47362662`
supplies the remaining 29 cataloged files. The complete cold preparation
pipeline therefore uses **48 REST requests plus one archive request**, not 80
REST requests. This does not reserve quota against unrelated users or excuse
actual exhaustion: there is no retry, backoff, proxy, token fallback or endpoint
lottery.

`prepare --native-fetch` is a separate, explicit preparation-only selector for
the reviewed canonical `ReadOnlyClient(data, native_fetch=True)` interface.
The caller does not read credentials or infer the mode from its environment.
The canonical client validates the native context and consumes
`PENNILOGIC_NATIVE_SOURCE_TOKEN` before any existing-input short-circuit; that
same client is reused for cold acquisition. The selector requires the matching
accepted canonical client and workflow policy; the API caller alone neither
implements nor authorizes native authentication. It is refused for `run`,
`verify`, `_run`, or in combination with `--offline-source` or
`--require-prepared`. Default preparation remains anonymous, with no token
fallback. Archive requests retain their separate credential-free headers.
Official generation from accepted Infra
`687d4d9acbd8a0f801c9ac4267230c8c67735eb0` supplies the matching client and
workflow steps. Offline tests use that client with synthetic credentials and
responses; they do not establish hosted authentication or quota availability.

The canonical native workflow must keep its two token-scoped acquisition steps
on either side of the existing credential-free Money provider preparation and
verification. Interop still verifies those provider inputs before its three
metadata GETs; provider execution must not be moved into a token-scoped step.
After acquisition, `prepare --require-prepared` verifies the complete 39-input
inventory and acquisition receipt with zero requests and without executing
Node, JVM or the full interop gate. Missing, partial or changed inputs/receipt
refuse rather than reacquire.

The same non-secret `--require-prepared` flag on `quality.py build` or
`quality.py test` forwards `-PmoneyClientInteropRequirePrepared=true` to Gradle.
The interop task forwards it through `run` into the isolated `_run` worker,
which repeats strict preparation before using the inputs. Disappearance after
the preceding verification therefore cannot enable anonymous reacquisition.
That same explicit Gradle condition removes `--fetch` from the existing
database preparation task, using its offline prepared-input verification at
the later build use point as well. Money provider source/strategy validation
already refuses missing inputs without any remote acquisition fallback.
No environment variable or marker selects this condition. Omitted flags keep
the existing manual/Conformance behavior; other quality commands and full
interop `verify` do not accept this selector. This input condition does not
replace SDK, execution, coverage, mutation or native acceptance checks.

The API-owned binary reader reuses the native no-proxy/no-redirect opener,
framing checks and budget primitives; the native reader itself decodes JSON and
is unchanged. The archive is bounded to 2 MiB compressed and 4 MiB unpacked,
within a single native 4 MiB response-byte/32-request/180-second budget and
10-second per-request deadlines. Truncated, concatenated, trailing, oversized
or malformed compressed data, unsafe/duplicate/unbound paths, links, sparse or
special entries and invalid archive termination are refused. Nothing is
extracted to disk. Selected members must match the verified tree's path, mode,
size and blob identity, plus the unchanged catalog's SHA-256; only these
verified bytes are subsequently staged. Unselected contents are never executed
or published.

GitHub's archive exports the pinned `smoke/kotlin/gradlew.bat` with CRLF rather
than its canonical Git-blob LF bytes. Only that cataloged member may undergo
this transport normalization, with at most twice its bound size before
normalization. Its resulting bytes must still match the **unchanged** exact
size, Git blob and SHA-256. No other file, line ending or hash check is relaxed.
The raw archive hash remains bound and the receipt explicitly names any
canonicalized member.

The strict native acquisition `/2` receipt distinguishes the three metadata
requests from the archive transfer and binds its compressed/unpacked sizes,
member count, digest, canonicalized paths, aggregate response bytes and elapsed time. Old per-blob
native receipts do not qualify as the new route. Explicit offline `/1`
receipts remain honestly offline. This transport is distinct from personal
authenticated metadata research; inherited tokens are not used. A refusal is
not an invitation to bypass an authentication guard.
Helper modules execute from source bytes, without reading or writing unbound
Python bytecode caches beside immutable inputs.

An exact, owned offline snapshot can be supplied explicitly:

```text
python -I -S -B scripts/money_client_interop.py prepare --offline-source "<owned exact 39-file snapshot>"
```

It must contain exactly the cataloged files, with no links or extra executables.
Offline provenance is recorded as offline, not as a native network fetch. Partial,
changed or linked installations are preserved and refused, never blindly replaced.
There is no workstation-specific path in the normal build.

The cold-pipeline regression calls the real Money materializer, provider,
database preparer and interop preparer against a shared 60-request response
fixture, starting with no outputs in its owned test root. Public immutable
Git metadata is recorded in `acquisition-metadata.json`; blob response data is
read and hash-verified from the separately prepared exact reference inputs.
These tests require the documented Money/database/interop preparation first.
The fixture uses the native identity/tree/blob/hash and wire-budget checks,
not replacement materializers, and opens no sockets. Its simulated request
counts and bytes are distinct from actual network acquisition evidence.

Generator readiness is checked using the accepted toolchain helper and checksum
pins. Cached generator mismatches are refusals, not instructions to download an
unpinned replacement. Existing default generated trees are checked against the
accepted golden files before regeneration; partial or modified output is not
silently healed.

Python and npm dependencies are reconstructed in new, run-specific owned
storage on every invocation. A version response from an existing interpreter or
compiler cache is **not** a trust check: the old `venv` and `producer/node_modules`
are never probed or executed. A trusted isolated base-Python probe establishes
the missing fresh paths before restoration. Python uses `venv --copies` and
isolated pip with the existing hash-required, binary-only requirements, no
dependency resolution, cache or bytecode compilation. npm uses the exact
accepted package/lock/configuration with `ci --ignore-scripts --engine-strict`,
no audit/funding requests, no executable links and a fresh owned package cache.
Only after these restores are the interpreter's package versions and compiler
version checked. Failed or partial fresh storage is preserved, never reused.

The runtime inventory binds the freshly installed Python configuration,
executables and packages, npm packages, and both selected and resolved approved
host-tool paths. It is compared before/after client execution and during offline
verification. Host-managed runtime aliases may resolve to their actual binary;
this does not relax the no-link rule for owned inputs, packages or outputs.
The accepted Gradle wrapper, dependency verification metadata and explicitly
selected dependency cache remain in use. The generated Kotlin model compiles
in each new run with `--no-build-cache --rerun-tasks`; both live and retained
evidence require the real compiler task execution, not `FROM-CACHE`,
`UP-TO-DATE`, `NO-SOURCE` or a skipped task. Historical cache-restored model
runs remain labeled as such. No shared cache is cleaned and no service, paid runner,
release, CI dispatch, publication or deployment is involved.

Every child receives a positive, command-specific environment, not an inherited
environment with a few secret names removed. It contains approved runtime/OS
directories (including Windows PowerShell), the required Java/Gradle settings
only for Java commands, and owned HOME/temp directories. Ambient PATH,
Python/JVM/Node startup options, package-manager configuration and unrelated
credentials are not forwarded. The Windows process-budget bootstrap itself
uses `-I -S -B` and cannot spawn its child before its owned Job is attached.

An elapsed-budget failure samples bounded process state immediately before the
existing owned termination, then attaches one ASCII `process_budget_state=` JSON
note to the original `BudgetExceeded`. Successful commands, nonzero exits and
other refusal branches do not collect or emit it. The note is diagnostic only;
it changes no deadline, command, environment, capture, cleanup or acceptance rule.
The Windows process is the bootstrap; on POSIX it is the direct owned command.

Version `pennilogic.process-budget-state/1` has exactly `schema`, `status`,
`process_running`, `returncode`, `capture_readers`, `capture_readers_alive`,
`setup_ms`, `wait_ms` and `elapsed_ms` when `status` is `observed`.
`process_running` is boolean; `returncode` is null iff running, otherwise an
integer from -2147483648 through 4294967295. Reader counts are integers from 0
through 2, with alive no greater than total. Monotonic elapsed values are
integers from 0 through 86400000 milliseconds, with setup plus wait equal to
elapsed. Setup includes launch, Job attachment, capture setup and bootstrap
release; wait starts afterwards. This observation bound does not extend any
execution budget. Booleans are not accepted as integer values.

Unavailable, malformed or out-of-range samples instead emit only
`{"schema":"pennilogic.process-budget-state/1","status":"unavailable"}` after the
same prefix. Notes use compact sorted JSON and are at most 384 ASCII bytes.
They contain no PID, command, path, environment, raw output, exception text or
stack content. Sampling failure preserves the original timeout and cleanup.
The canonical Infra qualifier owns transport within the matching unittest
`ERROR` block; a note is neither an exception-allowlist extension nor evidence
of a complete inventory or a passing test.

The separate `quality.py gate-self-test` command copies
`scripts/money_client_interop.py`, `scripts/tests/fixtures/money_client_interop`,
and `build/source-materialization` into its isolated working directory. Its final
clean `build` runs this gate after the planted test, lint and Money-guard refusals
are restored. Generated outputs, client tool caches and passing interop reports
are not copied; the scratch build uses the same verified preparation path.

## Evidence and limits

Each invocation creates a fresh run under `build/money-client-interop/runs/`
and fresh dependencies under `build/money-client-interop/runtimes/`.
An exclusive `run.lock` prevents concurrent writers; an abandoned lock is
refused rather than deleted or silently reused.
`current.json` is invalidated before execution. A successful report binds the API
bridge/build/source inputs, backend and generated-client classpaths, generated
trees, scratch specification, commands and their filtered environments, fresh
dependency/tool inventories, separate output streams, counts and
all run outputs. Offline `verify` checks those bindings again, rechecks actual
runtime receipts against their counts and run identity, and validates every
returned transport against the canonical corpus. A recomputed file digest alone
cannot make an empty, skipped or stale receipt qualify. Missing,
truncated, duplicate-key, wrong-version/source, stale, modified, zero-case,
unexecuted and skipped evidence fails closed. Old success cannot rescue a
failed new attempt.
The current report schema is `pennilogic.api-money-client-interop/2`; older
reports cannot stand in for the strengthened execution boundary.

Within one final `verify` call, the complete output inventory retains bounded
command records/streams, transports, scratch specification and generated manifests.
Their hashes and semantic checks use those same freshly read bytes; reuse still
checks path links, file kind and size. Other files remain fully inventoried.
Each `api_inputs` call likewise retains its own fixture binding map rather than
hashing those fixtures twice. Neither reuse crosses a call boundary: standalone
verification and pre/post-execution, report-publication and final-verification
checks remain fresh. Read-count regressions are source-only evidence, not measured
latency savings or acceptance of any outstanding runtime, timing or skipped gate.

Live and retained/offline transports compare nested values through the existing
canonical JSON helper, not Python container equality: Boolean, integer and
floating-point neighbors remain distinct. Retained source/classpath/output
binding maps and each on-disk command record use the same type-preserving
comparison; rehashing malformed byte counts or numeric execution flags cannot
make them match their correctly typed counterparts. A shared negative-control validator
requires exactly the first envelope's `total` to be replaced by the known valid
second case's Money. Both time fields, every other first-case field and all
later rows must remain exact. A date-only change, malformed/numeric Money or
unchanged Money cannot substitute for the required backend
`wire-disagreement` at `case-00000`; the real clean recovery remains mandatory.

Wire files contain **synthetic fixtures only**. Runtime stdout contains bounded
metadata receipts; errors use static causes and case IDs, never amounts or raw
payloads. Unexpected runtime output is retained only as byte counts and hashes,
not copied into logs or reports. The owned process-tree budget reuses the API's
existing process-budget helper and never stops a shared service or daemon.
Captured stdout and stderr are drained incrementally with a 4 MiB per-stream
**and aggregate** limit. Exceeding either limit terminates the owned execution;
it is not checked only after an unbounded `communicate()` allocation. An overflow
or timeout retains static failure metadata and observed-prefix byte counts and
hashes only, marks capture incomplete and cannot form a passing command receipt.
Output exactly at the limit remains valid. Capture errors fail explicitly.
The same aggregate stream-size constraint is enforced on retained command
receipts; independently bounded streams cannot combine into oversized evidence.
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
