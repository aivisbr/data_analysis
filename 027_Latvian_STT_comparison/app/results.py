"""Combined result views, history and immutable JSON/CSV exports. Local only."""

import csv
import io
import json
from threading import RLock
from uuid import UUID

from app.diagnostics import normalize_strict
from app.evaluation_inputs import EvaluationInputError
from app.transcripts import TranscriptStorageError, _write_atomic


class ResultsStore:
    def __init__(self, inputs, legacy, diagnostics):
        self.inputs, self.legacy, self.diagnostics = inputs, legacy, diagnostics
        self._lock = RLock()

    def history(self):
        if not self.inputs.root.exists():
            return []
        rows = []
        try:
            directories = list(self.inputs.root.iterdir())
        except OSError:
            raise EvaluationInputError() from None
        for directory in directories:
            if not directory.is_dir() or directory.name.startswith(".pending-"):
                continue
            try:
                identifier = UUID(directory.name)
            except ValueError:
                continue
            try:
                snapshot = self.inputs.load(identifier)
                rows.append({"id": str(identifier), "created_at": snapshot.created_at.isoformat(),
                    "reading": snapshot.reading, "variant": snapshot.variant,
                    "hypothesis_count": len(snapshot.hypotheses), "valid": True,
                    "complete": all(path.is_file() for path in (self.legacy._path(identifier),
                        self.diagnostics._path(identifier), directory / "results.json", directory / "results.csv"))})
            except EvaluationInputError:
                rows.append({"id": str(identifier), "created_at": None, "valid": False, "complete": False})
        return sorted(rows, key=lambda row: row["created_at"] or "", reverse=True)

    def source_path(self, item):
        source = item.source
        if source.kind == "upload":
            return str(self.inputs._path(self.inputs.inputs_root, source.input_id, "input.txt"))
        try:
            checkpoint = self.inputs.transcripts.load(source.run_id)
            _, attempt = self.inputs.transcripts._attempt(checkpoint.run, source.attempt_id)
            relative = attempt.raw_file if source.variant == "raw" else attempt.editable_file
            if relative:
                return str(self.inputs.transcripts._path(source.run_id, *relative.split("/")))
        except (TranscriptStorageError, ValueError):
            pass
        return None

    def bundle(self, identifier):
        snapshot = self.inputs.load(identifier)
        legacy, diagnostics = self.legacy.load(identifier), self.diagnostics.load(identifier)
        reference, hypotheses = self.inputs.read_snapshot_texts(identifier)
        core = {"schema_version": 1, "snapshot": snapshot.model_dump(mode="json"),
                "legacy": legacy.model_dump(mode="json"), "diagnostics": diagnostics.model_dump(mode="json"),
                "texts": {"reference": reference, "hypotheses": hypotheses,
                          "strict_reference": normalize_strict(reference),
                          "strict_hypotheses": tuple(normalize_strict(text) for text in hypotheses)}}
        return {**core, "paths": {"output_directory": str(self.inputs._path(self.inputs.root, identifier)),
            "inputs": [{"source": self.source_path(item),
                        "snapshot": str(self.inputs._path(self.inputs.root, identifier, item.file))}
                       for item in (snapshot.ground_truth, *snapshot.hypotheses)]}}

    @staticmethod
    def csv_bytes(bundle):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["evaluation_id", "reading", "variant", "label", "input_sha256", "snapshot_file",
            "legacy_scorer", "diagnostic_scorer", "wer_rate", "cer_rate", "strict_cer_rate",
            "punctuation_precision", "punctuation_recall", "punctuation_f1", "punctuation_tp", "punctuation_fp", "punctuation_fn",
            "case_accuracy", "case_correct", "case_eligible", "case_coverage", "lexical_coverage", "exact_match",
            "word_hits", "word_substitutions", "word_deletions", "word_insertions",
            "char_hits", "char_substitutions", "char_deletions", "char_insertions", "repeated_tokens"])
        for item, legacy, diagnostic in zip(bundle["snapshot"]["hypotheses"], bundle["legacy"]["scores"], bundle["diagnostics"]["scores"]):
            d, w, c = diagnostic["diagnostics"], legacy["wer"], legacy["cer"]
            p, case = d["punctuation"]["totals"], d["capitalization"]
            label = item["label"]
            # Keep spreadsheet formulas inert while retaining exact labels in JSON.
            if label.lstrip().startswith(("=", "+", "-", "@")) or label.startswith(("\t", "\r", "\n")):
                label = "'" + label
            writer.writerow([bundle["snapshot"]["id"], item["reading"], item["variant"], label, item["sha256"], item["file"],
                bundle["legacy"]["scorer_version"], bundle["diagnostics"]["scorer_version"], w["rate"], c["rate"], d["strict_cer"]["rate"],
                p["precision"], p["recall"], p["f1"], p["true_positive"], p["false_positive"], p["false_negative"],
                case["accuracy"], case["correct"], case["eligible"], case["coverage"], case["lexical_coverage"], d["exact_match"],
                w["hits"], w["substitutions"], w["deletions"], w["insertions"],
                c["hits"], c["substitutions"], c["deletions"], c["insertions"], d["has_repeated_lexical_tokens"]])
        return stream.getvalue().encode("utf-8-sig")

    def calculate(self, identifier):
        with self._lock:
            self.legacy.score(identifier)
            self.diagnostics.score(identifier)
            bundle = self.bundle(identifier)
            # Core contains immutable snapshot/results, excluding mutable source-path availability.
            core = {key: value for key, value in bundle.items() if key != "paths"}
            files = {"results.json": (json.dumps(core, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                     "results.csv": self.csv_bytes(core)}
            for filename, payload in files.items():
                path = self.inputs._path(self.inputs.root, identifier, filename)
                try:
                    if path.exists():
                        if path.read_bytes() != payload:
                            raise OSError("Export changed")
                    else:
                        _write_atomic(path, payload, exclusive=True)
                except OSError:
                    raise EvaluationInputError("Rezultātu eksportu nevar saglabāt.") from None
            return bundle

    def export_path(self, identifier, kind):
        if kind not in ("json", "csv"):
            raise EvaluationInputError("Eksporta veids nav derīgs.", code="invalid")
        bundle = self.bundle(identifier)
        core = {key: value for key, value in bundle.items() if key != "paths"}
        expected = ((json.dumps(core, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    if kind == "json" else self.csv_bytes(core))
        path = self.inputs._path(self.inputs.root, identifier, f"results.{kind}")
        try:
            if path.read_bytes() != expected:
                raise EvaluationInputError("Eksporta fails ir mainīts.")
        except FileNotFoundError:
            raise EvaluationInputError("Eksports vēl nav saglabāts; pabeidziet aprēķinu.", code="not_found") from None
        except OSError:
            raise EvaluationInputError() from None
        return path
