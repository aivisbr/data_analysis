# STT Transcription Evaluation

Compares speech-to-text tool outputs against a ground-truth transcript using Word Error Rate (WER) and Character Error Rate (CER).

## Requirements

- Python 3.10+
- [jiwer](https://pypi.org/project/jiwer/) — WER/CER computation

Install dependencies:

```bash
pip install jiwer
```

## Usage

```bash
# Evaluate all tool transcripts in the tests/ folder
python evaluate_cli.py -g LSMzinas_lav_GT.txt -i tests/*.txt

# Evaluate specific files with custom display names
python evaluate_cli.py -g LSMzinas_lav_GT.txt -i tests/20260328_ElevenLabs_raw.txt tests/20260328_Gemini_Fast_terms.txt --names ElevenLabs Gemini
```

### Arguments

| Flag | Description |
|---|---|
| `-g`, `--ground_truth` | Path to the ground-truth `.txt` file (one sentence per line) |
| `-i`, `--inputs` | One or more tool transcript `.txt` files |
| `-n`, `--names` | Optional display names for each tool (defaults to filenames) |

## File structure

```
LSMzinas_lav_GT.txt        # Ground-truth transcript (Latvian)
evaluate_cli.py            # Evaluation script
tests/                     # Tool transcription outputs
```
