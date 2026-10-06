import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "src/main/resources/db/migrations"


def document(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate inventory key")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


class DatabaseInventoryTest(unittest.TestCase):
    def setUp(self):
        self.source = document(ROOT / "database/admission-inventory-source.json")
        self.inventory = document(ROOT / "database/admission-inventory.json")
        self.nodes = {node["id"]: node for node in self.inventory["nodes"]}
        self.columns = {
            column["name"]: column
            for script in self.inventory["scripts"] for column in script["columns"]
        }

    def test_source_is_exact_accepted_api_evidence_not_an_acceptance_flag(self):
        self.assertEqual(
            {"schema", "repository_id", "commit", "tree", "inventory", "files"},
            set(self.source),
        )
        self.assertEqual("pennilogic.api-database-admission-source/1", self.source["schema"])
        self.assertEqual(1394134582, self.source["repository_id"])
        self.assertEqual("d39f4692c13413040439c5e87fed81728e0577f1", self.source["commit"])
        self.assertEqual("8da895cc998e5ec43105cfb6fa41a82ea2194f8c", self.source["tree"])
        self.assertEqual("database/admission-inventory.json", self.source["inventory"])
        paths = [record["path"] for record in self.source["files"]]
        self.assertEqual(7, len(paths))
        self.assertEqual(len(paths), len(set(paths)))
        for record in self.source["files"]:
            with self.subTest(path=record["path"]):
                self.assertEqual({"path", "bytes", "sha256", "git_blob"}, set(record))
                content = (ROOT / record["path"]).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(record["bytes"], len(content))
                self.assertEqual(record["sha256"], hashlib.sha256(content).hexdigest())
                blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
                self.assertEqual(record["git_blob"], hashlib.sha1(blob).hexdigest())

    def test_all_four_directions_bind_complete_unchanged_sql(self):
        self.assertEqual({"schema", "nodes", "scripts"}, set(self.inventory))
        self.assertEqual("pennilogic.database-admission.inventory/1", self.inventory["schema"])
        scripts = self.inventory["scripts"]
        self.assertEqual(4, len(scripts))
        actual = {}
        for script in scripts:
            self.assertEqual({"migration", "direction", "sha256", "grammar", "columns"}, set(script))
            self.assertEqual("api-ledger-v2-exact", script["grammar"])
            name = script["migration"] + "." + script["direction"] + ".sql"
            self.assertNotIn(name, actual)
            actual[name] = script
            content = (MIGRATIONS / name).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(content).hexdigest(), script["sha256"])
            if name != "V002__create_ledger.up.sql":
                self.assertEqual([], script["columns"])
        self.assertEqual({path.name for path in MIGRATIONS.iterdir()}, set(actual))
        self.assertEqual(
            {"V001__create_pennilogic_schema", "V002__create_ledger"},
            {script["migration"] for script in scripts},
        )
        for migration in {script["migration"] for script in scripts}:
            self.assertEqual(
                {"up", "down"}, {script["direction"] for script in scripts if script["migration"] == migration},
            )

    def test_all_provenance_is_explicit_and_tied_to_pinned_evidence(self):
        self.assertEqual(len(self.nodes), len(self.inventory["nodes"]))
        self.assertEqual(16, len(self.nodes))
        evidence = {record["path"] for record in self.source["files"]}
        referenced = set()
        for name, node in self.nodes.items():
            self.assertRegex(name, r"^[a-z_]+$")
            if node["kind"] == "SOURCE":
                self.assertEqual({"id", "kind", "source_kind", "evidence"}, set(node))
                self.assertTrue(node["evidence"])
                self.assertEqual(len(node["evidence"]), len(set(node["evidence"])))
                self.assertTrue(set(node["evidence"]) <= evidence)
                self.assertIn(node["source_kind"], {
                    "IDENTIFIER", "INTEGER_MINOR_UNITS", "FIXED_SCALE_INTEGER", "CURRENCY_CODE",
                    "DATE_TIME", "BOOLEAN", "NUMERIC_MEASUREMENT", "NON_USER_REFERENCE",
                    "TRANSACTION", "MERCHANT", "MEMO",
                })
            else:
                self.assertEqual({"id", "kind", "inputs"}, set(node))
                self.assertIn(node["kind"], {"ENCRYPT", "HASH"})
                self.assertEqual(1, len(node["inputs"]))
                parent = self.nodes[node["inputs"][0]]
                self.assertEqual("SOURCE", parent["kind"])
                self.assertIn(parent["source_kind"], {"TRANSACTION", "MERCHANT", "MEMO"})
                referenced.update(node["inputs"])
        count = 0
        for script in self.inventory["scripts"]:
            for column in script["columns"]:
                count += 1
                self.assertEqual({"name", "sql_type", "provenance"}, set(column))
                self.assertRegex(column["name"], r"^pennilogic\.[a-z_][a-z_0-9]*\.[a-z_][a-z_0-9]*$")
                self.assertIn(column["provenance"], self.nodes)
                referenced.add(column["provenance"])
        self.assertEqual(70, count)
        self.assertEqual(count, len(self.columns))
        self.assertEqual(set(self.nodes), referenced)

    def test_column_inventory_matches_the_accepted_ledger_and_crypto_contracts(self):
        ledger = document(ROOT / "src/test/resources/ledger/adr-017-parameters.json")
        crypto = document(ROOT / "src/test/resources/ledger/adr-018-inventory.json")
        tables = {table["table"]: table for table in crypto["tables"]}
        account, transaction = tables["accounts"], tables["transactions"]
        absent = {"source", "source_confidence", "needs_review", "category_id", "source_event_id",
                  "dedupe_key", "dedupe_key_version"}
        expected = {
            "accounts": set(account["constrained"] + account["encrypted"] + account["blind_indexed"])
                        | {"system_role", "mask_bidx"},
            "transactions": (set(transaction["constrained"]) - absent)
                            | set(transaction["encrypted"] + transaction["blind_indexed"])
                            | set(ledger["append_only"]["transactions_immutable_columns"])
                            | set(ledger["append_only"]["transactions_write_once_columns"])
                            | set(ledger["exchange"]["descriptive_merchant_amount"]["columns"])
                            | {"merchant_bidx", "external_ref_bidx"},
            "entries": set(ledger["entry"]["columns"]),
            "ledger_currencies": set(ledger["ledger_currencies"]["columns"]),
            "statement_snapshots": set(ledger["statement_snapshot"]["columns"]),
            "exchange_groups": set(ledger["exchange"]["columns"]),
        }
        qualified = {f"pennilogic.{table}.{column}" for table, columns in expected.items() for column in columns}
        self.assertEqual(qualified, set(self.columns))

    def test_numeric_money_never_becomes_a_ciphertext_or_vector_classification(self):
        numeric = {name: column for name, column in self.columns.items()
                   if column["provenance"] in {"minor_units", "fixed_scale_rate"}}
        self.assertEqual(
            {"pennilogic.entries.amount_minor", "pennilogic.statement_snapshots.statement_balance_minor",
             "pennilogic.exchange_groups.quoted_rate_e10"},
            set(numeric),
        )
        self.assertEqual({"bigint"}, {column["sql_type"] for column in numeric.values()})
        descriptive = self.columns["pennilogic.transactions.merchant_amount_minor"]
        self.assertEqual("bytea", descriptive["sql_type"])
        self.assertEqual("merchant_ciphertext", descriptive["provenance"])
        self.assertEqual("MERCHANT", self.nodes["merchant_description"]["source_kind"])

    def test_every_reserved_byte_column_keeps_its_user_derived_provenance(self):
        expected = {
            "accounts.name": "transaction_ciphertext",
            "accounts.institution": "transaction_ciphertext",
            "accounts.mask": "transaction_ciphertext",
            "accounts.mask_bidx": "transaction_blind_index",
            "transactions.reason_note": "memo_ciphertext",
            "transactions.description": "transaction_ciphertext",
            "transactions.merchant_display": "merchant_ciphertext",
            "transactions.merchant_bidx": "merchant_blind_index",
            "transactions.merchant_raw": "merchant_ciphertext",
            "transactions.merchant_amount_minor": "merchant_ciphertext",
            "transactions.note": "memo_ciphertext",
            "transactions.external_ref": "transaction_ciphertext",
            "transactions.external_ref_bidx": "transaction_blind_index",
        }
        actual = {name.removeprefix("pennilogic."): column["provenance"]
                  for name, column in self.columns.items() if column["sql_type"] == "bytea"}
        self.assertEqual(expected, actual)
        self.assertNotIn("EMBED", {node["kind"] for node in self.nodes.values()})
        self.assertNotIn("ASSISTANT_CONVERSATION", {node.get("source_kind") for node in self.nodes.values()})


if __name__ == "__main__":
    unittest.main()
