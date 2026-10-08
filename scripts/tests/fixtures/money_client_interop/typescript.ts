import { readFileSync, writeFileSync } from "node:fs";
import {
    SyntheticEnvelopeFromJSON,
    SyntheticEnvelopeToJSON,
} from "../generated/typescript/src/models/SyntheticEnvelope.js";
import { Money } from "../generated/typescript/src/models/Money.js";

function record(value: unknown): Record<string, unknown> {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError("transport-shape");
    }
    return value as Record<string, unknown>;
}

function main(): void {
    const [operation, inputFile, outputFile] = process.argv.slice(2);
    if (!operation || !["convert", "disagree", "reject"].includes(operation) || !inputFile || !outputFile) {
        throw new TypeError("arguments");
    }
    const input = record(JSON.parse(readFileSync(inputFile, "utf8")));
    if (!Array.isArray(input.cases) || input.cases.length < 1 || input.cases.length > 10030) {
        throw new TypeError("transport-count");
    }
    const cases: unknown[] = input.cases;
    const converted = cases.map((element, index) => {
        const row = record(element);
        if (operation === "reject") {
            let rejected = false;
            try {
                SyntheticEnvelopeFromJSON(row.envelope);
            } catch (error: unknown) {
                if (!(error instanceof Error)) throw new TypeError("conversion-error");
                rejected = true;
            }
            if (!rejected) throw new TypeError("invalid-accepted");
            return { id: row.id, status: "rejected" };
        }
        const model = SyntheticEnvelopeFromJSON(row.envelope);
        if (!(model.total instanceof Money) || typeof model.total.minorUnits !== "bigint") {
            throw new TypeError("wrapper-boundary");
        }
        // Plant the disagreement in a generated-model field, then use its emitted converter.
        if (operation === "disagree" && index === 0) {
            model.total = SyntheticEnvelopeFromJSON(record(cases[1]).envelope).total;
        }
        return { id: row.id, envelope: SyntheticEnvelopeToJSON(model) };
    });
    writeFileSync(outputFile, JSON.stringify({
        ...input,
        schema: operation === "reject" ? "pennilogic.api-money-client-rejections/1" : input.schema,
        cases: converted,
    }) + "\n", { flag: "wx" });
    console.log(JSON.stringify({
        event: "money_client_conversion",
        status: "passed",
        target: "typescript",
        operation,
        case_count: converted.length,
        run_id: input.run_id,
    }));
}

try {
    main();
} catch (error: unknown) {
    // This process is a privacy boundary: never render a codec exception or its input.
    const code = error instanceof Error ? "typescript-conversion" : "typescript-unexpected";
    console.error(JSON.stringify({ event: "money_client_conversion", status: "refused", code }));
    process.exitCode = 1;
}
