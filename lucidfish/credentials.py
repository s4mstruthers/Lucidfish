"""API keys for cloud LLM providers — stored safely, never in the database.

Lookup order for a provider's key:

1. its environment variable (e.g. ``OPENAI_API_KEY``), which may come from a
   ``.env`` file that git ignores;
2. the operating system's credential store via :mod:`keyring` — macOS
   Keychain, Windows Credential Manager, or Secret Service / KWallet on Linux;
3. a key entered in the web UI *for this session only*, kept in memory when no
   secure store is available (it is gone when the server stops).

Keys are never written to the SQLite database, never logged, and never sent
back to the browser: the UI only ever receives a masked hint such as
``sk-…a1b2`` and where the key came from.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

SERVICE = "lucidfish"
_session_keys: dict[str, str] = {}
_cache: dict[str, str | None] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class KeyInfo:
    key: str | None
    source: str          # "env", "keychain", "session" or "" (missing)
    env_var: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.key)

    def public(self) -> dict:
        """Safe-to-share description (never includes the key itself)."""
        return {"present": self.present, "source": self.source, "hint": mask(self.key),
                "env_var": self.env_var}


def _keyring():
    """The keyring module if a *secure* backend is usable, else None."""
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception:
        return None
    cls = type(backend)
    name, module = cls.__name__.lower(), cls.__module__
    insecure = ("fail" in module or "null" in module or "plaintext" in name
                or module.startswith("keyrings.alt"))
    if insecure or (hasattr(backend, "backends") and not backend.backends):
        return None
    return keyring


def secure_store_name() -> str | None:
    """Human name of the OS credential store in use, or None if there isn't one."""
    kr = _keyring()
    if kr is None:
        return None
    try:
        backend = kr.get_keyring()
        if hasattr(backend, "backends") and backend.backends:
            backend = backend.backends[0]
        module = type(backend).__module__.lower()
    except Exception:
        return None
    if "macos" in module:
        return "macOS Keychain"
    if "windows" in module:
        return "Windows Credential Manager"
    if "secretservice" in module or "libsecret" in module:
        return "Secret Service (GNOME Keyring)"
    if "kwallet" in module:
        return "KWallet"
    return "system keyring"


def _keyring_get(provider: str) -> str | None:
    if provider in _cache:
        return _cache[provider]
    kr = _keyring()
    value = None
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, provider)
        except Exception:
            value = None
    _cache[provider] = value
    return value


def get_key(provider: str, env_var: str | None) -> KeyInfo:
    """Resolve the API key for `provider` (see module docstring for the order)."""
    if env_var and os.environ.get(env_var):
        return KeyInfo(os.environ[env_var].strip(), "env", env_var)
    with _lock:
        stored = _keyring_get(provider)
        if stored:
            return KeyInfo(stored, "keychain", env_var)
        if _session_keys.get(provider):
            return KeyInfo(_session_keys[provider], "session", env_var)
    return KeyInfo(None, "", env_var)


def validate_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise ValueError("The API key is empty.")
    if len(key) > 512 or any(ch.isspace() for ch in key):
        raise ValueError("That doesn't look like an API key (it contains spaces or is too long).")
    return key


def set_key(provider: str, key: str) -> str:
    """Store a key. Returns where it went: "keychain" or "session"."""
    key = validate_key(key)
    with _lock:
        kr = _keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, provider, key)
                _cache[provider] = key
                _session_keys.pop(provider, None)
                return "keychain"
            except Exception:
                pass  # e.g. headless Linux without a running Secret Service
        _session_keys[provider] = key
        return "session"


def delete_key(provider: str) -> None:
    with _lock:
        _session_keys.pop(provider, None)
        _cache.pop(provider, None)
        kr = _keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, provider)
            except Exception:
                pass  # nothing stored


def mask(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "•" * 6
    return f"{key[:3]}…{key[-4:]}"


def redact(text: str, *secrets: str | None) -> str:
    """Remove secrets from text before it is shown or logged (some APIs echo keys in errors)."""
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, mask(s))
    return text
