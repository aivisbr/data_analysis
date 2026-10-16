"""Compare T008 scores against the original CLI without executing its main/UI."""

import argparse
import ast
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys
import unicodedata

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import jiwer
from app.scoring import SCORER_VERSION, normalize_legacy, score_legacy


def original_normalize():
    """Load only the supplied normalize function; avoid stdout changes/import side effects."""
    tree = ast.parse((PROJECT_ROOT / "example" / "evaluate_cli.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "normalize")
    namespace = {"re": re, "unicodedata": unicodedata}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "example/evaluate_cli.py", "exec"), namespace)
    return namespace["normalize"]


def verify_examples():
    normalize = original_normalize()
    reference_bytes = (PROJECT_ROOT / "example" / "LSMzinas_lav_GT.txt").read_bytes()
    reference = reference_bytes.decode("utf-8-sig")
    ref_norm = normalize(reference)
    if normalize_legacy(reference) != ref_norm:
        raise AssertionError("Reference normalization differs")
    rows = []
    for path in sorted((PROJECT_ROOT / "example" / "tests").rglob("*.txt")):
        payload = path.read_bytes()
        hypothesis = payload.decode("utf-8-sig")
        hyp_norm = normalize(hypothesis)
        wer, cer = score_legacy(reference, hypothesis)
        if normalize_legacy(hypothesis) != hyp_norm or wer.rate != jiwer.wer(ref_norm, hyp_norm) or cer.rate != jiwer.cer(ref_norm, hyp_norm):
            raise AssertionError(f"Legacy mismatch: {path.name}")
        rows.append({"file": path.relative_to(PROJECT_ROOT).as_posix(),
                     "source_sha256": sha256(payload).hexdigest(),
                     "wer": wer.model_dump(), "cer": cer.model_dump()})
    return {"scorer_version": SCORER_VERSION, "jiwer_version": version("jiwer"),
            "reference_file": "example/LSMzinas_lav_GT.txt",
            "reference_sha256": sha256(reference_bytes).hexdigest(),
            "matched_files": len(rows), "scores": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON verification report")
    args = parser.parse_args()
    report = verify_examples()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: {report['matched_files']} example transcripts match original whole-document WER/CER.")
    print(f"Scorer: {report['scorer_version']}; jiwer: {report['jiwer_version']}")


if __name__ == "__main__":
    main()
