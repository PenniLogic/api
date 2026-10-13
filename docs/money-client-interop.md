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

The temporary **DIAGNOSTIC COUNTERFACTUAL** in the approved-shell regression
leaves the default `PSModulePath` untouched and keeps the original uncaptured
`Write-Output`, 10-second call, return/output assertions and legacy E/S-or-U/X prefixes.
It exclusively creates `build/diagnostics/api106-approved-shell-module-lookup.jsonl`
outside JUnit selection. The child appends only bounded, flushed ASCII NDJSON:
at most 8192 bytes, five records and 6144 bytes per LF-terminated record, with
integer `v=2` throughout. No raw paths, module names, commands, messages,
credentials or ANSI are exported. Historical V1 capsules retain their exact
four-record contract and contain no negative-hook availability or observation.
Versions must not be mixed or reinterpreted. This V2 producer requires the
corresponding reviewed canonical collector; its generated adoption is separate.

The existing boundary file gains one optional literal `C` after `ES[01]{3}` and
before optional `X`. Only the first matching `Write-Output` pre-lookup callback
can append it, immediately after its existing pre-record writer invocation
returns. The callback snapshots an eligible boundary path before that invocation;
the path is armed only after the redirect flags are appended, around the original
bare command statement, and disarmed before `X`. The existing first-target latch
prevents a second or nested lookup from emitting another `C`. No `C` is emitted
under `EU`, before the redirect flags, or during finalization.

`C` attests only that this writer invocation returned, including a return through
its existing handled failure path. It does not certify a successful JSON write,
native delegate return, lookup success, entry into the cmdlet, output or pipeline
completion, cleanup, or cause. Failure to observe `C` is not proof of non-return.
The marker is untimestamped and collected after the owning command: it does not
prove the writer returned before the ten-second deadline. The earlier `pre.ms`
sample precedes the writer invocation and cannot supply that missing ordering.
Marker append errors use the existing callback-refusal handling; no raw error or
path is exported. The V2 JSON records and five-record limit are unchanged.
The paired canonical boundary reader retains every old valid prefix and adds
only `ES[01]{3}CX?`, with a seven-byte valid maximum and eight-byte overflow probe.
It reports `pre_record_writer_return` as `observed` for valid `C`, otherwise
`not-observed`; repeated or out-of-order `C` remains invalid. The earlier
producer-only increment left the generated reader unchanged. This composed source
adoption includes the canonical workflow/checker and 2254-byte inline boundary
reader from protected-merged [PenniLogic/infra#87](https://github.com/PenniLogic/infra/pull/87),
commit `0f5f7b7ce4baa8206add295ce0adee03292a5b20`. Old records are not
reinterpreted; native success and acceptance remain unestablished.

The V2 record order is `roots`, `observer`, optional `pre`, optional `not-found`,
optional `post`, each at most once. `roots/observed` has `value` (`present` or
`absent`) and `roots`:
up to 32 entries with consecutive `i`, `kind` (`pshome`, `unknown`, `empty`),
`sha256` and nullable `duplicate_of`. Hashes are lowercase SHA256 of each exact
strict-UTF-8 component, without normalization; duplicates refer to the earliest
identical ordinal string. Empty/trailing components remain in order. An empty
value has one empty entry; absence has no entries. The environment string is
capped at 32768 UTF-16 units and each component at 2048. `pshome` means a lexical
case/slash/trailing-separator match to this child's `PSHOME\Modules`, not physical
path identity. Refusal replaces those fields with `code`: `input-limit`,
`encoding` or `snapshot`.

`observer/armed` registers only the public WinPS5.1
`PreCommandLookupAction`/`PostCommandLookupAction` APIs, without replacing
existing hooks or initiating lookup. Their availability is documented in
`Microsoft.PowerShell.5.1.ReferenceAssemblies`; the returned `CommandName`,
`Command`, `CommandType`, `Module` and `Path` getters are read through their
public reflection metadata, not alias resolution or command enumeration.
Unavailable codes are `roots-refused`, `existing-hooks`, `unsupported-api`;
installation failure is `observer/refused` with `code=install`.

Every V2 `observer` also has an exact `not_found` object. The optional public
WinPS5.1 `CommandNotFoundAction` hook is admitted independently: only a supported,
currently null slot is assigned, and identity readback is required before its
separate armed flag is set. Its object is `{"status":"armed"}`, or has
`status=unavailable` with `code=existing-hook|unsupported-api`, or
`status=refused` with `code=install`. The base Pre/Post observer can remain armed
in all those cases. If and only if the base observer is not armed, the object is
`{"status":"unavailable","code":"observer-unavailable"}`. A partially installed
negative handler remains inert; an existing handler is never overwritten.

Only the first matching actual lookup is sampled. `pre/observed` adds nullable
`ms`. With both observers armed and an earlier unambiguous `pre`, the first
matching negative callback adds only `v`, `phase=not-found`, `status=observed`
and nullable `ms`. This record is nonterminal: it establishes callback entry
and recording, not callback return, lookup completion, a particular subsequent
command result, test success or cleanup. An absent negative record remains
unknown, not evidence of successful resolution or a cache hit.
`post/observed` adds nullable `ms`, `lookup_ms`, actual `command_type`
(1,2,4,8,16,32,64,128,256, or null for no returned command), `module_kind` and
`module_sha256`. Module kinds are `no-command`, `none`, `unknown`, `pshome`,
`other`; only the latter two have a hash of the observed module path, capped at
2048 UTF-16 units. `pshome` is lexical absolute containment below `PSHOME\Modules`,
not module-byte or symlink attestation. Alias metadata describes its defining
module, never its resolved target. Times are monotonic diagnostic-relative
milliseconds in 0..600000 or null, not a changed deadline or method duration.
The lookup interval includes callback/recording overhead. Nested target lookup
or a repeated matching negative callback invalidates pairing, emits one
`post/unavailable` with `code=nested-target`, and emits no inner provenance or
second negative record. A negative callback without a recorded pre emits only
the existing `post/unavailable` with `code=post-without-pre`.
All three boundary samples use the existing `Stopwatch.ElapsedMilliseconds`
without further rounding or clamping. An out-of-range sample is null; a negative
sample below a valid pre sample is null, and a post sample below either retained
valid predecessor is null. All nonnull pre/negative/post samples are therefore
nondecreasing, even across a null sample. When pre and post are both nonnull,
`lookup_ms` is exactly `post.ms - pre.ms`; otherwise it is null. A null negative
sample does not change that rule, and there is no negative elapsed field.
Throwing property, clock or formatting access uses the existing callback
refusal, not a fabricated sample or negative edge.

Other `post` records have only `code`: unavailable codes are
`observer-unavailable`, `no-pre`, `no-post`, `nested-target`, `post-without-pre`;
refusal codes are `command-type`, `module-path-limit`, `callback`. All records
also have `v`, `phase`, `status`; unknown fields are invalid. Writer/open failures
emit only the fixed `api106_module_lookup_capture`/`refused` stderr event, never
exception text or a success fallback. After a terminal post or capture failure,
callbacks emit no further records. Normal finalization after pre/negative
without post emits `post/unavailable` with `code=no-post`. Successful finalization
removes all three delegates only when each slot still refers to the fixture's
own delegate; later replacements are never removed. Existing stream/hash cleanup
is unchanged, and process termination can leave a strict prefix.

The canonical collector must read at most 8193 bytes and validate the entire
capsule before emitting its safe projection. Reparse/nonregular files, I/O errors,
oversize, incomplete lines, non-ASCII/BOM/ANSI, duplicate JSON keys, wrong types,
unknown fields/enums, invalid hashes/duplicate indices and impossible phase
relationships are explicit refusals, without echoing offending data. A valid
prefix is partial/unknown: only a final post completes a capsule, not a negative
edge alone. Absent callbacks do not prove a cached lookup. The earlier producer-only
increment retained the 2105-byte boundary reader; this composed adoption uses the
2254-byte inline reader described above. Neither roots, callbacks, `C`, X nor a
green instrumented test establishes cause, delivery, no-error behavior,
cleanup, a default-launch repair or merge acceptance.

### Dormant, one-use observer comparison

The original owning test contains an inactive `APPROVED_SHELL_PAIR_ENCLOSURE`
seam, initialized to `None`. No environment variable, ordinary command or
generated workflow activates it. With that value unchanged, the original composed
PowerShell payload, provider call and return/output assertions are unchanged.
The pair protocol and its synthetic tests do not authorize a real execution.

The API-owned `scripts/api106_observer_pair.py` source prepares an explicit
Windows/test-only `python scripts/quality.py test --api106-observer-pair` route.
This is default-off diagnostic source, not execution authorization or an accepted
native enclosure. Ordinary `quality.py test` still runs its original unittest
command. The generated qualifier does not forward the option.

The prepared supervisor creates the actual discovery interpreter suspended,
retains its creation process/thread handles, attaches the existing Windows Job,
rechecks the API source/interpreter bindings and only then resumes its thread.
There is no extra live bootstrap interpreter, breakaway or attachment fallback.
The child keeps the ordinary interpreter startup flags, cwd, environment and
standard streams. A fixed byte-bound `-c` bootstrap replaces only the `-m` entry
and restores its absolute cwd import entry. It discovers the full ordinary
suite, locates exactly one original test object and its existing module, requires
the `None` seam, and installs there without reloading, filtering or reordering
the suite. The private fixed-size control pipe, unnamed eight-byte deadline
mapping and its unnamed mutex are separate from test logs and stdin. Only those three private handles
and duplicates of the original three standard handles enter the suspended
child's explicit handle whitelist. Source/tool bindings are API-specific per-use continuity checks of the
selected CPython 3.14 executable and fixed API sources, not Infra Git/Gitleaks
admission, a signed runtime attestation or native qualification.

At that test's `startTest`, the child samples one monotonic epoch and atomically
publishes its exact bits into the initially zero shared word, before target
admission or its first control wait. The successful zero-to-epoch exchange is
the publication point; a delayed exchange retains the earlier sampled time,
never fresh credit. The parent already owns and monitors the mapping. It does
not need the `ARM` pipe frame to discover or enforce that epoch; any later `ARM`
must match it exactly. Scheduling before publication is not a demonstrated
native bound, and no target admission/control wait precedes publication.
The same epoch is yielded by the enclosure and used for all 28/48-second protocol
checks; acknowledgment latency never restarts it. Normal `stopTest` accounting
follows the original per-test fixture cleanup and result callbacks. Initial
discovery and class/module fixtures can precede this per-test window; they are not prewarmed,
reordered or claimed inside it. Each arm still reserves a complete ten-second
work plus existing shared ten-second cleanup allowance. Neither inner limit is
increased or shortened.

All shared-word reads and conditional writes hold the same inherited mutex.
`CreateMutexW`, zero-time `WaitForSingleObject` and `ReleaseMutex` replace the
unavailable current-host `InterlockedCompareExchange64` export; no intrinsic,
undocumented entrypoint or raw unsynchronized access is substituted. The
documented interlocked API-set contract also resolves to KernelBase on that
observed host, whose export table lacks the symbol; it is not a usable alias.
The [documented wait/release barriers](https://learn.microsoft.com/en-us/windows/win32/sync/synchronization-and-multiprocessor-issues)
protect the complete word operation. The
[zero-time acquisition](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)
never enters a blocking wait: contention, abandonment
or another acquisition failure refuses the control path rather than hiding
the epoch behind a lock wait, retrying or granting another allowance. Thus even
brief benign contention can abort this diagnostic; it is not evidence about
the required shell or the original stall. A granted abandoned mutex is released
once without consuming its word. A failed or interrupted release remains
uncertain, with no further word access or release retry. The sampled epoch is
retained locally even when publication or release fails, so cleanup cannot
resample it. Successful publication is visible after the protected write and
release, still before admission or any blocking control wait.

The independent parent observes the protected atomic word before, during and after
overlapped control/report I/O, with pending waits sliced at at most 25 milliseconds.
That observation interval is not another allowance or a native timing guarantee.
A live child publishing an epoch but never delivering `ARM` still has the same
independent parent deadline owner. While the word is zero, preceding suite work
does not acquire a global 60-second limit. The case must finalize by 48 seconds; otherwise its
outer Job is terminated and termination, member drain, reaping, control/report
I/O cancellation and release use the remainder of the same 60-second deadline,
including the 12-second reserve. Unconfirmed cleanup remains a failure, never
success-shaped evidence; unconfirmed overlapped I/O does not attest its later
interpreter-finalization time or a bounded parent exit. API-owned mock controls
cover this state machine but do not prove real Windows timing, cancellation,
nested-Job or filesystem behavior.

The child publishes a permanent closed word only after receiving the final
`DONE` acknowledgment within the original work deadline. The parent independently
observes that closure before disarming case enforcement; waiting for closure
also uses that deadline. The word is never reset: premature close, epoch change,
erasure or rearm is refused. Subsequent ordinary suite work keeps its existing
accounting without an expired case deadline. This mapping, mutex, atomic
publication and polling are additional observer perturbations, not an established causal fix.

One parent ownership guard spans acquisition, attachment, protocol construction,
supervision and finalization; there is no pre-entry cleanup handoff. Every
acquired temporary inheritable handle receives one release attempt even when an
earlier release raises a declared cleanup error, `KeyboardInterrupt` or
`SystemExit`. Ambiguous closes are not retried. The original acquisition/creation
failure stays primary, secondary release failures remain explicit, and release
uncertainty prevents a true cleanup claim. The shared Windows ownership helper
is unchanged. The mapping view and both mapping/mutex handles are independently
released, even after a declared cleanup failure or cancellation; partial
acquisition remains owned. These source corrections do not supersede the frozen
v1/v2 reviews or constitute independent acceptance of v3. A resource-free DLL
binding check verifies export resolution only, not mutex behavior, physical
deadline enforcement or any hosted activation.

The fixed order is required, then `auxiliary-no-lookup-delegates`, each at most
once. The required arm receives the original command object and calls the same
provider with the same cwd, capture and assertions. The auxiliary is a copy:
only its diagnostic file bindings and lookup-observer registration/reporting
change. Root sampling and its writer remain; Pre/Post delegate construction
remains but neither is installed, so `Armed` stays false and the existing
negative-handler guard prevents its installation. The auxiliary omits the
corresponding observer/terminal records rather than fabricating `armed`,
unavailable-reason or callback evidence. Its lookup file is intentionally a
roots-only V2 prefix, not a completed required capsule. The original bare
`Write-Output`, shell flags, module environment, stdin/capture contract, E/S/X
operations and stream/hash disposal are unchanged. No new marker is added.

The pair exclusively creates `api106-approved-shell-observer-pair.jsonl` in the
existing diagnostic directory. This retained claim prevents another pair at
that location even after failure or interpreter restart; nothing is deleted or
retried. The auxiliary uses separate, exclusively created
`api106-approved-shell-auxiliary-boundary.bin` and
`api106-approved-shell-auxiliary-module-lookup.jsonl`, never the required files.
The ASCII report has event `api106_approved_shell_observer_pair`, integer `v=1`,
`diagnostic_only=true`, at most six records, 1024 bytes per record and 4096 total:
`begin`, then each admitted arm's `started`/`arm`, then `complete` if both
outcomes can be recorded. An unexecuted arm has an explicit reason instead of a
process outcome. Partial reports remain partial; `complete` describes arm
accounting, **not** the external enclosure's later cleanup receipt.

Arm records contain role, outcome, elapsed milliseconds, nullable return code
and nullable UTF-8 text byte count/hash of returned stdout, not raw output,
exception text, paths or credentials. Both arms apply the original assertions.
A required error/failure is re-raised unchanged after the auxiliary, including
its traceback; an auxiliary success never replaces it. A required process/setup
or cleanup error prevents the auxiliary when safe ownership completion is
unknown. Secondary reporting/enclosure failure is chained without erasing the
required failure. An auxiliary failure is recorded separately and cannot turn
the required arm's own pass into a claimed required failure, or into pair
acceptance. An enclosure cannot suppress an incomplete protocol into success.
Unexpected interruption leaves an incomplete report and propagates.

The discovery process and its descendants acquire an additional outer Job;
the unchanged per-arm Jobs are nested. That is an additional observer confounder.
Normal case completion requires the known live discovery process, identified
by its creation handle, to be the sole outer member: it is not active-zero.
The outer Job remains owned until suite exit without an expired case cleanup
deadline. If the parent must terminate discovery, the rest of the inventory is
incomplete; it cannot preserve a killed interpreter's live exception or fabricate
replacement test results. On the normal path, the original required exception
and unittest objects/order/result accounting are preserved.

The parent exclusively creates the private
`api106-approved-shell-enclosure.jsonl` receipt, bounded to eight ASCII records,
1024 bytes each and 8192 total, using deadline-bound overlapped writes.
Its case-closed record is a pre-acknowledgment phase observation that one known
discovery member remains, not proof of the later shared-word closure; its
cleanup record cannot attest its own subsequent file-handle close. A partial
receipt is partial, not proof of cleanup or full inventory.

Hosted activation is still unwired. After separate canonical source review, the
qualifier/workflow must explicitly forward the option and an independent
`always()` collector must retain and safely project the pair report, auxiliary
boundary/roots-only capsule and enclosure receipt even when discovery fails or
is terminated. Today's collector only handles the required V2 capsule and its
inline boundary reader. No generated consumer or shared process-ownership helper
is changed by this preparation. The ordinary source/protocol controls, including
the new supervisor mocks, never run either real arm. Independent review and a
separate one-use execution decision remain required.

Required-fail/auxiliary-pass would support observer participation only in that
paired occurrence, not identify a callback or explain the pre-observer failure.
Two instances of the same stall would show these diagnostic delegates are not
necessary for that reproduced failure. Pass/pass, inverse results, different
failures or unmatched preceding state are inconclusive. Fixed order, filesystem
and module caches, scheduling and separate shell startups remain confounders;
there is no retry, prewarm or successful-sidecase acceptance. Current and
historical native failures, complete Windows inventory, API-suite 300-second
timing, per-test parity and full cleanup obligations remain open.

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

After the existing termination and capture snapshot, the elapsed-failure path
also projects only the original `BudgetExceeded.streams` metadata into an
additive `process_budget_capture_state=` note. It precedes the existing optional
Job note and final v1 note, preserving Job-v1 adjacency. There are no new process,
member or clock queries, output reads, rehashes, waits or changes to the exception.
The adopted canonical qualifier transports this bounded capture note within the
matching unittest `ERROR` block, preserving refusal and incomplete outcomes.

Version `pennilogic.process-budget-capture-state/1` uses recursively sorted compact
ASCII JSON, at most 512 bytes including the prefix. An observed envelope has
exactly `schema`, `status: "observed"`, `scope: "snapshot"`, `complete: false`,
`mode: "merged"` or `"separate"`, and `stdout`/`stderr` objects each containing
exactly `bytes` and `sha256`. Counts are non-boolean integers from 0 through
18446744073709551615; hashes are exactly 64 lowercase hexadecimal characters.
A zero count requires the SHA256 of empty bytes. In merged mode, stdout describes
the merged raw capture and the retained stderr slot must be zero with that empty
hash; this unused slot does **not** prove that the shell emitted no stderr.
Separate mode describes each retained stream independently. These are existing
raw-byte counts/hashes, not hashes of replacement-decoded text or outer unittest
captures. No arbitrary output, command, private path, raw note or environment is
projected.

Missing or malformed metadata, an invalid mode, inconsistent merged stderr,
or a projection/encoding refusal instead emits only
`{"schema":"pennilogic.process-budget-capture-state/1","status":"unavailable"}`
after the same prefix, with no invented counts or hashes. The original failure
and existing notes remain intact. `complete: false` means completeness is not
established, not that truncation was positively observed: this is only the
already-retained snapshot after the existing cleanup attempt. It does not prove
EOF, all child output, process exit, a wait cause or a timeout repair. The
original v1 schema/384-byte bound and Job schema/512-byte bound are unchanged.

On Windows, the same elapsed-failure boundary also queries only the already-owned
Job. It attaches `process_budget_job_state=` immediately before the unchanged
final v1 note. Version `pennilogic.process-budget-job-state/1` has exactly
`schema`, `accounting` and `members`, encoded as recursively sorted compact ASCII
JSON, at most 512 bytes including the prefix. The adopted canonical Infra
qualifier transports the optional Job fields from the matching unittest `ERROR`
block, requiring the Job note immediately before the unchanged final v1 note.
Malformed or misplaced Job notes remain unavailable, not observed zeroes.
The existing v1 schema and 384-byte bound, original `BudgetExceeded`,
refusal and incomplete-outcome treatment remain unchanged. This source adoption
adds no runtime evidence and does not diagnose or repair the original timeout.

Accounting uses one query: an `observed` block contains `status`,
`total_assigned` (the Job's cumulative process count) and `active`, with unsigned
32-bit integers and active no greater than total. Membership uses one fixed
16-entry query without sizing, retry or further enumeration. An `observed` or
`partial` block contains `status`, `listed`, `bootstrap`,
`system_approved_shell`, `other` and `unclassified`. Counts are integers from 0
through 16, bootstrap is at most one, and the four roles sum to listed.
Booleans are not integers. Unclassified entries, an incomplete list or a
disagreement with actually observed active accounting make membership partial;
cumulative total is never compared with listed. An unavailable accounting
component supplies no invented active count.

Unavailable or unsupported components contain only their corresponding
`status`; an oversized membership component contains only
`{"status":"oversized"}`. Missing evidence is not observed zeroes. Expected
query or encoding refusals preserve the original failure and cleanup, with no
raw exception text. No identities, handles, creation times, paths or their
hashes, argv, environment or command output are emitted.

Each returned PID is opened at most once with limited-query access. Membership
in this exact Job is checked before metadata and again afterwards; observer-owned
handles are closed even on refusal, never the borrowed Job/bootstrap handles.
Bootstrap identity uses the retained process object, not its PID or image name.
Its [`CompareObjectHandles` function](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-compareobjecthandles)
is resolved lazily from the fixed system `KernelBase.dll` provider with
`LOAD_LIBRARY_SEARCH_SYSTEM32`. Job ownership, accounting, membership and the
other handle/image APIs remain on `kernel32`; no provider is loaded for this
comparison until a usable nonempty member list needs classification. A missing
comparison library leaves membership unavailable; a missing export leaves it
unsupported. Neither replaces the original timeout or changes cleanup. Regression
fakes use separate strict DLL namespaces rather than assuming one DLL exports
every function.
An approved-shell role requires a positive image-path match to the already
resolved requested shell and its already-established system location. Unknown
or ambiguous identities remain unclassified, without filesystem discovery,
image-buffer resizing or runtime substitution.

The two queries and per-handle checks are not an atomic census. PID reuse within
the Job or exit after opening can prevent a handle classification from describing
the earlier accounting instant. Counts do not establish per-role liveness,
absence throughout execution, binary authenticity, lifecycle history or a wait
cause. Fixed query counts and buffers bound work, not operating-system latency;
no extra wait, prewarm, retry or execution-budget extension is added. Portable
fake-Win32 and mocked-run regressions exercise this contract, not live kernel
behavior, a timeout repair, native acceptance or the five-minute requirement.

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
