import asyncio
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import main as main_module
from ai_analyzer import SuitabilityResult, VacancyAnalyzer
from config import load_settings
from database import Database, VacancyStatus
from hh_client import PageState, VacancyDetails, VacancySummary
from llm.errors import LLMAuthenticationError
from llm.managed import ManagedLLMProvider
from llm.providers.fake import FakeProvider
from main import process_vacancy
from tests.test_config import VALID_ENV, write_profile


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
SUMMARY = VacancySummary(
    "job-1", "Python developer", "https://example.com/vacancy/job-1", "Python"
)


class FakeHHClient:
    def __init__(self, details: VacancyDetails):
        self.details = details

    async def read_vacancy(self, summary, captcha_solver=None):
        return self.details


class FakeAnalyzer:
    async def assess(self, title: str, description: str) -> SuitabilityResult:
        return SuitabilityResult(
            suitable=True, confidence=0.91, reason="Relevant work"
        )

    async def generate_cover_letter(self, title: str, description: str) -> str:
        return "Safe local-profile letter"


class FakeTelegram:
    def __init__(self):
        self.previews: list[tuple[str, bool]] = []
        self.notifications: list[str] = []

    async def send_preview(self, vacancy, include_actions: bool):
        self.previews.append((vacancy.id, include_actions))

    async def notify(self, text: str):
        self.notifications.append(text)

    async def notify_analysis_failed(self, title: str, url: str, error_type: str):
        self.notifications.append(error_type)

    async def request_captcha(self, screenshot, title: str, timeout_seconds: int):
        return None


def settings(tmp_path: Path, mode: str):
    loaded = load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, "TG_USER_ID": "42", "APP_MODE": mode},
    )
    return replace(loaded, database_path=tmp_path / "agent.db")


def test_run_shares_mistral_manager_and_assigns_telegram_notifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_settings = settings(tmp_path, "approval")
    captured: dict[str, object] = {}

    class FakeBackend:
        async def start(self):
            return object()

        async def close(self):
            return None

    class FakeMistralManager:
        def __init__(self):
            self.notifier = None

        def set_notifier(self, notifier):
            self.notifier = notifier

        async def close(self):
            return None

    manager = FakeMistralManager()

    class FakeHHClient:
        def __init__(self, *_args):
            pass

        async def ensure_login(self) -> bool:
            return True

    class FakeAnalyzer:
        def __init__(self, _settings, provider):
            captured["analyzer_provider"] = provider

    class FakeTelegramService:
        def __init__(
            self,
            _settings,
            _database,
            _approval_service,
            _control,
            *,
            mistral_keys,
            **_kwargs,
        ):
            captured["telegram"] = self
            captured["telegram_manager"] = mistral_keys

        async def notify(self, _text: str) -> None:
            return None

        async def start_polling(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def fake_agent_loop(*_args) -> None:
        return None

    monkeypatch.setattr(main_module, "MistralKeyManager", FakeMistralManager, raising=False)
    monkeypatch.setattr(main_module, "create_browser_backend", lambda _settings: FakeBackend())
    monkeypatch.setattr(main_module, "create_llm_provider", lambda *_args: manager)
    monkeypatch.setattr(main_module, "HHClient", FakeHHClient)
    monkeypatch.setattr(main_module, "VacancyAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(main_module, "TelegramService", FakeTelegramService)
    monkeypatch.setattr(main_module, "agent_loop", fake_agent_loop)

    asyncio.run(main_module.run(app_settings))

    telegram = captured["telegram"]
    assert captured["analyzer_provider"] is manager
    assert captured["telegram_manager"] is manager
    assert manager.notifier == telegram.notify


def test_dry_run_records_and_previews_without_pending_actions(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, "dry_run")
    database = Database(app_settings.database_path)
    database.init()
    telegram = FakeTelegram()
    details = VacancyDetails(
        SUMMARY, PageState.VACANCY_LOADED, "Example", "Build Python services"
    )

    asyncio.run(
        process_vacancy(
            SUMMARY,
            app_settings,
            database,
            FakeHHClient(details),
            FakeAnalyzer(),
            telegram,
            now_factory=lambda: NOW,
        )
    )

    vacancy = database.get("job-1")
    assert vacancy.status is VacancyStatus.DISCOVERED
    assert vacancy.cover_letter == "Safe local-profile letter"
    assert telegram.previews == [("job-1", False)]


def test_approval_mode_records_pending_and_sends_actions(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, "approval")
    database = Database(app_settings.database_path)
    database.init()
    telegram = FakeTelegram()
    details = VacancyDetails(
        SUMMARY, PageState.VACANCY_LOADED, "Example", "Build Python services"
    )

    asyncio.run(
        process_vacancy(
            SUMMARY,
            app_settings,
            database,
            FakeHHClient(details),
            FakeAnalyzer(),
            telegram,
            now_factory=lambda: NOW,
        )
    )

    assert database.get("job-1").status is VacancyStatus.PENDING_APPROVAL
    assert telegram.previews == [("job-1", True)]


def test_browser_read_error_is_persisted_as_apply_failed(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, "dry_run")
    database = Database(app_settings.database_path)
    database.init()
    telegram = FakeTelegram()
    details = VacancyDetails(
        SUMMARY, PageState.NETWORK_ERROR, error="navigation failed"
    )

    asyncio.run(
        process_vacancy(
            SUMMARY,
            app_settings,
            database,
            FakeHHClient(details),
            FakeAnalyzer(),
            telegram,
            now_factory=lambda: NOW,
        )
    )

    vacancy = database.get("job-1")
    assert vacancy.status is VacancyStatus.APPLY_FAILED
    assert "navigation failed" in vacancy.error_text


def test_llm_failure_records_analysis_failure_without_requesting_approval(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path, "approval")
    database = Database(app_settings.database_path)
    database.init()
    provider = ManagedLLMProvider(
        FakeProvider([LLMAuthenticationError()]),
        database,
        max_retries=1,
        max_requests_per_day=100,
    )
    telegram = FakeTelegram()
    details = VacancyDetails(
        SUMMARY, PageState.VACANCY_LOADED, "Example", "Build Python services"
    )

    asyncio.run(
        process_vacancy(
            SUMMARY,
            app_settings,
            database,
            FakeHHClient(details),
            VacancyAnalyzer(app_settings, provider),
            telegram,
            now_factory=lambda: NOW,
        )
    )

    vacancy = database.get("job-1")
    assert vacancy.status is VacancyStatus.ANALYSIS_FAILED
    assert vacancy.error_text == "authentication"
    assert vacancy.cover_letter == ""
    assert telegram.previews == []


def test_cli_reports_configuration_error_without_traceback(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--check-config",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--profile",
            str(tmp_path / "missing.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.startswith("Configuration error:")
    assert "Traceback" not in result.stderr
