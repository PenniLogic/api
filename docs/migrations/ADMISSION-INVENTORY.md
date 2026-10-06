# Database admission inventory source

`database/admission-inventory.json` describes the actual V001/V002 column
semantics for the Infra-owned database admission provider. This is source
data for [PenniLogic/docs#56](https://github.com/PenniLogic/docs/issues/56) /
[PenniLogic/infra#25](https://github.com/PenniLogic/infra/issues/25), coupled to
the existing [#55](https://github.com/PenniLogic/api/issues/55) runner.
It does not activate an admission gate, accept an embedding decision, install
a provider, or authorize a database apply. Original #55 remains closed for
its delivered scope; no other original acceptance is implied.

## Exact evidence, not authority

`database/admission-inventory-source.json` names accepted API commit
`d39f4692c13413040439c5e87fed81728e0577f1`, tree
`8da895cc998e5ec43105cfb6fa41a82ea2194f8c`, and the byte lengths, SHA-256
and Git blob identifiers of seven evidence files at that commit.
The SQL, existing ledger documentation, and unchanged ADR-017/018 extractions
are the evidence; this new inventory does not pretend to exist at that commit.
Its own later publication must be bound separately by the reviewed Infra
provider. There is deliberately no accepted flag or trust entry here.

The four script entries cover every up and every reverse, even if a runner
selects only one direction. `api-ledger-v2-exact` is the provider's finite
language for these **four exact LF-normalized SQL buffers**, not permission
for arbitrary PL/pgSQL, DML, an altered constraint, a new migration, or a
renamed script. The provider owns SQL parsing, extension policy, provenance
taint propagation and admission. This data does not duplicate that policy.

V001 creates/drops a schema and creates no columns. V002 up creates six tables
with 70 columns; V002 down creates no columns and preserves the existing
nonempty-ledger refusal. Its `columns: []` describes column creation, not
permission to ignore the SQL or its history-preservation guard.
The runner-owned registry/lock is not a V001/V002 migration; its writes must
also sit behind mandatory consumer admission when that integration lands.

## Purpose and provenance

Every column names a graph node; each source node names pinned evidence paths.
The classification is an explicit inventory of these bytes, never inference
from a column name or a caller's `embedding: false`.

| Node | Actual purpose |
| --- | --- |
| `identifier` | UUID keys, opaque owners and account/transaction/correction/evidence links. This does not prove an identity or provenance provider exists. |
| `minor_units` | Only `entries.amount_minor` and `statement_snapshots.statement_balance_minor`: constrained BIGINT signed minor units. |
| `fixed_scale_rate` | Only `exchange_groups.quoted_rate_e10`: constrained positive BIGINT at scale 10, with FX unavailable under INR-only admission. |
| `currency` | CHAR(3) currency codes, including the merchant-code reservation which must stay NULL. Currency-code shape is not admission of another ledger currency. |
| `instant_or_date` | Finite dates and millisecond instants; application-supplied ledger clocks keep their existing checks. |
| `archive_flag` | Account archive boolean. |
| `bounded_count` | Currency exponent and transaction entry count, not balances or vectors. |
| `reference_domain` | Closed scalar enum domains and the `rate_feed` reference reservation. The latter is forced NULL until its real reference provider exists. User descriptions are never placed in this class. |
| `transaction_description` | User-derived financial/account descriptions, masked account references and external transaction references. The provider's TRANSACTION source category includes these financial-description origins; none is a global non-user reference. |
| `merchant_description` | User-derived merchant display/raw descriptions and the inactive descriptive merchant-amount reservation. |
| `memo` | User notes and immutable correction reason notes. |

The ENCRYPT and HASH nodes retain their user-derived parent, never cleanse
provenance. HASH describes the separate blind-index reservation, not a
raw-message hash or a working index provider. These transformations describe
the purpose of the existing reserved shape, **not running crypto**.
All thirteen BYTEA columns and the paired merchant currency must remain NULL
under the existing unavailable-provider constraints. Arbitrary ciphertext,
an encoded vector, an encrypted embedding, or a static-prototype label does
not gain permission from this inventory.

`transactions.merchant_amount_minor` is not `entries.amount_minor`: it is the
NULL-only descriptive merchant-figure reservation named by ADR-017, not an
authoritative numeric money field, conversion or computation. Its MERCHANT
origin is retained through the inactive ENCRYPT node. Activating it still
requires the accepted crypto and merchant provenance providers and a separate
reviewed migration. This inventory does not loosen any money representation,
append-only, RLS, no-admitting-policy or recovery invariant.

No column is an embedding, assistant-conversation store, inferred vector,
vector extension, static-prototype store or generic durable feature container.
That conclusion is limited to the exact source above. A future column or
source change requires a newly reviewed inventory and real provider admission.

## Focused evidence

`python -m unittest discover -s scripts/tests -p test_database_inventory.py`
checks the closed source data, exact evidence bytes, complete script pairing,
explicit provenance, the accepted ADR column inventory and reserved-field
classifications. It is not an implementation of the Infra admission policy.

`./gradlew integrationTest --tests com.pennilogic.migration.DatabaseInventoryPostgresTest`
applies the unchanged migrations through the existing runner to an owned
disposable PostgreSQL instance and compares **every** resulting stored column
and type with this inventory. It also checks empty-database reversal and
reapply without modifying the SQL. Original ledger and recovery suites remain
separate compatibility evidence, not embedding-provider qualification.

No gate or native/production acceptance is claimed by these tests. Reviewed
policy publication, Infra trust/distribution, mandatory API integration and
independent current-head review remain required.
