from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from database import Database
from llm.base import ProviderAdapter
from llm.errors import (
    LLMDailyLimitError,
    LLMError,
    LLMBlacklistError,
    LLMInvalidResponseError,
    LLMRateLimitError,
)
from llm.types import LLMRequest, LLMResponse


logger = logging.getLogger(__name__)
StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


class ManagedLLMProvider:
    def __init__(
        self,
        adapter: ProviderAdapter,
        database: Database,
        *,
        max_retries: int,
        max_rate_limit_retries: int | None = None,
        max_requests_per_day: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_factory: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.adapter = adapter
        self.database = database
        self.max_retries = max_retries
        self.max_rate_limit_retries = (
            max_retries
            if max_rate_limit_retries is None
            else max_rate_limit_retries
        )
        self.max_requests_per_day = max_requests_per_day
        self._sleep = sleep
        self._now = now_factory

    async def generate_text(self, request: LLMRequest) -> LLMResponse:
        response, _ = await self._generate(request, None)
        return response

    async def generate_structured(
        self,
        request: LLMRequest,
        response_model: type[StructuredResult],
    ) -> tuple[LLMResponse, StructuredResult]:
        response, parsed = await self._generate(request, response_model)
        assert parsed is not None
        return response, parsed

    async def _generate(
        self,
        request: LLMRequest,
        response_model: type[StructuredResult] | None,
    ) -> tuple[LLMResponse, StructuredResult | None]:
        invalid_responses = 0
        active_request = request
        # prompt_version = хеш только от system_instructions.
        # Семантика: все вызовы с одним промптом имеют одинаковый pv,
        # меняется промпт → меняется pv. user_content (конкретная вакансия)
        # в хеш НЕ входит — иначе pv был бы уникален для каждой вакансии
        # и не годился бы для группировки.
        prompt_version = hashlib.sha256(
            request.system_instructions.encode("utf-8")
        ).hexdigest()[:12]
        for attempt in range(self.max_retries + 1):
            row_id = self.database.reserve_llm_request(
                provider=self.adapter.name,
                model=request.model,
                operation=request.operation,
                now=self._now(),
                daily_limit=self.max_requests_per_day,
                vacancy_id=request.vacancy_id,
                prompt_version=prompt_version,
            )
            if row_id is None:
                raise LLMDailyLimitError()
            try:
                response = await self.adapter.complete(active_request)
                logger.debug(
                    "llm_response_meta finish_reason=%r text_len=%s",
                    response.finish_reason,
                    len(response.text or ""),
                )
                # Blacklist = только явный сигнал от API (finish_reason).
                # Текстовые boilerplate-отказы GigaChat приходят с finish_reason=end_turn
                # и должны уходить в retryable invalid_response, чтобы был шанс на retry.
                if response.finish_reason == "blacklist":
                    logger.warning(
                        "llm_blacklist_skip provider=%s operation=%s finish_reason=blacklist raw_head=%r",
                        self.adapter.name, request.operation, (response.text or "")[:200],
                    )
                    raise LLMBlacklistError()
                if not response.text.strip() or response.finish_reason == "length":
                    raise LLMInvalidResponseError()
                parsed = None
                if response_model is not None:
                    raw_text = response.text.strip()
                    import re as _re
                    raw_text = _re.sub(r"```(?:json)?", "", raw_text).strip()
                    first_brace = raw_text.find("{")
                    last_brace = raw_text.rfind("}")
                    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                        raw_text = raw_text[first_brace : last_brace + 1]
                    import re as _re
                    # Pre-fix: лишняя скобка "), -> ", (частая глючка GigaChat)
                    raw_text = _re.sub(r'"\)\s*,', '",', raw_text)
                    # Fix missing ']' before "verdict" (GigaChat glitch):
                    #   ...{"level": N}}, "verdict": ...
                    # -> ...{"level": N}], "verdict": ...
                    raw_text = _re.sub(
                        r'\}\s*\}\s*,\s*("verdict"\s*:)',
                        r'}], \1',
                        raw_text,
                    )

                    parsed = None
                    last_err: Exception | None = None

                    # Attempt 1: strict
                    try:
                        parsed = response_model.model_validate_json(raw_text)
                    except Exception as e:
                        last_err = e

                    # Attempt 2: json-repair (лучший для LLM)
                    if parsed is None:
                        try:
                            from json_repair import repair_json as _repair
                            fixed = _repair(raw_text, return_objects=False)
                            parsed = response_model.model_validate_json(fixed)
                        except Exception as e:
                            last_err = e

                    # Attempt 3: dirtyjson
                    if parsed is None:
                        try:
                            import dirtyjson as _dirtyjson
                            obj = _dirtyjson.loads(raw_text)
                            parsed = response_model.model_validate(obj)
                        except Exception as e:
                            last_err = e

                    # Attempt 4: python-dict fixes
                    if parsed is None:
                        try:
                            import json as _json
                            fixed = raw_text.replace("'", '"').replace("None", "null")
                            fixed = fixed.replace("True", "true").replace("False", "false")
                            parsed = response_model.model_validate_json(fixed)
                        except Exception as e:
                            last_err = e

                    if parsed is None:
                        logger.warning(
                            "json_parse_failed raw_head=%r raw_tail=%r err=%s",
                            raw_text[:200], raw_text[-200:], last_err,
                        )
                        raise LLMInvalidResponseError() from last_err
                self.database.complete_llm_request(
                    row_id,
                    success=True,
                    finished_at=self._now(),
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=response.latency_ms,
                    provider_request_id=response.request_id,
                    raw_response=response.text,
                )
                logger.info(
                    "llm_request provider=%s model=%s operation=%s success=true "
                    "latency_ms=%s request_id=%s input_tokens=%s output_tokens=%s",
                    self.adapter.name,
                    response.model,
                    request.operation,
                    response.latency_ms,
                    response.request_id or "",
                    response.input_tokens,
                    response.output_tokens,
                )
                return response, parsed
            except LLMError as exc:
                if isinstance(exc, LLMInvalidResponseError):
                    invalid_responses += 1
                    if invalid_responses == 1:
                        active_request = replace(
                            request,
                            system_instructions=(
                                f"{request.system_instructions}\nThe previous response was "
                                "invalid. Return only content matching the requested format."
                            ),
                        )
                self.database.complete_llm_request(
                    row_id,
                    success=False,
                    finished_at=self._now(),
                    error_type=exc.category,
                )
                logger.warning(
                    "llm_request provider=%s model=%s operation=%s success=false error_type=%s",
                    self.adapter.name,
                    request.model,
                    request.operation,
                    exc.category,
                )
                retry_limit = (
                    self.max_rate_limit_retries
                    if isinstance(exc, LLMRateLimitError)
                    else self.max_retries
                )
                if (
                    not exc.retryable
                    or attempt >= retry_limit
                    or invalid_responses > 1
                ):
                    raise
                await self._sleep(self._retry_delay(exc, attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(error: LLMError, attempt: int) -> float:
        if (
            isinstance(error, LLMRateLimitError)
            and error.retry_after_seconds is not None
            and math.isfinite(error.retry_after_seconds)
            and error.retry_after_seconds >= 0
        ):
            return max(0.5, min(error.retry_after_seconds, 5.0))
        return min(0.5 * (2**attempt), 5.0)

    async def close(self) -> None:
        await self.adapter.close()
