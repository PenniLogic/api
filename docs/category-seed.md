# Indian-market category seed

## Status and ownership

This is **proposed source content** for the original
[PenniLogic/api#9](https://github.com/PenniLogic/api/issues/9), not an already
accepted seed, runtime implementation or issue-completion claim. API owns its
content; Contracts owns later publication under ADR-016 sections 2.1 and 10.7.
The canonical input is
[`data/categories/category-seed.v1.json`](../data/categories/category-seed.v1.json).
There is no previous accepted seed whose keys this file claims to reproduce.
Only `debt`, `debt.interest`, `debt.fees`, `fees` and `fees.foreign_exchange`
are reserved by the accepted ADR; the remaining names and presentation values
are this proposal.

The file contains 59 public reference categories: 16 roots and 43 children.
It contains no owner identifiers, accounts, transactions, amounts, currency,
raw messages, merchant records or personal labels. This does **not** make
user-created categories, personal overrides or an owner's use of these keys
non-sensitive. ADR-016 section 11 requires personal names to be encrypted and
owner-scoped. No user data was used to select the labels.

## Accepted inputs and limits of the research

The governing input is the accepted
[ADR-016 / T-ADR-CAT-02](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/adr/ADR-016.md),
especially sections 2.1, 7, 8, 10.7 and the
`adr-016-category-parameters` JSON block in section 14 (schema version 1).
Its Git blob is `322750d84711fd87e69496075f78f4951df5d88b`;
the complete file's SHA-256 is
`3de637cf26eeebcd58748bbc39565f303a02756de0d3b15370d02bf4e7b1b03d`.
Its public decision issue is
[PenniLogic/docs#36](https://github.com/PenniLogic/docs/issues/36),
not the historical private issue numbered 38.

The original API9 specification was inspected in full and its SHA-256
reproduced as
`de922322dc5c97904f7d497c8939e944452d93f670dd108cf8d6c4ddb52fca64`
(4,602 UTF-8 bytes). At preparation, its native blockers were public Docs36
(closed) and [API7](https://github.com/PenniLogic/api/issues/7) (open).
Source preparation does not waive that dependency.

Coverage also uses these inspected project sources at Docs commit
`47986ef6bef986a3ba9214ccf652609347d73c66`:

- [Product specification, sections 3.1-3.3](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/product/01-product-spec.md):
  clarification instead of guessing, recurring bills, refunds distinct from
  income, loan/card costs, salary and self-employment income.
- [Competitive research, section 12](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/research/02-competitive-landscape.md):
  budgeting, bill calendars and EMI tracking are product-research context,
  not measured evidence for these particular labels or a spending distribution.
- [Experience plan, section 12](https://github.com/PenniLogic/docs/blob/47986ef6bef986a3ba9214ccf652609347d73c66/product/08-experience-design-and-sdlc-plan.md):
  explicit source terminology and localization, with comprehension research
  distinct from implementation.

Groceries, eating out, rent, society maintenance, LPG, recharge, bus/metro,
auto-rickshaw, rail travel, household supplies/help, clothing, health,
education, personal care, leisure, gifts, insurance, taxes and income provide
a finite household-purpose starting set beyond the issue's six examples.
These are neutral proposed defaults, not financial recommendations, regional
or demographic profiling, a complete statistical classification, or proof of
a real-user concept study. No qualified human design, accessibility or
localization approval is claimed.

## Source interface

The complete JSON document is the input. It has no remote references and
requires no code execution, downloaded artwork, asset package or new dependency.
All listed fields are required; additional fields are rejected by the source
tests. JSON object member order is immaterial.

| Location | Type and meaning |
| --- | --- |
| `schema_version` | Integer `1`, the document shape version; booleans are not integers here. |
| `seed_version` | Integer `1`, matching the `.v1.json` filename; separate from schema evolution. |
| `market` | `"IN"`; not a currency or ledger-admission rule. |
| `default_locale` | `"en-IN"`, Indian English. |
| `locales` | `["en-IN"]`, the complete supported locale set in this version. |
| `icon_encoding` | `"unicode-scalar"`. |
| `colour_encoding` | `"srgb8"`. |
| `icons[]` | Unique `{id, code_point}` objects, sorted by `id`; `id` matches `^[a-z][a-z0-9_]{1,30}$`; `code_point` is one uppercase `U+` hexadecimal Unicode scalar identifying a symbol. |
| `colours[]` | Unique `{id, rgb}` objects, sorted by `id`; `id` uses the same registry-ID grammar; `rgb` is three integers in `[0, 255]` in red/green/blue order, in sRGB, with no alpha. |
| `categories[]` | The ordered rows defined below. |

| Category member | Type and rule |
| --- | --- |
| `key` | Stable, case-sensitive system-key string (the Contracts proposal calls it `CategorySystemKey`); ADR pattern `^[a-z][a-z0-9_]{1,30}(\.[a-z][a-z0-9_]{1,30})?$`, maximum 63 characters. Never an owner-scoped category UUID. |
| `parent_key` | `null` for a root; otherwise the existing root named by the key's first segment. |
| `nature` | `"EXPENSE"` or `"INCOME"`; a child always has its parent's nature. |
| `labels` | Exactly one trimmed, NFC label of 1-120 characters for every supported locale, keyed by the exact locale tag. |
| `icon` | One `icons[].id`, not a glyph, path, URL or arbitrary icon name. |
| `colour` | One `colours[].id`, not a hex value, CSS expression or arbitrary colour. |
| `introduced_in` | Integer seed version `1` for every v1 row. |
| `retired_in` | `null` for every v1 row. |
| `sort_order` | Non-negative integer, contiguous from zero within each sibling group (all roots are one group). |

Parents have depth zero; children have depth one. There are no deeper nodes,
cycles, missing parents or cross-nature edges. The array is a pre-order traversal:
each root in `sort_order`, immediately followed by its children in `sort_order`.
This is **display order only**, never allocation `position`, a rounding rule,
transaction chronology or a financial ranking. Both roots and children are
reference categories; a root may have no children.

Labels are source copy, never identifiers. Render an exact supported locale;
an unsupported locale falls back explicitly to `default_locale`. Do not infer
a label from a key or claim that fallback English is a Hindi/regional-language
translation. Locale additions must supply every category's label together.
The v1 labels are printable ASCII Indian English, carried in UTF-8 JSON.
Input is strict UTF-8 without a BOM, LF-terminated with a final newline,
duplicate-free JSON, and NFC-normalized. Control/format characters, unpaired
surrogates, non-finite numbers and unknown fields are invalid.

The icons are **standard Unicode characters**, with explicit scalar values
rather than names of nonexistent bundled artwork. A consumer can convert the
scalar using its platform's Unicode text support. Font appearance and glyph
availability are platform-dependent; the label remains the meaning, and an
unavailable decorative glyph must not hide it. The colours are the fixed sRGB
values of the eight named colours in the registry, not semantic success/error,
income/expense or advice signals. A consumer derives its typed vocabulary and
rendering values from these records, not from a hand-maintained copy. This is
reference presentation data, not a UI theme, icon-font installation or a
rendered contrast/WCAG guarantee. Those remain client work.

## Financial boundaries and example coverage

The seed never infers an entry's account type, payment purpose, amount or
assignment. Only already-categorisable `INCOME`/`EXPENSE` entries can receive
categories, as defined by ADR-016. The following are selection boundaries,
not executable merchant/payment-rail rules:

| Case | Source mapping or deliberate absence |
| --- | --- |
| Groceries | `food.groceries`, covering staples, produce and other grocery purchases without a diet/profile taxonomy. |
| Fuel | `transport.fuel`; vehicle fuel/charging, not household cooking gas (`utilities.cooking_gas`). |
| Rent paid | `housing.rent`; rental income is separately `income.rent`. |
| Mobile recharge/bill | `utilities.mobile_recharge`. |
| UPI payment | **No `upi` or `transfers` category.** UPI is a rail, not evidence of purpose. A categorisable purchase may use its known purpose; unknown purpose remains the ADR's null allocation. |
| EMI / loan / card interest | `debt.interest` for the stated interest expense only; `debt.fees` for stated loan/card charges. Never the whole repayment. |
| Own-account transfer, card bill payment, ATM withdrawal, loan disbursement, EMI principal or unstated EMI split | No seed key: these are outside the dimension. A projection cannot supply missing interest facts. |
| Refund | No refund-as-income key. The negative `EXPENSE` allocation reduces the purchase category under ADR-016 section 8.3; it does not become `income.*`. |
| Foreign exchange fee | Reserved `fees.foreign_exchange`; its presence does not enable E30, multi-currency posting or an exchange endpoint. |
| Banking fee | `fees.banking`; loan/card fees remain `debt.fees`. |
| Salary, self-employment and other listed income purposes | `income.*`, exclusively `INCOME`; signs and source facts are the ledger's, never inferred from this list. |
| Taxes / insurance | Only actual categorisable expense entries. No inferred tax on take-home pay, recommendation, investment/savings contribution or asset movement. |
| Unknown purpose | No `uncategorised`, `unknown` or catch-all seed row. The ADR's null allocation is not a category and cannot be granted. |

Subscriptions classify the underlying purpose (for example media versus
mobile service), not the recurrence mechanism. Short-stay accommodation is
distinct from housing rent; transport costs remain transport. Broad labels
are deliberately not merchants, diagnoses, religions or named relationships.

## Consumer handoff and evolution

Accepted Contracts commit
`ffdd507206990cb880baea150ffae2f6a7bb3043` was inspected read-only. It has no
`Category`, `CategorySystemKey`, `SystemCategoryKey`, `CategorySeedResult` or
seed/registry definition. The concurrent Contracts1 author confirmed its
uncommitted `CategorySystemKey` proposal uses the exact ADR grammar above;
`Category` separates the private owner `name` from `system_key` and currently
omits icon/colour pending a canonical provider. No `CategorySeedResult` or
localization map is yet defined. These are proposals, not accepted authority.

For that additive Contracts work, derive `CategorySystemKey` values from
`categories[].key`, icon values from `icons[].id`, and colour values from
`colours[].id`. A system `Category.system_key` joins to `key`; its default
parent, nature, label and presentation come from that row. The proposed wire
`category_id` and `parent_id` remain owner-scoped identifiers created by the
future API, not strings invented by this file. `seed_version` identifies the source version;
this document is **not** a `CategorySeedResult` response or proof of
idempotent materialisation.

After independent review and protected acceptance, Contracts may publish the
**exact accepted file bytes**, conventionally as `spec/category-seed.v1.json`,
and derive its schema enums from them. Pin the accepted API commit plus the
file's SHA-256 and byte size; fail on missing/mismatched input instead of
inventing inline defaults. No consumer materializer, provider execution,
publication or runtime loader is part of this unit.

Once accepted, never repurpose a key, change its nature/parent, or remove an
old key to rewrite history. A later seed version adds keys with its version in
`introduced_in`; retirement records the later `retired_in` while retaining
identity/old rows. New owners omit retired keys and existing materialised rows
stand, per ADR-016 section 2.1. Keep earlier version files available. Registry
IDs likewise retain their meaning; additions and locale/presentation changes
are explicit versioned, reviewed source changes. Nothing here re-parents an
owner's category, follows runtime redirects, widens a grant or alters a label
override. Runtime later-child placement is ADR-016 section 2.2's separate work.

## Source checks and remaining acceptance

Run the ordinary test discovery selector from the repository root:

```text
python -m unittest discover -s scripts/tests -p test_category_seed.py
python scripts/check_repository.py
```

The tests read the actual file. The small in-test v1 catalogue is an independent
regression oracle, not a runtime/consumer default. Positive checks cover every
key, label, nature, parent and presentation mapping. Negative mutations remove
keys and registry entries and corrupt IDs, references, hierarchy, order,
locales, versions, encodings and semantic exclusions. Only synthetic markers
are used for prohibited-field/content cases. No private-record heuristic is
claimed to prove that arbitrary future text is safe.

This does not run or satisfy API9's database schema, exact split/shortfall
rejection, merge checksums/cycle/depth, delete/archive, as-of, ledger, sharing,
API contract or seed-application tests. No amounts are added to split-error
contracts: the accepted ADR, not the historical issue wording, governs that
separate work. Full original API9, Contracts1 and native Infra22 enforcement,
real-user research, independent Core/domain/Contract/QA review, native CI,
protected publication and integration remain later gates. No migration,
endpoint, authorization, allocation, merge, Money/ledger code, build or CI
configuration is changed here.
