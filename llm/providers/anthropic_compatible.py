from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp

from llm.errors import (
    LLMError,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMTransientError,
    error_for_http_status,
)
from llm.types import LLMRequest, LLMResponse


class AnthropicCompatibleProvider:
    name = "anthropic_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        json_mode: bool = False,
        reasoning_enabled: bool | None = None,
        session: Any = None,
    ):
        self.url = f"{base_url.rstrip('/')}/v1/messages"
        self.api_key = api_key
        self.json_mode = json_mode
        self.reasoning_enabled = reasoning_enabled
        self._session = session
        self._owns_session = session is None

    def _get_session(self) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system_instructions = request.system_instructions
        if request.json_schema is not None:
            system_instructions += (
                "\nReturn only a JSON object matching this JSON schema:\n"
                + json.dumps(request.json_schema, ensure_ascii=False)
            )
        payload: dict[str, object] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": system_instructions,
            "messages": [
                {"role": "user", "content": request.user_content},
            ],
            "temperature": request.temperature,
        }
        started = time.perf_counter()
        try:
            async with self._get_session().post(
                self.url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=request.timeout_seconds),
            ) as response:
                if not 200 <= response.status < 300:
                    raise error_for_http_status(
                        response.status, response.headers.get("Retry-After")
                    )
                try:
                    data = await response.json()
                except (TypeError, ValueError) as exc:
                    raise LLMInvalidResponseError() from exc
        except LLMError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise LLMTimeoutError() from exc
        except aiohttp.ClientError as exc:
            raise LLMTransientError() from exc
        try:
            content_blocks = data["content"]
            text = "".join(
                block.get("text", "")
                for block in content_blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise LLMInvalidResponseError() from exc
        if not isinstance(text, str) or not text.strip():
            raise LLMInvalidResponseError()
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        stop_reason = data.get("stop_reason")
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.get("model") if isinstance(data.get("model"), str) else request.model,
            request_id=data.get("id") if isinstance(data.get("id"), str) else None,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=usage.get("input_tokens")
            if isinstance(usage.get("input_tokens"), int)
            else None,
            output_tokens=usage.get("output_tokens")
            if isinstance(usage.get("output_tokens"), int)
            else None,
            finish_reason=stop_reason if isinstance(stop_reason, str) else None,
        )

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
