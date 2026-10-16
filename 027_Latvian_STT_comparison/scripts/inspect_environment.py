r"""Offline T001 inspection; no clients, credentials, installs or API calls.

Windows CMD: .venv\Scripts\python.exe scripts\inspect_environment.py
Optional snapshot: append --output docs\environment.json
"""

import argparse
import importlib
import inspect
import json
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


PACKAGES = (
    "openai", "google-genai", "elevenlabs", "fastapi", "uvicorn", "jiwer",
    "python-dotenv", "python-multipart", "websockets", "httpx", "numpy", "pytest",
)
INTERFACES = {
    "openai.live.connect": ("openai.resources.realtime.realtime", "AsyncRealtime", "connect"),
    "openai.live.update": ("openai.resources.realtime.realtime", "AsyncRealtimeSessionResource", "update"),
    "openai.live.append": ("openai.resources.realtime.realtime", "AsyncRealtimeInputAudioBufferResource", "append"),
    "openai.live.commit": ("openai.resources.realtime.realtime", "AsyncRealtimeInputAudioBufferResource", "commit"),
    "openai.file.create": ("openai.resources.audio.transcriptions", "AsyncTranscriptions", "create"),
    "gemini.live.connect": ("google.genai.live", "AsyncLive", "connect"),
    "gemini.live.send": ("google.genai.live", "AsyncSession", "send_realtime_input"),
    # Inspect generated SDK internals only; application adapters use client.aio.interactions.
    "gemini.file.create": ("google.genai._gaos.google_genai", "AsyncGeminiNextGenInteractions", "create"),
    "elevenlabs.live.connect": ("elevenlabs.realtime.scribe", "ScribeRealtime", "connect"),
    "elevenlabs.live.send": ("elevenlabs.realtime.connection", "RealtimeConnection", "send"),
    "elevenlabs.live.commit": ("elevenlabs.realtime.connection", "RealtimeConnection", "commit"),
    "elevenlabs.live.close": ("elevenlabs.realtime.connection", "RealtimeConnection", "close"),
    "elevenlabs.file.convert": ("elevenlabs.speech_to_text.client", "AsyncSpeechToTextClient", "convert"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"python": sys.version.split()[0], "executable": sys.executable,
              "packages": {}, "tools": {}, "interfaces": {}, "errors": []}
    for package in PACKAGES:
        try:
            report["packages"][package] = version(package)
        except PackageNotFoundError:
            report["packages"][package] = None
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        entry = {"path": path, "version": None}
        if path:
            try:
                result = subprocess.run([path, "-version"], capture_output=True,
                                        text=True, timeout=10, check=True)
                entry["version"] = result.stdout.splitlines()[0]
            except (OSError, subprocess.SubprocessError) as exc:
                report["errors"].append(f"{tool}: {type(exc).__name__}")
        report["tools"][tool] = entry
    for label, (module, cls, method) in INTERFACES.items():
        try:
            fn = getattr(getattr(importlib.import_module(module), cls), method)
            signature = inspect.signature(fn)
            entry = {"parameters": list(signature.parameters),
                     "is_coroutine_function": inspect.iscoroutinefunction(fn)}
            if label == "openai.live.connect":
                entry["default_max_retries"] = signature.parameters["max_retries"].default
            report["interfaces"][label] = entry
        except (ImportError, AttributeError, ValueError, KeyError) as exc:
            report["errors"].append(f"{label}: {type(exc).__name__}")
    try:
        importlib.import_module("openai.lib._websocket")
        report["openai_websocket_transport_import"] = True
        from google.genai import interactions, types
        report["gemini_config_fields"] = {
            "live": list(types.AudioTranscriptionConfig.model_fields),
            "file": list(interactions.TranscriptionConfig.model_fields),
            "generation": list(interactions.GenerationConfig.model_fields),
        }
    except (ImportError, AttributeError) as exc:
        report["errors"].append(f"SDK config/transport: {type(exc).__name__}")
    payload = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
