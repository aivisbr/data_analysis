"""
STT Evaluation Script — WER + CER comparison of transcription tools on Latvian content
Usage:
    python evaluate_transcription.py --ground_truth ground_truth.txt --inputs gemini.txt mistral.txt
    python evaluate_transcription.py --ground_truth ground_truth.txt --inputs gemini.txt mistral.txt --names Gemini Mistral
"""

import argparse
import glob
import re
import sys
import unicodedata
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from jiwer import wer, cer
except ImportError:
    raise SystemExit("Install jiwer first:  pip install jiwer")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

LATVIAN_DIACRITICS = set("āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ")


def normalize(text: str, keep_diacritics: bool = True) -> str:
    """
    Flatten, lowercase, and strip punctuation while preserving Latvian diacritics.
    All newlines are collapsed into a single space so segmentation differences
    between the reference and hypothesis don't affect the score.
    """
    # Collapse all whitespace / newlines
    text = " ".join(text.split())
    text = text.lower()

    if keep_diacritics:
        # Remove punctuation but keep letters (including Latvian accented ones)
        text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    else:
        # Strip diacritics too — useful for a separate "ASCII-only" comparison
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        text = re.sub(r"[^\w\s]", " ", text)

    # Collapse multiple spaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Per-sentence alignment (best-effort)
# ---------------------------------------------------------------------------

def sentence_scores(reference_lines: list[str], hypothesis: str) -> list[dict]:
    """
    Score each reference sentence individually using sequential alignment.

    The hypothesis is treated as a flat word sequence.  For each reference
    sentence (in order) we consume a proportional number of words from the
    hypothesis and compute WER/CER on that segment.  This is approximate
    but useful for spotting which sentences degrade most.
    """
    hyp_words = normalize(hypothesis).split()
    results = []
    offset = 0

    for ref_line in reference_lines:
        ref_norm = normalize(ref_line)
        if not ref_norm:
            continue
        ref_words = ref_norm.split()
        n = len(ref_words)

        # Consume the next n words from the hypothesis as the aligned segment
        segment = " ".join(hyp_words[offset:offset + n])
        offset += n

        try:
            s_wer = wer(ref_norm, segment) if segment else 1.0
            s_cer = cer(ref_norm, segment) if segment else 1.0
        except Exception:
            s_wer = s_cer = float("nan")

        results.append({
            "sentence": ref_line.strip(),
            "wer": s_wer,
            "cer": s_cer,
        })
    return results


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate(ground_truth_path: str, input_paths: list[str], names: list[str]) -> None:
    """Evaluate one or more STT tool transcripts against a ground-truth file.

    Prints per-tool WER/CER, an optional per-sentence breakdown (when the
    reference has 20 or fewer sentences), and a comparative ranking when
    multiple tools are provided.
    """
    ref_text = Path(ground_truth_path).read_text(encoding="utf-8")
    ref_lines = [l for l in ref_text.splitlines() if l.strip()]
    ref_flat = normalize(ref_text)

    ref_word_count = len(ref_flat.split())
    ref_char_count = len(ref_flat.replace(" ", ""))

    print("\n" + "=" * 70)
    print("  STT EVALUATION — WER & CER")
    print("=" * 70)
    print(f"  Reference : {ground_truth_path}")
    print(f"  Sentences : {len(ref_lines)}")
    print(f"  Words     : {ref_word_count}")
    print(f"  Chars     : {ref_char_count}")
    print("=" * 70)

    all_results = []

    for path, name in zip(input_paths, names):
        hyp_text = Path(path).read_text(encoding="utf-8")
        hyp_flat = normalize(hyp_text)

        try:
            tool_wer = wer(ref_flat, hyp_flat)
            tool_cer = cer(ref_flat, hyp_flat)
        except Exception as e:
            print(f"\n[ERROR] Could not score {name}: {e}")
            continue

        # Word-level counts (approximate)
        ref_words = ref_flat.split()
        hyp_words = hyp_flat.split()
        word_diff = len(hyp_words) - len(ref_words)

        all_results.append({
            "name": name,
            "wer": tool_wer,
            "cer": tool_cer,
            "word_count": len(hyp_words),
            "word_diff": word_diff,
        })

        print(f"\n  Tool : {name}")
        print(f"  File : {path}")
        print(f"  ─────────────────────────────")
        print(f"  WER  : {tool_wer:.2%}  (Word Error Rate  — lower is better)")
        print(f"  CER  : {tool_cer:.2%}  (Char Error Rate  — lower is better)")
        print(f"  Words in output  : {len(hyp_words)}  (ref has {len(ref_words)}, diff: {word_diff:+d})")

        # Per-sentence breakdown (only for short refs to keep output readable)
        if len(ref_lines) <= 20:
            print(f"\n  Per-sentence WER (approximate):")
            per_sent = sentence_scores(ref_lines, hyp_text)
            for i, s in enumerate(per_sent, 1):
                flag = "✓" if s["wer"] < 0.15 else ("~" if s["wer"] < 0.40 else "✗")
                snippet = s["sentence"][:60]
                print(f'    [{flag}] Sent {i:02d}  WER={s["wer"]:.0%}  CER={s["cer"]:.0%}  "{snippet}"')

    # ---------------------------------------------------------------------------
    # Summary ranking
    # ---------------------------------------------------------------------------
    if len(all_results) > 1:
        print("\n" + "=" * 70)
        print("  RANKING  (by WER, then CER)")
        print("=" * 70)
        ranked = sorted(all_results, key=lambda x: (x["wer"], x["cer"]))
        for rank, r in enumerate(ranked, 1):
            medal = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"#{rank}"
            print(f"  {medal}  {r['name']:<20} WER={r['wer']:.2%}   CER={r['cer']:.2%}")
        print()

        best = ranked[0]
        print(f"  → Best tool overall: {best['name']}")
        wer_gap = ranked[-1]["wer"] - best["wer"]
        cer_gap = ranked[-1]["cer"] - best["cer"]
        print(f"  → WER gap between best and worst: {wer_gap:.2%}")
        print(f"  → CER gap between best and worst: {cer_gap:.2%}")

    print("\n" + "=" * 70 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """Parse CLI arguments and run the evaluation."""
    parser = argparse.ArgumentParser(
        description="Evaluate STT transcription tools using WER and CER."
    )
    parser.add_argument(
        "--ground_truth", "-g",
        required=True,
        help="Path to ground truth .txt file (one sentence per line)"
    )
    parser.add_argument(
        "--inputs", "-i",
        nargs="+",
        required=True,
        help="Paths to one or more tool output .txt files"
    )
    parser.add_argument(
        "--names", "-n",
        nargs="*",
        default=None,
        help="Display names for each tool (same order as --inputs). "
             "Defaults to filenames."
    )
    args = parser.parse_args()

    # Expand globs manually (Windows CMD does not expand wildcards)
    expanded = []
    for pattern in args.inputs:
        matches = sorted(glob.glob(pattern))
        expanded.extend(matches if matches else [pattern])
    args.inputs = expanded

    # Default names = stem of each file
    if not args.names:
        args.names = [Path(p).stem for p in args.inputs]

    if len(args.names) != len(args.inputs):
        parser.error("--names must have the same number of entries as --inputs")

    evaluate(args.ground_truth, args.inputs, args.names)


if __name__ == "__main__":
    main()
