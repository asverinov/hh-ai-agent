from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypeVar

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

from config import LLMSettings
from database import Database, MistralKeyRow
from llm.base import ProviderAdapter
from llm.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMNoAvailableKeysError,
    LLMPermissionError,
    LLMRateLimitError,
    MistralKeyDuplicateError,
    MistralKeyInputError,
)
from llm.managed import ManagedLLMProvider
from llm.providers.mistral import MistralProvider
from llm.types import LLMRequest, LLMResponse


_FINGERPRINT_DOMAIN = b"mistral-key-fingerprint\0"
logger = logging.getLogger(__name__)
Result = TypeVar("Result")
StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


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
        self._lock = asyncio.Lock()
        self._current_id: int | None = None
        self._current_provider: ManagedLLMProvider | None = None
        self._unavailable_notified = False

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

    def set_notifier(self, notifier: Callable[[str], Awaitable[None]]) -> None:
        self._notifier = notifier

    def list_keys(self) -> tuple[MistralKeyView, ...]:
        return tuple(self._view(row) for row in self.database.mistral_keys(self._now()))

    async def generate_text(self, request: LLMRequest) -> LLMResponse:
        return await self._execute(lambda provider: provider.generate_text(request))

    async def generate_structured(
        self,
        request: LLMRequest,
        response_model: type[StructuredResult],
    ) -> tuple[LLMResponse, StructuredResult]:
        return await self._execute(
            lambda provider: provider.generate_structured(request, response_model)
        )

    async def _execute(
        self,
        operation: Callable[[ManagedLLMProvider], Awaitable[Result]],
    ) -> Result:
        async with self._lock:
            attempted: set[int] = set()
            while True:
                key = self._select_key(excluding=attempted)
                if key is None:
                    await self._notify_unavailable_once()
                    raise LLMNoAvailableKeysError()
                attempted.add(key.id)
                provider = await self._activate(key)
                try:
                    result = await operation(provider)
                except (LLMAuthenticationError, LLMPermissionError) as exc:
                    await self._disable_current(key, exc.category)
                    continue
                except LLMRateLimitError as exc:
                    await self._cooldown_current(key, exc)
                    continue
                self._unavailable_notified = False
                return result

    def _select_key(self, *, excluding: set[int]) -> MistralKeyRow | None:
        ready = [
            key
            for key in self.database.mistral_keys(self._now())
            if key.status == "ready" and key.id not in excluding
        ]
        if not ready:
            return None
        self._unavailable_notified = False
        if self._current_id is not None:
            current = next((key for key in ready if key.id == self._current_id), None)
            if current is not None:
                return current
        return ready[0]

    async def _activate(self, key: MistralKeyRow) -> ManagedLLMProvider:
        if self._current_id == key.id and self._current_provider is not None:
            return self._current_provider
        await self._close_current()
        adapter = self._adapter_factory(self._cipher.reveal(key.encrypted_key))
        provider = ManagedLLMProvider(
            adapter,
            self.database,
            max_retries=self.settings.max_retries,
            max_requests_per_day=self.settings.max_requests_per_day,
            sleep=self._sleep,
            now_factory=self._now,
        )
        self._current_id = key.id
        self._current_provider = provider
        return provider

    async def _disable_current(self, key: MistralKeyRow, error_type: str) -> None:
        now = self._now()
        self.database.update_mistral_key_state(
            key.id,
            status="disabled",
            cooldown_until=None,
            last_checked_at=now,
            last_error_type=error_type,
            now=now,
        )
        await self._close_current()

    async def _cooldown_current(
        self, key: MistralKeyRow, error: LLMRateLimitError
    ) -> None:
        now = self._now()
        seconds = error.retry_after_seconds
        cooldown_until = (
            now + timedelta(seconds=seconds)
            if seconds is not None and math.isfinite(seconds) and seconds > 0
            else now + timedelta(hours=1)
        )
        self.database.update_mistral_key_state(
            key.id,
            status="cooldown",
            cooldown_until=cooldown_until,
            last_checked_at=now,
            last_error_type=error.category,
            now=now,
        )
        await self._close_current()

    async def add_key(self, raw: str) -> MistralKeyView:
        if not 16 <= len(raw) <= 512 or any(character.isspace() for character in raw):
            raise MistralKeyInputError()
        protected = self._cipher.protect(raw)
        async with self._lock:
            if self.database.mistral_key_by_hmac(protected.key_hmac, self._now()):
                raise MistralKeyDuplicateError()
            error = await self._health_error(raw)
            if error is not None:
                raise error
            now = self._now()
            key_id = self.database.add_mistral_key(
                encrypted_key=protected.encrypted_key,
                key_hmac=protected.key_hmac,
                suffix=protected.suffix,
                now=now,
            )
            if key_id is None:
                raise MistralKeyDuplicateError()
            self._unavailable_notified = False
            row = self.database.mistral_key(key_id, now)
            assert row is not None
            return self._view(row)

    async def check_key(self, key_id: int) -> MistralKeyCheckResult | None:
        async with self._lock:
            return await self._check_key_locked(key_id)

    async def _check_key_locked(
        self, key_id: int
    ) -> MistralKeyCheckResult | None:
        row = self.database.mistral_key(key_id, self._now())
        if row is None:
            return None
        error = await self._health_error(self._cipher.reveal(row.encrypted_key))
        now = self._now()
        if error is None:
            status = "ready"
            cooldown_until = None
            error_type = ""
            self._unavailable_notified = False
        elif isinstance(error, (LLMAuthenticationError, LLMPermissionError)):
            status = "disabled"
            cooldown_until = None
            error_type = error.category
        elif isinstance(error, LLMRateLimitError):
            status = "cooldown"
            seconds = error.retry_after_seconds
            cooldown_until = (
                now + timedelta(seconds=seconds)
                if seconds is not None and math.isfinite(seconds) and seconds > 0
                else now + timedelta(hours=1)
            )
            error_type = error.category
        else:
            status = row.status
            cooldown_until = (
                datetime.fromisoformat(row.cooldown_until)
                if row.cooldown_until is not None
                else None
            )
            error_type = error.category
        self.database.update_mistral_key_state(
            key_id,
            status=status,
            cooldown_until=cooldown_until,
            last_checked_at=now,
            last_error_type=error_type,
            now=now,
        )
        if status != "ready" and self._current_id == key_id:
            await self._close_current()
        checked = self.database.mistral_key(key_id, now)
        assert checked is not None
        return MistralKeyCheckResult(
            key=self._view(checked),
            success=error is None,
            error_type=error_type,
        )

    async def check_all(self) -> tuple[MistralKeyCheckResult, ...]:
        async with self._lock:
            results: list[MistralKeyCheckResult] = []
            for key in self.database.mistral_keys(self._now()):
                result = await self._check_key_locked(key.id)
                if result is not None:
                    results.append(result)
            return tuple(results)

    async def delete_key(self, key_id: int) -> bool:
        async with self._lock:
            deleted = self.database.delete_mistral_key(key_id)
            if self._current_id == key_id:
                await self._close_current()
            return deleted

    async def close(self) -> None:
        async with self._lock:
            await self._close_current()

    async def _health_error(self, raw: str) -> LLMError | None:
        provider = ManagedLLMProvider(
            self._adapter_factory(raw),
            self.database,
            max_retries=0,
            max_requests_per_day=self.settings.max_requests_per_day,
            sleep=self._sleep,
            now_factory=self._now,
        )
        try:
            await provider.generate_text(self._health_request())
        except LLMError as exc:
            return exc
        finally:
            await self._close_provider(provider)
        return None

    def _health_request(self) -> LLMRequest:
        return LLMRequest(
            system_instructions="Return only OK.",
            user_content="Reply OK.",
            model=self.settings.model,
            temperature=0,
            max_output_tokens=min(self.settings.max_output_tokens, 8),
            timeout_seconds=self.settings.timeout_seconds,
            operation="mistral_key_healthcheck",
        )

    async def _close_current(self) -> None:
        provider = self._current_provider
        self._current_id = None
        self._current_provider = None
        if provider is not None:
            await self._close_provider(provider)

    @staticmethod
    async def _close_provider(provider: ManagedLLMProvider) -> None:
        try:
            await provider.close()
        except Exception as exc:
            logger.warning(
                "mistral_key_provider_close_failed error_type=%s",
                type(exc).__name__,
            )

    async def _notify_unavailable_once(self) -> None:
        if self._unavailable_notified:
            return
        self._unavailable_notified = True
        if self._notifier is None:
            return
        try:
            await self._notifier("No available Mistral API keys")
        except Exception as exc:
            logger.warning(
                "mistral_keys_unavailable_notification_failed error_type=%s",
                type(exc).__name__,
            )

    def _view(self, row: MistralKeyRow) -> MistralKeyView:
        return MistralKeyView(
            id=row.id,
            suffix=row.suffix,
            status=row.status,
            cooldown_until=(
                datetime.fromisoformat(row.cooldown_until)
                if row.cooldown_until is not None
                else None
            ),
            last_checked_at=(
                datetime.fromisoformat(row.last_checked_at)
                if row.last_checked_at is not None
                else None
            ),
            last_error_type=row.last_error_type,
            is_current=row.id == self._current_id,
        )
