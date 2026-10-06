"""Validate the actual v1 reference data; this is not a runtime seed provider."""

import copy
import json
from pathlib import Path
import re
import unicodedata
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "data" / "categories" / "category-seed.v1.json"
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,30}(\.[a-z][a-z0-9_]{1,30})?$")
ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,30}")
TOP_FIELDS = {
    "schema_version", "seed_version", "market", "default_locale", "locales",
    "icon_encoding", "colour_encoding", "icons", "colours", "categories",
}
CATEGORY_FIELDS = {
    "key", "parent_key", "nature", "labels", "icon", "colour",
    "introduced_in", "retired_in", "sort_order",
}

# A source regression oracle, deliberately independent of the JSON being validated.
GROUPS = (
    ("food", "Food and drink", "green", (
        ("groceries", "Groceries"),
        ("eating_out", "Eating out and takeaway"),
    )),
    ("housing", "Housing", "brown", (
        ("rent", "Rent"),
        ("maintenance", "Repairs and society maintenance"),
    )),
    ("utilities", "Utilities and communication", "teal", (
        ("electricity", "Electricity"),
        ("water", "Water"),
        ("cooking_gas", "LPG and cooking gas"),
        ("mobile_recharge", "Mobile recharge and bills"),
        ("internet", "Broadband and internet"),
    )),
    ("transport", "Transport", "blue", (
        ("fuel", "Fuel and vehicle charging"),
        ("public_transport", "Bus and metro"),
        ("taxi_auto", "Taxi and auto-rickshaw"),
        ("rail_travel", "Rail travel"),
        ("air_travel", "Air travel"),
        ("vehicle_maintenance", "Vehicle maintenance"),
        ("parking_tolls", "Parking and tolls"),
    )),
    ("household", "Household", "brown", (
        ("supplies", "Household supplies"),
        ("appliances", "Furniture and appliances"),
        ("domestic_help", "Domestic help"),
    )),
    ("shopping", "Clothing and personal goods", "purple", (
        ("clothing", "Clothing"),
        ("footwear", "Footwear"),
        ("electronics", "Personal electronics"),
    )),
    ("health", "Health", "red", (
        ("medicines", "Medicines"),
        ("care", "Consultations and treatment"),
    )),
    ("education", "Education", "blue", (
        ("fees", "School and tuition fees"),
        ("books_supplies", "Books and study supplies"),
    )),
    ("personal", "Personal care", "purple", (
        ("care", "Grooming and personal care"),
        ("fitness", "Fitness"),
    )),
    ("leisure", "Leisure and stays", "orange", (
        ("entertainment", "Entertainment and outings"),
        ("subscriptions", "Media subscriptions"),
        ("accommodation", "Short-stay accommodation"),
    )),
    ("giving", "Gifts and donations", "orange", (
        ("gifts", "Gifts"),
        ("donations", "Donations"),
    )),
    ("insurance", "Insurance premiums", "blue", ()),
    ("taxes", "Taxes", "gray", ()),
    ("debt", "Loan and card costs", "purple", (
        ("interest", "EMI and card interest"),
        ("fees", "Loan and card fees"),
    )),
    ("fees", "Banking and exchange fees", "gray", (
        ("banking", "Banking service fees"),
        ("foreign_exchange", "Foreign exchange fees"),
    )),
    ("income", "Income", "teal", (
        ("salary", "Salary and wages"),
        ("self_employment", "Self-employment income"),
        ("interest", "Interest received"),
        ("pension", "Pension"),
        ("rent", "Rental income"),
        ("gifts", "Gifts received"),
    )),
)
ICONS = {
    "debt": "U+1F4C3",
    "education": "U+1F4D6",
    "fees": "U+1F4CB",
    "food": "U+1F35A",
    "giving": "U+1F381",
    "health": "U+2695",
    "household": "U+1F527",
    "housing": "U+1F3E0",
    "income": "U+1F4BC",
    "insurance": "U+2602",
    "leisure": "U+1F3AB",
    "personal": "U+2702",
    "shopping": "U+1F455",
    "taxes": "U+1F4C4",
    "transport": "U+1F68C",
    "utilities": "U+1F4A1",
}
COLOURS = {
    "blue": [0, 0, 255],
    "brown": [165, 42, 42],
    "gray": [128, 128, 128],
    "green": [0, 128, 0],
    "orange": [255, 165, 0],
    "purple": [128, 0, 128],
    "red": [255, 0, 0],
    "teal": [0, 128, 128],
}
CATALOGUE = {}
for root_order, (root_key, root_label, colour, children) in enumerate(GROUPS):
    nature = "INCOME" if root_key == "income" else "EXPENSE"
    CATALOGUE[root_key] = (None, nature, root_label, root_key, colour, root_order)
    for child_order, (suffix, label) in enumerate(children):
        CATALOGUE[f"{root_key}.{suffix}"] = (
            root_key, nature, label, root_key, colour, child_order,
        )


class InvalidSeed(ValueError):
    """Static rule identifiers only; refused source contents are never echoed."""


def require(condition, rule):
    if not condition:
        raise InvalidSeed(rule)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_member")
        result[key] = value
    return result


def reject_constant(_value):
    raise InvalidSeed("non_finite_number")


def normalized_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_strings(key)
            normalized_strings(item)
    elif isinstance(value, list):
        for item in value:
            normalized_strings(item)
    elif isinstance(value, str):
        require(unicodedata.normalize("NFC", value) == value, "unicode_normalization")
        require(
            not any(unicodedata.category(char).startswith("C") for char in value),
            "unicode_control",
        )


def registry(rows, fields):
    require(isinstance(rows, list) and len(rows) > 0, "registry_shape")
    result = {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == fields, "registry_shape")
        key = row["id"]
        require(isinstance(key, str) and ID_PATTERN.fullmatch(key), "registry_id")
        require(key not in result, "duplicate_registry_id")
        result[key] = row
    require(list(result) == sorted(result), "registry_order")
    return result


def validate_seed(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidSeed("utf8_encoding") from None
    require(not text.startswith("\ufeff"), "utf8_bom")
    require("\r" not in text, "lf_endings")
    require(text.endswith("\n") and not text.endswith("\n\n"), "final_newline")
    try:
        seed = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except json.JSONDecodeError:
        raise InvalidSeed("json_syntax") from None
    normalized_strings(seed)
    require(isinstance(seed, dict) and set(seed) == TOP_FIELDS, "document_shape")
    for field in ("schema_version", "seed_version"):
        require(type(seed[field]) is int and seed[field] == 1, "version")
    require(seed["market"] == "IN", "market")
    require(seed["locales"] == ["en-IN"], "locale_set")
    require(seed["default_locale"] == "en-IN", "default_locale")
    require(seed["icon_encoding"] == "unicode-scalar", "icon_encoding")
    require(seed["colour_encoding"] == "srgb8", "colour_encoding")

    icons = registry(seed["icons"], {"id", "code_point"})
    for icon in icons.values():
        point = icon["code_point"]
        require(
            isinstance(point, str) and re.fullmatch(r"U\+[0-9A-F]{4,6}", point),
            "icon_scalar",
        )
        scalar = int(point[2:], 16)
        require(0 <= scalar <= 0x10FFFF and not 0xD800 <= scalar <= 0xDFFF, "icon_scalar")
        require(unicodedata.category(chr(scalar)).startswith("S"), "icon_symbol")
    colours = registry(seed["colours"], {"id", "rgb"})
    for colour in colours.values():
        rgb = colour["rgb"]
        require(
            isinstance(rgb, list) and len(rgb) == 3
            and all(type(channel) is int and 0 <= channel <= 255 for channel in rgb),
            "colour_rgb",
        )

    rows = seed["categories"]
    require(isinstance(rows, list) and len(rows) > 0, "category_shape")
    by_key = {}
    siblings = {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == CATEGORY_FIELDS, "category_shape")
        key = row["key"]
        require(isinstance(key, str) and KEY_PATTERN.fullmatch(key), "category_key")
        require(key not in by_key, "duplicate_category_key")
        by_key[key] = row
        parent = row["parent_key"]
        require(
            parent is None or isinstance(parent, str) and KEY_PATTERN.fullmatch(parent),
            "parent_shape",
        )
        require(row["nature"] in ("EXPENSE", "INCOME"), "category_nature")
        require(isinstance(row["icon"], str) and row["icon"] in icons, "icon_reference")
        require(isinstance(row["colour"], str) and row["colour"] in colours, "colour_reference")
        labels = row["labels"]
        require(isinstance(labels, dict) and set(labels) == set(seed["locales"]), "label_locales")
        for label in labels.values():
            require(
                isinstance(label, str) and 1 <= len(label) <= 120
                and label == label.strip()
                and all(0x20 <= ord(char) < 0x7F for char in label),
                "label_text",
            )
        require(
            type(row["introduced_in"]) is int and row["introduced_in"] == 1
            and row["retired_in"] is None,
            "v1_lifecycle",
        )
        require(type(row["sort_order"]) is int and row["sort_order"] >= 0, "sort_order")
        siblings.setdefault(parent, []).append(row)

    require(set(by_key) == set(CATALOGUE), "category_catalogue")
    for key, row in by_key.items():
        seen = set()
        current = key
        while current is not None:
            require(current in by_key, "parent_reference")
            require(current not in seen, "parent_cycle")
            seen.add(current)
            current = by_key[current]["parent_key"]
        require(len(seen) <= 2, "parent_depth")
        parent = row["parent_key"]
        require(parent == (key.split(".")[0] if "." in key else None), "parent_prefix")
        if parent is not None:
            require(row["nature"] == by_key[parent]["nature"], "parent_nature")

    for group in siblings.values():
        require([row["sort_order"] for row in group] == list(range(len(group))), "sibling_order")
        for locale in seed["locales"]:
            labels = [row["labels"][locale].casefold() for row in group]
            require(len(labels) == len(set(labels)), "sibling_labels")
    expected_order = []
    for root in siblings[None]:
        expected_order.append(root["key"])
        expected_order.extend(row["key"] for row in siblings.get(root["key"], []))
    require([row["key"] for row in rows] == expected_order, "preorder")

    for key, row in by_key.items():
        require(
            (row["parent_key"], row["nature"], row["labels"]["en-IN"],
             row["icon"], row["colour"], row["sort_order"]) == CATALOGUE[key],
            "category_mapping",
        )
    require({key: row["code_point"] for key, row in icons.items()} == ICONS, "icon_catalogue")
    require({key: row["rgb"] for key, row in colours.items()} == COLOURS, "colour_catalogue")
    return seed


def category(seed, key):
    return next(row for row in seed["categories"] if row["key"] == key)


class CategorySeedSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SOURCE.read_bytes()
        cls.seed = validate_seed(cls.raw)

    def changed(self):
        return copy.deepcopy(self.seed)

    def assert_invalid(self, value, rule):
        raw = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=True) + "\n").encode("utf-8")
        with self.assertRaisesRegex(InvalidSeed, f"^{rule}$") as refusal:
            validate_seed(raw)
        self.assertNotIn("synthetic-private-marker", str(refusal.exception))

    def test_actual_versioned_source_and_complete_mappings(self):
        self.assertEqual(f"category-seed.v{self.seed['seed_version']}.json", SOURCE.name)
        self.assertEqual(59, len(self.seed["categories"]))
        self.assertEqual(16, sum(row["parent_key"] is None for row in self.seed["categories"]))
        self.assertEqual(16, len(self.seed["icons"]))
        self.assertEqual(8, len(self.seed["colours"]))
        self.assertEqual(set(ICONS), {row["icon"] for row in self.seed["categories"]})
        self.assertEqual(set(COLOURS), {row["colour"] for row in self.seed["categories"]})
        self.assertEqual(self.seed, validate_seed(self.raw))

    def test_reserved_adr_keys_and_expense_income_separation(self):
        for key in ("debt", "debt.interest", "debt.fees", "fees", "fees.foreign_exchange"):
            with self.subTest(key=key):
                self.assertEqual("EXPENSE", category(self.seed, key)["nature"])
        self.assertEqual("INCOME", category(self.seed, "income.interest")["nature"])
        self.assertEqual("EXPENSE", category(self.seed, "housing.rent")["nature"])
        self.assertEqual("INCOME", category(self.seed, "income.rent")["nature"])

    def test_every_category_is_required(self):
        for index, row in enumerate(self.seed["categories"]):
            with self.subTest(key=row["key"]):
                changed = self.changed()
                del changed["categories"][index]
                self.assert_invalid(changed, "category_catalogue")

    def test_every_icon_and_colour_is_required_by_actual_rows(self):
        for field, rule in (("icons", "icon_reference"), ("colours", "colour_reference")):
            for index, row in enumerate(self.seed[field]):
                with self.subTest(registry=field, identifier=row["id"]):
                    changed = self.changed()
                    del changed[field][index]
                    self.assert_invalid(changed, rule)

    def test_duplicate_category_and_registry_ids(self):
        for field, rule in (
            ("categories", "duplicate_category_key"),
            ("icons", "duplicate_registry_id"),
            ("colours", "duplicate_registry_id"),
        ):
            with self.subTest(field=field):
                changed = self.changed()
                changed[field].append(copy.deepcopy(changed[field][0]))
                self.assert_invalid(changed, rule)

    def test_unknown_category_and_registry_ids_even_when_well_formed(self):
        changed = self.changed()
        category(changed, "food.groceries")["key"] = "food.snacks"
        self.assert_invalid(changed, "category_catalogue")
        for field, row, rule in (
            ("icons", {"id": "other_icon", "code_point": "U+2600"}, "icon_catalogue"),
            ("colours", {"id": "other_colour", "rgb": [0, 0, 0]}, "colour_catalogue"),
        ):
            with self.subTest(field=field):
                changed = self.changed()
                changed[field].append(row)
                changed[field].sort(key=lambda value: value["id"])
                self.assert_invalid(changed, rule)

    def test_invalid_key_grammar_and_length(self):
        for key in ("Food", "a", "a" * 32, "food.a", "food." + "a" * 32,
                    "food.groceries.fresh", "food-groceries", "food/groceries", None, [], 1):
            with self.subTest(key=key):
                changed = self.changed()
                category(changed, "food.groceries")["key"] = key
                self.assert_invalid(changed, "category_key")

    def test_invalid_registry_id_grammar(self):
        for field in ("icons", "colours"):
            for identifier in ("A", "a", "a" * 32, "#000000", "icon/path", "food.icon", None, []):
                with self.subTest(field=field, identifier=identifier):
                    changed = self.changed()
                    changed[field][0]["id"] = identifier
                    self.assert_invalid(changed, "registry_id")

    def test_missing_parent_and_invalid_parent_type(self):
        changed = self.changed()
        category(changed, "food.groceries")["parent_key"] = "missing_root"
        self.assert_invalid(changed, "parent_reference")
        for parent in (True, [], {}, "bad-parent"):
            with self.subTest(parent=parent):
                changed = self.changed()
                category(changed, "food.groceries")["parent_key"] = parent
                self.assert_invalid(changed, "parent_shape")

    def test_self_cycle_and_two_node_cycle(self):
        changed = self.changed()
        category(changed, "food")["parent_key"] = "food"
        self.assert_invalid(changed, "parent_cycle")
        changed = self.changed()
        category(changed, "food")["parent_key"] = "housing"
        category(changed, "housing")["parent_key"] = "food"
        self.assert_invalid(changed, "parent_cycle")

    def test_depth_and_key_parent_disagreement(self):
        changed = self.changed()
        category(changed, "food.eating_out")["parent_key"] = "food.groceries"
        self.assert_invalid(changed, "parent_depth")
        changed = self.changed()
        category(changed, "food.groceries")["parent_key"] = "housing"
        self.assert_invalid(changed, "parent_prefix")

    def test_nature_must_be_closed_and_inherited(self):
        for nature in ("TRANSFER", "expense", None, []):
            with self.subTest(nature=nature):
                changed = self.changed()
                category(changed, "debt.interest")["nature"] = nature
                self.assert_invalid(changed, "category_nature")
        changed = self.changed()
        category(changed, "debt.interest")["nature"] = "INCOME"
        self.assert_invalid(changed, "parent_nature")
        changed = self.changed()
        for key in ("debt", "debt.interest", "debt.fees"):
            category(changed, key)["nature"] = "INCOME"
        self.assert_invalid(changed, "category_mapping")

    def test_root_and_child_order_is_integer_contiguous_and_unique(self):
        for key in ("food", "food.groceries"):
            for value, rule in ((True, "sort_order"), (-1, "sort_order"), ("0", "sort_order"),
                                (0.0, "sort_order"), (None, "sort_order"), (1, "sibling_order"),
                                (99, "sibling_order")):
                with self.subTest(key=key, value=value):
                    changed = self.changed()
                    category(changed, key)["sort_order"] = value
                    self.assert_invalid(changed, rule)

    def test_preorder_and_registry_order_are_not_implicit(self):
        changed = self.changed()
        child = category(changed, "food.eating_out")
        changed["categories"].remove(child)
        changed["categories"].append(child)
        self.assert_invalid(changed, "preorder")
        for field in ("icons", "colours"):
            with self.subTest(field=field):
                changed = self.changed()
                changed[field].reverse()
                self.assert_invalid(changed, "registry_order")

    def test_no_arbitrary_icon_or_colour_references(self):
        for field, rule, values in (
            ("icon", "icon_reference", ("not_registered", "U+1F35A", "asset.svg", "", None, [])),
            ("colour", "colour_reference", ("not_registered", "#00FF00", "rgb(0,0,0)", "", None, [])),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    changed = self.changed()
                    category(changed, "food.groceries")[field] = value
                    self.assert_invalid(changed, rule)

    def test_icon_is_a_fixed_unicode_symbol_not_an_asset_name(self):
        for value, rule in (
            ("basket", "icon_scalar"), ("U+1f35a", "icon_scalar"),
            ("U+D800", "icon_scalar"), ("U+110000", "icon_scalar"),
            ("U+000A", "icon_symbol"), ("U+0041", "icon_symbol"),
            ("U+1F35A U+FE0F", "icon_scalar"), (None, "icon_scalar"),
            ("U+2600", "icon_catalogue"),
        ):
            with self.subTest(value=value):
                changed = self.changed()
                changed["icons"][0]["code_point"] = value
                self.assert_invalid(changed, rule)

    def test_colour_is_a_fixed_srgb_triplet(self):
        for value, rule in (
            ([-1, 0, 255], "colour_rgb"), ([256, 0, 255], "colour_rgb"),
            ([True, 0, 255], "colour_rgb"), ([0.0, 0, 255], "colour_rgb"),
            (["0", 0, 255], "colour_rgb"), ([0, 255], "colour_rgb"),
            ([0, 0, 255, 255], "colour_rgb"), ("#0000FF", "colour_rgb"),
            (None, "colour_rgb"), ([1, 0, 255], "colour_catalogue"),
        ):
            with self.subTest(value=value):
                changed = self.changed()
                changed["colours"][0]["rgb"] = value
                self.assert_invalid(changed, rule)

    def test_each_label_requires_the_complete_locale_map(self):
        for row in self.seed["categories"]:
            with self.subTest(key=row["key"]):
                changed = self.changed()
                category(changed, row["key"])["labels"] = {}
                self.assert_invalid(changed, "label_locales")
        for labels in ({"en_US": "Groceries"}, {"en-IN": "Groceries", "hi-IN": "Groceries"}, [], None):
            with self.subTest(labels=labels):
                changed = self.changed()
                category(changed, "food.groceries")["labels"] = labels
                self.assert_invalid(changed, "label_locales")

    def test_locale_and_representation_metadata_are_closed(self):
        for field, values, rule in (
            ("locales", ([], ["en-IN", "en-IN"], ["en"], ["hi-IN"], "en-IN"), "locale_set"),
            ("default_locale", ("en", "hi-IN", None), "default_locale"),
            ("market", ("US", None), "market"),
            ("icon_encoding", ("svg", None), "icon_encoding"),
            ("colour_encoding", ("css", None), "colour_encoding"),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    changed = self.changed()
                    changed[field] = value
                    self.assert_invalid(changed, rule)

    def test_empty_overlong_untrimmed_or_non_ascii_v1_labels(self):
        for label in ("", " " * 3, " Groceries", "Groceries ", "x" * 121, "Caf\u00e9", None, 1):
            with self.subTest(label=label):
                changed = self.changed()
                category(changed, "food.groceries")["labels"]["en-IN"] = label
                self.assert_invalid(changed, "label_text")

    def test_sibling_labels_are_unique_ignoring_case(self):
        changed = self.changed()
        category(changed, "food.eating_out")["labels"]["en-IN"] = "gRoCeRiEs"
        self.assert_invalid(changed, "sibling_labels")

    def test_wrong_known_label_or_presentation_mapping_is_not_accepted(self):
        for field, value in (("labels", {"en-IN": "Fuel and vehicle charging"}),
                             ("icon", "health"), ("colour", "blue")):
            with self.subTest(field=field):
                changed = self.changed()
                category(changed, "food.groceries")[field] = value
                self.assert_invalid(changed, "category_mapping")

    def test_versions_and_lifecycle_cannot_be_coerced_or_silently_advanced(self):
        for field in ("schema_version", "seed_version"):
            for value in (None, True, 0, -1, 2, "1", 1.0):
                with self.subTest(field=field, value=value):
                    changed = self.changed()
                    changed[field] = value
                    self.assert_invalid(changed, "version")
        for field, values in (
            ("introduced_in", (None, True, 0, -1, 2, "1", 1.0)),
            ("retired_in", (True, 0, -1, 1, 2, "1", 1.0)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    changed = self.changed()
                    category(changed, "food")[field] = value
                    self.assert_invalid(changed, "v1_lifecycle")

    def test_missing_required_fields_are_refused_at_every_object_level(self):
        for path, fields, rule in (
            ((), TOP_FIELDS, "document_shape"),
            (("categories", 0), CATEGORY_FIELDS, "category_shape"),
            (("icons", 0), {"id", "code_point"}, "registry_shape"),
            (("colours", 0), {"id", "rgb"}, "registry_shape"),
        ):
            for field in fields:
                with self.subTest(path=path, field=field):
                    changed = self.changed()
                    target = changed
                    for part in path:
                        target = target[part]
                    del target[field]
                    self.assert_invalid(changed, rule)

    def test_unknown_private_money_or_asset_fields_are_refused(self):
        for path, field, rule in (
            ((), "owner_id", "document_shape"),
            (("categories", 0), "account_id", "category_shape"),
            (("categories", 0), "currency", "category_shape"),
            (("categories", 0), "balance", "category_shape"),
            (("categories", 0), "amount_minor", "category_shape"),
            (("categories", 0), "raw_message", "category_shape"),
            (("categories", 0), "personal_name", "category_shape"),
            (("categories", 0), "merchant_rule", "category_shape"),
            (("icons", 0), "url", "registry_shape"),
            (("colours", 0), "alpha", "registry_shape"),
        ):
            with self.subTest(path=path, field=field):
                changed = self.changed()
                target = changed
                for part in path:
                    target = target[part]
                target[field] = "synthetic-private-marker"
                self.assert_invalid(changed, rule)
        changed = self.changed()
        category(changed, "food.groceries")["labels"]["en-IN"] = "synthetic-private-marker"
        self.assert_invalid(changed, "category_mapping")

    def test_transfers_principal_unknown_purpose_and_refunds_are_not_new_categories(self):
        for key in ("upi", "transfers", "emi", "principal", "uncategorised", "unknown",
                    "refunds", "loan_disbursement", "credit_card_payment", "cash_withdrawal"):
            with self.subTest(key=key):
                changed = self.changed()
                row = copy.deepcopy(category(changed, "food"))
                row.update(key=key, labels={"en-IN": "Synthetic category"}, sort_order=16)
                changed["categories"].append(row)
                self.assert_invalid(changed, "category_catalogue")
        for old, replacement in (
            ("debt.fees", "debt.principal"),
            ("income.gifts", "income.refunds"),
            ("income.gifts", "income.loan_disbursement"),
        ):
            with self.subTest(replacement=replacement):
                changed = self.changed()
                category(changed, old)["key"] = replacement
                self.assert_invalid(changed, "category_catalogue")
        changed = self.changed()
        category(changed, "debt.interest")["labels"]["en-IN"] = "Whole EMI payment"
        self.assert_invalid(changed, "category_mapping")

    def test_empty_and_wrong_record_shapes_are_refused(self):
        for value in ({}, [], None):
            with self.subTest(document=value):
                self.assert_invalid(value, "document_shape")
        for field, rule in (("categories", "category_shape"), ("icons", "registry_shape"),
                            ("colours", "registry_shape")):
            for value in ([], {}, None, [None]):
                with self.subTest(field=field, value=value):
                    changed = self.changed()
                    changed[field] = value
                    self.assert_invalid(changed, rule)

    def test_strict_utf8_and_line_endings(self):
        for raw, rule in (
            (b"\xef\xbb\xbf" + self.raw, "utf8_bom"),
            (self.raw.decode("utf-8").encode("utf-16"), "utf8_encoding"),
            (b"\xff" + self.raw, "utf8_encoding"),
            (self.raw.replace(b"\n", b"\r\n"), "lf_endings"),
            (self.raw.rstrip(b"\n"), "final_newline"),
            (self.raw + b"\n", "final_newline"),
            (b"{broken\n", "json_syntax"),
        ):
            with self.subTest(rule=rule):
                self.assert_invalid(raw, rule)

    def test_duplicate_json_members_are_rejected_including_nested_labels(self):
        for original, replacement in (
            (b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
            (b'"nature": "EXPENSE"', b'"nature": "EXPENSE", "nature": "INCOME"'),
            (b'"en-IN": "Groceries"', b'"en-IN": "Groceries", "en-IN": "Groceries"'),
        ):
            with self.subTest(member=original):
                changed = self.raw.replace(original, replacement, 1)
                self.assertNotEqual(self.raw, changed)
                self.assert_invalid(changed, "duplicate_json_member")

    def test_non_finite_json_numbers_are_rejected(self):
        for value in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(value=value):
                changed = self.raw.replace(b'"seed_version": 1', b'"seed_version": ' + value, 1)
                self.assertNotEqual(self.raw, changed)
                self.assert_invalid(changed, "non_finite_number")

    def test_unicode_normalization_controls_and_surrogates_are_rejected(self):
        for label, rule in (
            ("Cafe\u0301", "unicode_normalization"),
            ("Groceries\u202e", "unicode_control"),
            ("Groceries\x00", "unicode_control"),
            ("Groceries\n", "unicode_control"),
            ("Groceries\ud800", "unicode_control"),
        ):
            with self.subTest(rule=rule):
                changed = self.changed()
                category(changed, "food.groceries")["labels"]["en-IN"] = label
                self.assert_invalid(changed, rule)


if __name__ == "__main__":
    unittest.main()
