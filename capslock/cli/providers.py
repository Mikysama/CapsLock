"""Provider client construction independent of the CLI application module."""

from openai import AsyncOpenAI

from ..configuration import Settings
from ..runtime import AgentRuntimeError


def create_client(settings: Settings) -> AsyncOpenAI:
    if not settings.model_config.api_key or settings.model_config.api_key.startswith(
        "your_"
    ):
        raise AgentRuntimeError("API key is not configured")
    return AsyncOpenAI(
        api_key=settings.model_config.api_key,
        base_url=settings.model_config.base_url,
        timeout=settings.model_config.timeout_seconds,
    )


def create_provider_clients(settings: Settings) -> dict[str, AsyncOpenAI]:
    clients: dict[str, AsyncOpenAI] = {}
    for name, provider in (settings.providers or {}).items():
        if not provider.api_key or provider.api_key.startswith("your_"):
            continue
        clients[name] = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=provider.timeout_seconds,
        )
    if not clients:
        create_client(settings)
    return clients
