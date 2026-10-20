"""Initial documented capability profiles, not proof of endpoint/account access."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from app.contracts import Mode, Provider, Record


class Support(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


class CapabilityProfile(Record):
    id: str
    version: int = Field(default=1, ge=1)
    provider: Provider
    model_id: str
    mode: Mode
    delivery: Literal["streaming", "committed_turn", "completed_file"]
    language: Support = Support.SUPPORTED
    keywords: Support = Support.UNSUPPORTED
    prompt: Support = Support.UNSUPPORTED
    latency: Support = Support.UNSUPPORTED
    formatting: Support = Support.UNSUPPORTED
    automatic_turn_detection: Support = Support.UNSUPPORTED
    input_sample_rates: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()


def _profile(provider, model_id, mode, delivery, **options):
    return CapabilityProfile(id=f"{provider.value}:{model_id}:{mode.value}",
                             provider=provider, model_id=model_id,
                             mode=mode, delivery=delivery, **options)


CAPABILITY_PROFILES: tuple[CapabilityProfile, ...] = (
    _profile(Provider.OPENAI, "gpt-transcribe", Mode.LIVE, "committed_turn",
             keywords=Support.SUPPORTED, prompt=Support.SUPPORTED,
             automatic_turn_detection=Support.UNVERIFIED, input_sample_rates=(24000,),
             notes=('Adapteris izmanto manuālus 10 sekunžu apstiprinājumus; automātiska VAD nav pieejama.',)),
    _profile(Provider.OPENAI, "gpt-live-transcribe", Mode.LIVE, "streaming",
             keywords=Support.SUPPORTED, prompt=Support.SUPPORTED,
             latency=Support.UNVERIFIED, input_sample_rates=(24000,),
             notes=("Rokasgrāmatas un GA shēmas latentuma atbalsts atšķiras; jāpārbauda.",)),
    _profile(Provider.OPENAI, "gpt-realtime-whisper", Mode.LIVE, "streaming",
             latency=Support.SUPPORTED, input_sample_rates=(24000,),
             notes=("Nav VAD vai konteksta uzvednes atbalsta; jāizmanto manuāla vadība.",)),
    _profile(Provider.OPENAI, "gpt-transcribe", Mode.FILE, "completed_file",
             keywords=Support.SUPPORTED, prompt=Support.SUPPORTED),
    _profile(Provider.GEMINI, "gemini-3.5-transcribe-live", Mode.LIVE, "streaming",
             keywords=Support.SUPPORTED, formatting=Support.SUPPORTED,
             automatic_turn_detection=Support.SUPPORTED, input_sample_rates=(16000,),
             notes=('Sesija līdz 10 minūtēm; pirms pauzes jāsaņem gala apstiprinājums. Valodas kods: lv-LV. Līdz 1000 terminiem.',)),
    _profile(Provider.GEMINI, "gemini-3.5-transcribe", Mode.FILE, "completed_file",
             keywords=Support.SUPPORTED, formatting=Support.SUPPORTED),
    _profile(Provider.ELEVENLABS, "scribe_v2_realtime", Mode.LIVE, "streaming",
             keywords=Support.SUPPORTED, formatting=Support.SUPPORTED,
             automatic_turn_detection=Support.UNVERIFIED,
             input_sample_rates=(24000,),
             notes=('Adapteris izmanto manuālus 20 sekunžu apstiprinājumus. VAD robežas vēl nav pārbaudītas; izvēle atspējota. Līdz 50 terminiem, katrs līdz 20 rakstzīmēm. ElevenLabs maksas atļauja ir atsevišķa.',)),
    _profile(Provider.ELEVENLABS, "scribe_v2", Mode.FILE, "completed_file",
             keywords=Support.SUPPORTED, formatting=Support.SUPPORTED),
    _profile(Provider.MISTRAL, "voxtral-mini-transcribe-realtime-3-0", Mode.LIVE, "streaming",
             keywords=Support.SUPPORTED, latency=Support.SUPPORTED, input_sample_rates=(16000,),
             notes=('Voxtral 3: lv-LV → lv. Latentums: minimal=80, low=240, medium=480, high=1200, xhigh=2400 ms. Pauze noslēdz sesiju; turpināšana atver jaunu. Pilnīgai sesijai nepieciešams transcription.done; kļūdas gadījumā saņemtais teksts tiek saglabāts kā nepilnīgs pieraksts. Automātiska posmu vadība šajā adapterī nav iespējota.',)),
    _profile(Provider.MISTRAL, "voxtral-small-transcribe-3-0", Mode.FILE, "completed_file",
             keywords=Support.SUPPORTED),
    _profile(Provider.MISTRAL, "voxtral-mini-transcribe-3-0", Mode.FILE, "completed_file",
             keywords=Support.SUPPORTED),
)


def get_profile(profile_id: str) -> CapabilityProfile:
    for profile in CAPABILITY_PROFILES:
        if profile.id == profile_id:
            return profile
    raise ValueError("Modeļa iespēju profils nav atrasts.")
