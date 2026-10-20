# Diktāts — Latvian speech-to-text comparison

A local web application for comparing speech-to-text models on Latvian dictation audio. Transcribe browser-tab audio live, upload an audio file, or compare existing transcripts against a reference text.

The backend is Python/FastAPI. The browser interface is in **Latvian**, and the setup instructions below target **Windows 11 and CMD**. The frontend is served by the backend; no separate frontend server or JavaScript build step is needed.

## Features

- Run selected models independently, with separate progress and errors.
- Capture audio from another browser tab on the same laptop, with pause/resume/stop controls.
- Upload audio and prepare a shared interval for file-based transcription.
- Keep the first full reading (**A / Try1**) and final full reading (**D / Try2**) separate.
- Save confirmed transcripts as original TXT files and separate editable copies.
- Evaluate transcripts against a Ground Truth TXT file using WER, CER, strict CER, punctuation F1 and capitalization diagnostics.
- Inspect highlighted text differences, reopen previous evaluations and export JSON/CSV results.
- Persist model selections and settings in SQLite.
- Try the workflow with free DEMO adapters that return clearly labelled synthetic text.

## Requirements

- Python **3.13**, the version used for development.
- FFmpeg and ffprobe on `PATH` for audio upload, preview and preparation.
- A browser supporting tab-audio sharing, such as Chrome or Edge on Windows. Audio-sharing options depend on the browser.
- Provider API keys and access to the selected models for real transcription. Keys are not required to start the app or evaluate existing TXT files.

## Install and start

Download or clone the repository, open CMD in its root directory, then run:

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist .env copy .env.example .env
```

If `.venv` already exists, reuse it. Edit `.env` and supply the keys for the providers you want to use:

```dotenv
OPENAI_API_KEY=your-openai-key
GEMINI_API_KEY=your-gemini-key
ELEVENLABS_API_KEY=your-elevenlabs-key
MISTRAL_API_KEY=your-mistral-key
```

Keep real keys in your local `.env`; only the placeholder `.env.example` belongs in Git. Existing process environment variables take precedence over `.env` values.

Check the audio tools and start the backend:

```bat
ffmpeg -version
ffprobe -version
.venv\Scripts\python.exe -m app
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/). Stop the server with **Ctrl+C**. If port 8000 is occupied:

```bat
.venv\Scripts\python.exe -m app --port 8001
```

The app binds to `127.0.0.1` and is intended for use on the same computer. Starting it does not initiate transcription requests. Local recording, DEMO execution and TXT evaluation are available without enabling paid transcription.

## Enable real transcription

Real provider execution is **disabled by default**, even when keys are configured. Set the relevant flags **before starting the backend, in the same CMD window**:

| Environment variable | Set to `1` to enable |
| --- | --- |
| `P7_ALLOW_PAID_TRANSCRIPTION` | Uploaded-file transcription through OpenAI, Gemini, ElevenLabs and Mistral. |
| `P7_ALLOW_PAID_LIVE_TRANSCRIPTION` | Live provider transcription. |
| `P7_ALLOW_PAID_ELEVENLABS_LIVE` | ElevenLabs live transcription, in addition to the general live flag. |

For example, to enable all implemented provider routes:

```bat
set "P7_ALLOW_PAID_TRANSCRIPTION=1"
set "P7_ALLOW_PAID_LIVE_TRANSCRIPTION=1"
set "P7_ALLOW_PAID_ELEVENLABS_LIVE=1"
.venv\Scripts\python.exe -m app
```

These variables can also be configured in your local `.env`. Restart the backend after changing them, then refresh the page. A separate launcher script is not required.

Only enabled models are selected for a new task. Each selected provider may charge separately; the flags are execution switches, **not monetary spending limits**. Use your provider dashboards to monitor costs. Requests are not automatically retried or reconnected; an explicit model retry is a new attempt and may incur another charge.

## Implemented model routes

| Provider | Live | Uploaded audio |
| --- | --- | --- |
| OpenAI | `gpt-transcribe`, `gpt-live-transcribe`, `gpt-realtime-whisper` | `gpt-transcribe` |
| Google Gemini | `gemini-3.5-transcribe-live` | `gemini-3.5-transcribe` |
| ElevenLabs | `scribe_v2_realtime` | `scribe_v2` |
| Mistral | `voxtral-mini-transcribe-realtime-3-0` | `voxtral-small-transcribe-3-0`, `voxtral-mini-transcribe-3-0` |

These are the exact IDs implemented in this project. Account access and provider availability must be checked for your own keys. The app does not silently replace a model with a different ID.

Mistral entries are added once on the next backend startup, including for existing databases, and start **unchecked**. Enable only the models you want to test. Deleted entries are not added again on later restarts. Mistral uses the existing file/live execution flags; no additional Mistral flag or launcher is required. The previous OpenAI/Gemini test budget does not include Mistral charges.

Voxtral 3 supports language hints (`lv-LV` maps to `lv`) and keywords through `context_bias`. Live latency presets map to 80, 240, 480, 1200 and 2400 ms for minimal, low, medium, high and xhigh respectively; leaving latency unset uses the service default. Prompt, formatting, automatic turn detection, diarization and timestamps are not exposed by these adapters. Pause ends a session and resume starts another; Voxtral 3 deltas are confirmed append-only text and are saved as a growing confirmed span. The service must still return `transcription.done` on pause/stop for the session to count as complete. An API error or missing final acknowledgement leaves the run incomplete, but received text is exported to both TXT copies. Audio uses 100 ms packets and an unchanged final tail; stopping sends `input_audio.end`. Numeric provider error codes and safe error categories are retained without arbitrary provider messages. No audio is padded, replayed or automatically retried.

The native Mistral file SDK is used for uploads. For realtime, the installed SDK's update helper exposes fewer settings than its Voxtral 3 schemas, so the adapter sends those public SDK message schemas over a backend WebSocket. See the [Voxtral 3 migration guide](https://docs.mistral.ai/studio/audio/migrate-from-voxtral-2); older documentation under `/deprecated/` describes Voxtral 2.

Use **Izmantot** (Use) to enable or disable a model, **Iestatījumi** (Settings) to edit it, and **Dzēst** (Delete) to remove its registry entry. Deleting an entry does not delete existing transcripts or history. Model settings and enabled states survive refreshes and server restarts; settings are frozen for each new run.

The settings form offers language hints, keywords/custom vocabulary, prompts, formatting and latency where supported by the integration. Use language codes such as `lv` or `lv-LV`; the form shows how they map to the provider. Disabled controls are unsupported or unverified for that profile. Adding an arbitrary model ID in the UI does not implement a new adapter: additional IDs/providers may require Python code and capability-profile changes.

## Live transcription — Tiešraide

1. Open the audio source, such as the dictation video, in another browser tab.
2. In **Tiešraide**, select models and choose **A · Try1** or **D · Try2**.
3. Choose how to process the capture:
   - **Neither server checkbox selected:** record locally in the browser. Download the WAV yourself; it will not appear in server history.
   - **Bezmaksas DEMO:** save audio on the backend and exercise the pipeline with synthetic placeholder text. This is not recognition of the recording.
   - **Maksas tiešraide:** send audio to the enabled real models. The general live flag must be enabled; ElevenLabs also needs its separate flag.
4. Click **Sākt** (Start). Select the source tab and enable **tab-audio sharing** in the browser's sharing dialog.
5. Use **Pauzēt** (Pause) to exclude repeated dictation fragments or other unwanted audio. Wait for the pause operation to finish before clicking **Turpināt** (Resume).
6. Click **Apturēt** (Stop), and wait for finalization and saving before closing the page.
7. Open **Tiešraides izpilžu vēsture** (Live run history), refresh the history if needed, and inspect each model's status, text and TXT downloads.

For a dictation read first in full, then in fragments, then in full again, make separate runs for the first and final complete readings. A and D are reading labels; they do not mean that the model learns from the first attempt.

Pausing drains accepted audio and closes the current model sessions. Resuming creates fresh sessions for a new segment in the same run. A failed worker is not silently restarted by Resume. Audio capture does not automatically resume after page reload or server restart.

## Uploaded audio — Audio fails

1. Open **Audio fails** and upload an audio file.
2. Select the reading label and the start/end times for the interval to transcribe. Preview the recording if needed.
3. Prepare the interval using **Sagatavot izvēlēto intervālu**. Changing the time fields afterward requires preparing a new interval.
4. Select models with **Izmantot**.
5. Click **Sākt transkripciju** (Start transcription). The file paid flag must be enabled. Alternatively, use **Izmēģināt ar DEMO modeļiem** for a synthetic demonstration without provider calls.
6. Review each model's result. Original and editable TXT copies are saved automatically; downloading them is optional.

The local upload limits are **250 MB** and **two hours**. FFmpeg prepares shared mono 24 kHz WAV audio, split into ordered chunks where needed. Models in one file job use the same preparation; chunk results are concatenated without text deduplication. A provider may impose additional limits. One model's failure does not stop the others. **Atkārtot modeli** creates a new attempt using the original frozen selection/preparation rather than replacing the earlier result.

## Compare transcripts — Rezultāti

1. Prepare a **UTF-8 TXT** reference containing the correct text. Text inputs are limited to **2 MiB per file**.
2. In **Rezultāti** (Results), choose A/Try1 or D/Try2 and the transcript variant: originals or edited copies.
3. Use **Pievienot etalonu** (Add reference) and **Pievienot transkriptus** (Add transcripts), or select available managed transcript files.
4. Select the transcript rows to compare and click **Aprēķināt** (Calculate).
5. Inspect the metric table and expand **Atšķirības** (Differences). Download JSON/CSV if needed.

Each evaluation saves the exact bytes read at that time. To evaluate corrections, edit the managed editable TXT or reupload an edited external file, then calculate a **new evaluation**. Old evaluations keep their original inputs and scores.

Uploaded TXT files become backend copies: editing the file you originally uploaded does not change that copy. Use the displayed **Avota fails** (Source file) path to identify what will be read next. **Saglabātā ievade** (Saved input) belongs to the immutable evaluation snapshot and should not be edited. A single evaluation cannot mix A and D or original and edited variants.

### Metrics

| UI column | Meaning |
| --- | --- |
| **WER ↓** | Word error rate: `(substitutions + deletions + insertions) / reference word count`. |
| **CER ↓** | Character error rate, using the same edit-distance principle on characters. |
| **Stingrais CER ↓** | Strict CER, preserving capitalization and punctuation. |
| **Pieturzīmju F1 ↑** | Punctuation F1: `2 × correct / (2 × correct + extra + missing)`. |
| **Burtu reģistrs ↑** | Correct capitalization among aligned words whose spelling otherwise matches, ignoring case. |
| **Tvērums** | Share of reference words with letter case that were eligible for capitalization comparison. |
| **Precīzi** | Exact equality after strict normalization; displayed as Jā/Yes or Nē/No. |

WER and ordinary CER lowercase both texts, replace punctuation with spaces and collapse whitespace. Latvian diacritics remain significant: `ā` differs from `a`. This legacy normalization does not apply Unicode NFC. Both metrics use `jiwer` and unit-cost Levenshtein edits. Error rates can exceed 100%; they are not accuracy percentages.

Strict normalization applies Unicode NFC and collapses whitespace while preserving case and punctuation. For example, reference `"Vidējais teikuma ilgums – divas stundas"` versus transcript `Vidējais teikuma ilgums - divas stundas` gives ordinary CER **0%** and strict CER **7.32%**: two missing quotation marks plus one dash substitution, divided by 41 reference characters. Spaces remaining after normalization count in CER.

Punctuation F1 includes quotation marks, brackets, dashes and other Unicode punctuation. It compares exact symbols at boundaries anchored by matching adjacent words, ignoring case. Both words must align and remain adjacent; at the text edges, the first/last word provides the anchor. A misspelled neighbouring word can make an otherwise plausible punctuation mark count as **both missing and extra**. Whitespace itself is ignored, but splitting a word can change those anchors. Read this metric alongside the differences view; it is not an independent or official dictation grading scale.

Capitalization accuracy is conditional on coverage. For example, if one of nine reference words is misspelled and one of the remaining eight has incorrect case, coverage is **8/9 = 88.89%**, and capitalization accuracy is **7/8 = 87.50%**. Coverage plus WER need not equal 100%. Missing denominators are shown as **N/A**.

The calculations are implemented in [app/scoring.py](app/scoring.py) and [app/diagnostics.py](app/diagnostics.py).

## Local storage

The app creates the runtime directories automatically:

```text
data/
  app.sqlite3                 Model registry and settings
  audio/                      Uploaded audio and preparations
  evaluation_inputs/          Imported reference/transcript copies
runs/
  <run-id>/
    metadata.json             Frozen selection and transcript checkpoints
    Try1/ or Try2/
      raw/                    Original confirmed TXT exports
      editable/               Separate copies for manual correction
evaluations/
  <evaluation-id>/             Input snapshot, scores and JSON/CSV exports
```

Live runs also preserve source audio and frame/distribution metadata. Original exports and existing editable copies are not overwritten during finalization. **Confirmed** text is exported to TXT; provisional text remains separate in checkpoints/history and is not automatically promoted into the original transcript.

By default, storage is under the project root. To use another parent directory, set this before starting the backend:

```bat
set "P7_STORAGE_ROOT=C:\diktats-data"
.venv\Scripts\python.exe -m app
```

A relative storage root is resolved against the project root. Changing it selects another storage location; it does not migrate existing files. Runtime data and credentials are excluded by `.gitignore`; add any custom storage directory inside the repository to the ignore rules too.

## Troubleshooting and limits

- **The page refuses to connect:** start the backend and keep its CMD window open. Check the URL/port printed in the terminal.
- **Paid controls are disabled:** set the appropriate flag in the backend process's CMD or `.env`, restart, and refresh the browser. Also prepare the audio interval and select models for file transcription.
- **No tab audio:** select a browser tab with audio sharing enabled. The current input workflow captures tab audio, not the microphone.
- **No local-only recording in history:** local capture stays in the browser. Download it before another recording or closing the page; choose DEMO or paid live mode to save on the backend.
- **Nepilnīgs / Incomplete:** processing ended without full finalization or encountered an error. Saved confirmed text can still be usable, but its completeness is not guaranteed. Inspect that model's error; a terminal saved run does not keep transcribing in the background.
- **An original TXT is empty:** the model may have produced only provisional text. Check the provisional history section and error rather than assuming it will later appear in the export.
- **Gemini text looks complete but the run is incomplete:** the service may not have acknowledged the audio end after Stop. The app keeps the confirmed text without claiming that every tail sample was processed.
- **Long live sessions:** the Gemini adapter limits each session to ten minutes; there is no automatic rollover/reconnection. OpenAI uses explicit ten-second commits; ElevenLabs uses serialized twenty-second commits. Text updates and finalization can therefore lag behind playback.
- **After restart:** unfinished runs are recovered as interrupted without replaying audio. Capture needs to be started again explicitly.

This is a single-process local application. Do not use multiple backend writer processes against the same storage. Available controls differ between providers; some options remain unverified, and successful default transcription does not establish support for every custom setting.

## Offline checks

From the project root:

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m pip check
```

The tests use synthetic data and mocked provider transports; they do not initiate paid transcription calls. Some compatibility/input/diagnostic tests currently require `example/evaluate_cli.py`, `example/LSMzinas_lav_GT.txt` and the 19 TXT fixtures under `example/tests/`, including its nested directories. Keep those files when running the full suite. The application itself does not need `example/` to start or process user-supplied inputs.

While the backend is running, a basic local health check is:

```bat
curl.exe http://127.0.0.1:8000/api/health
```

Health confirms that the local server responds; it does not verify provider access. Browser audio-sharing behaviour requires manual testing in your browser.

Historical implementation notes are preserved in [README_DEVELOPMENT.md](README_DEVELOPMENT.md). Those notes include references to local development documents and test records that may be omitted from the public repository; use this README for current setup and usage.
