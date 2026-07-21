"""Secret-safe credential references backed by the environment or OS keyring."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


KEYRING_SERVICE = "capslock"
_REFERENCE = re.compile(r"(env|keyring):([A-Za-z][A-Za-z0-9_.-]{0,127})\Z")


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialStatus:
    reference: str
    source: str
    available: bool


def parse_reference(reference: str) -> tuple[str, str]:
    match = _REFERENCE.fullmatch(reference.strip())
    if match is None:
        raise ValueError("credential must be env:NAME or keyring:NAME")
    return match.group(1), match.group(2)


def resolve_credential(reference: str) -> str | None:
    source, name = parse_reference(reference)
    if source == "env":
        return os.environ.get(name)
    keyring = _keyring()
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception as exc:
        raise CredentialError(
            "OS credential storage is unavailable; use an env:NAME reference"
        ) from exc


def credential_status(reference: str) -> CredentialStatus:
    source, _ = parse_reference(reference)
    try:
        available = bool(resolve_credential(reference))
    except CredentialError:
        available = False
    return CredentialStatus(reference, source, available)


def set_keyring_credential(name: str, secret: str) -> None:
    parse_reference(f"keyring:{name}")
    if not secret:
        raise ValueError("credential must not be empty")
    try:
        _keyring().set_password(KEYRING_SERVICE, name, secret)
    except Exception as exc:
        raise CredentialError(
            "OS credential storage is unavailable; use an env:NAME reference"
        ) from exc


def delete_keyring_credential(name: str) -> None:
    parse_reference(f"keyring:{name}")
    try:
        _keyring().delete_password(KEYRING_SERVICE, name)
    except Exception as exc:
        raise CredentialError("could not delete the OS credential") from exc


def _keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise CredentialError(
            "keyring is not installed; reinstall CapsLock or use env:NAME"
        ) from exc
    return keyring
