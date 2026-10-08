"""Drive emitted model methods and Pydantic validation, not a substitute Money codec."""

import json
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 5 or sys.argv[1] not in {"convert", "disagree", "reject"}:
        raise ValueError("arguments")
    operation, generated, input_file, output_file = sys.argv[1:]
    sys.path.insert(0, generated)
    from pydantic import ValidationError
    from pennilogic_contracts.models.money import Money, MoneyWireError
    from pennilogic_contracts.models.synthetic_envelope import SyntheticEnvelope

    document = json.loads(Path(input_file).read_bytes())
    rows = document["cases"]
    if type(rows) is not list or not 1 <= len(rows) <= 10030:
        raise ValueError("transport-count")
    if SyntheticEnvelope.model_config.get("strict") is not True or SyntheticEnvelope.model_config.get("hide_input_in_errors") is not True:
        raise ValueError("validation-configuration")
    converted = []
    for index, row in enumerate(rows):
        wire = row["envelope"]
        if operation == "reject":
            for convert in (SyntheticEnvelope.from_dict, SyntheticEnvelope.model_validate):
                try:
                    convert(wire)
                except (MoneyWireError, ValidationError):
                    continue
                raise ValueError("invalid-accepted")
            converted.append({"id": row["id"], "status": "rejected"})
            continue
        model = SyntheticEnvelope.from_dict(wire)
        validated = SyntheticEnvelope.model_validate(wire)
        if not isinstance(model.total, Money) or model.total != validated.total:
            raise ValueError("wrapper-boundary")
        if operation == "disagree" and index == 0:
            other = SyntheticEnvelope.from_dict(rows[1]["envelope"])
            model = model.model_copy(update={"total": other.total})
        rendered = model.to_dict()
        if rendered != json.loads(model.to_json()) or rendered != model.model_dump(mode="json", exclude_none=True):
            raise ValueError("serializer-disagreement")
        converted.append({"id": row["id"], "envelope": rendered})
    if operation == "reject":
        document["schema"] = "pennilogic.api-money-client-rejections/1"
    document["cases"] = converted
    with Path(output_file).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, separators=(",", ":")) + "\n")
    print(json.dumps({
        "event": "money_client_conversion", "status": "passed", "target": "python",
        "operation": operation, "case_count": len(converted), "run_id": document["run_id"],
    }))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TypeError, KeyError, IndexError, OSError, ImportError):
        print('{"event":"money_client_conversion","status":"refused","code":"python-conversion"}', file=sys.stderr)
        raise SystemExit(1) from None
