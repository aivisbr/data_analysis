"""Neutral hint-to-provider code mapping, without SDK imports or API calls."""

from app.contracts import Provider, TranscriptionSettings


# Explicit default regional codes for short hints; do not invent a region otherwise.
DEFAULT_LOCALES = {
    "lv": "lv-LV", "en": "en-US", "lt": "lt-LT", "et": "et-EE", "de": "de-DE",
    "fr": "fr-FR", "es": "es-ES", "ru": "ru-RU", "it": "it-IT", "pt": "pt-BR",
    "pl": "pl-PL", "uk": "uk-UA", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN",
}


def resolve_language(provider: Provider, hint: str | None) -> str | None:
    if hint is None:
        return None
    parts = hint.split("-")
    base = parts[0].lower()
    if len(base) != 2 or not base.isascii() or not base.isalpha():
        raise ValueError("Izmantojiet ISO 639-1 vai BCP-47 valodas kodu, piemēram, lv vai lv-LV.")
    if provider == Provider.GEMINI:
        if len(parts) == 1:
            if base not in DEFAULT_LOCALES:
                raise ValueError("Šai valodai norādiet pilnu BCP-47 kodu ar reģionu.")
            return DEFAULT_LOCALES[base]
        return "-".join([base, *[part.upper() if len(part) == 2 else part for part in parts[1:]]])
    return base


def effective_settings(provider: Provider, settings: TranscriptionSettings) -> TranscriptionSettings:
    return settings.evolve(language_hint=resolve_language(provider, settings.language_hint))
