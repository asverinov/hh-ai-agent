from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from config import LLMSettings
from database import Database
from llm.base import ProviderAdapter
from llm.errors import LLMConfigurationError
from llm.providers.mistral import MistralProvider


_FINGERPRINT_DOMAIN = b"mistral-key-fingerprint\0"


@dataclass(frozen=True)
class ProtectedMistralKey:
    encrypted_key: str = field(repr=False)
    key_hmac: str = field(repr=False)
    suffix: str


@dataclass(frozen=True)
class MistralKeyView:
    id: int
    suffix: str
    status: str
    cooldown_until: datetime | None
    last_checked_at: datetime | None
    last_error_type: str
    is_current: bool


@dataclass(frozen=True)
class MistralKeyCheckResult:
    key: MistralKeyView
    success: bool
    error_type: str


class MistralKeyCipher:
    def __init__(self, master_key: str) -> None:
        encoded_key = master_key.encode()
        self._fernet = Fernet(encoded_key)
        self._hmac_key = base64.urlsafe_b64decode(encoded_key)

    def protect(self, api_key: str) -> ProtectedMistralKey:
        raw = api_key.encode()
        return ProtectedMistralKey(
            encrypted_key=self._fernet.encrypt(raw).decode(),
            key_hmac=hmac.new(
                self._hmac_key,
                _FINGERPRINT_DOMAIN + raw,
                hashlib.sha256,
            ).hexdigest(),
            suffix=api_key[-4:],
        )

    def reveal(self, encrypted_key: str) -> str:
        try:
            return self._fernet.decrypt(encrypted_key.encode()).decode()
        except InvalidToken as exc:
            raise LLMConfigurationError(
                "MISTRAL_KEYS_MASTER_KEY cannot decrypt stored keys"
            ) from exc


class MistralKeyManager:
    def __init__(
        self,
        settings: LLMSettings,
        database: Database,
        *,
        adapter_factory: Callable[[str], ProviderAdapter] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_factory: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        notifier: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self._cipher = MistralKeyCipher(settings.mistral_keys_master_key)
        self._adapter_factory = adapter_factory or (
            lambda api_key: MistralProvider(api_key, settings.mistral_base_url)
        )
        self._sleep = sleep
        self._now = now_factory
        self._notifier = notifier

        protected = (
            self._cipher.protect(settings.mistral_api_key)
            if settings.mistral_api_key
            else None
        )
        database.import_legacy_mistral_key(
            encrypted_key=None if protected is None else protected.encrypted_key,
            key_hmac=None if protected is None else protected.key_hmac,
            suffix=None if protected is None else protected.suffix,
            now=self._now(),
        )
