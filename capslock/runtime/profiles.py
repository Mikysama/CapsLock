"""Explicit configured model identities shared by runtime and clients."""

from __future__ import annotations

from ..configuration import ModelProfileSettings, ProviderSettings


def resolve_profile(
    profiles: dict[str, ModelProfileSettings],
    *,
    profile_id: str | None = None,
    model: str | None = None,
) -> ModelProfileSettings:
    if profile_id is not None:
        if profile_id not in profiles:
            raise ValueError(
                f"model profile is unavailable: {profile_id}; select /model"
            )
        return profiles[profile_id]
    matches = [p for p in profiles.values() if p.model == (model or "").strip()]
    if len(matches) != 1:
        raise ValueError(
            "model name is unconfigured or ambiguous; select a configured profile with /model"
        )
    return matches[0]


def profile_choices(
    profiles: dict[str, ModelProfileSettings], providers: dict[str, ProviderSettings]
) -> list[dict[str, object]]:
    result = []
    for key, profile in sorted(profiles.items()):
        provider = providers.get(profile.provider)
        available = bool(
            provider
            and provider.api_key
            and not provider.api_key.startswith("your_")
            and provider.strict_tool_calls
        )
        result.append(
            {
                "id": key,
                "model": profile.model,
                "provider": profile.provider,
                "available": available,
            }
        )
    return result
