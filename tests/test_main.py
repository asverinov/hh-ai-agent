import asyncio
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_analyzer import SuitabilityResult, VacancyAnalyzer
from config import load_settings
from database import Database, VacancyStatus
from hh_client import CompanyDetails, PageState, VacancyDetails, VacancySearchResult, VacancySummary
from llm.errors import LLMAuthenticationError
from llm.managed import ManagedLLMProvider
from llm.providers.fake import FakeProvider
from main import VacancyProcessResult, SearchRunStats, process_vacancy, run_search_cycle
from tests.test_config import VALID_ENV, write_profile
from tg_bot import AgentControl


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
SUMMARY = VacancySummary(
    "job-1", "Python developer", "https://example.com/vacancy/job-1", "Python"
)


class FakeHHClient:
    def __init__(
        self,
        details: VacancyDetails,
        search_results: list[VacancySearchResult] | None = None,
    ):
        self.details = details
        self.search_results = search_results or []
        self.read_calls = 0
        self.search_calls = 0
        self.message_checks = 0

    async def read_vacancy(self, summary, captcha_solver=None):
        self.read_calls += 1
        return self.details

    async def search_vacancies(self, query, areas, experience_filters):
        self.search_calls += 1
        return (
            self.search_results.pop(0)
            if self.search_results
            else VacancySearchResult([], 0, 0)
        )

    async def check_messages(self, notify):
        self.message_checks += 1

    async def read_company_details(self, company_url: str):
        return CompanyDetails(4.7, 128)


class FakeAnalyzer:
    def __init__(self):
        self.assess_calls = 0

    async def assess(self, title: str, description: str) -> SuitabilityResult:
        self.assess_calls += 1
        return SuitabilityResult(
            suitable=True,
            confidence=0.91,
            reason="Relevant work",
            fit_points=[{"category": "Навыки", "text": "Python"}],
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
        self.notifications.append(f"analysis_failed:{error_type}")

    async def request_captcha(self, screenshot, title: str, timeout_seconds: int):
        return None


def settings(tmp_path: Path, mode: str):
    loaded = load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, "TG_USER_ID": "42", "APP_MODE": mode},
    )
    return replace(loaded, database_path=tmp_path / "agent.db")


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
    assert vacancy.fit_summary.startswith("Навыки: Python")
    assert vacancy.company_rating == 4.7
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


<<<<<<< HEAD
def test_pending_vacancy_is_resent_without_hh_or_llm_calls(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, "approval")
    database = Database(app_settings.database_path)
    database.init()
    assert database.discover(
        job_id=SUMMARY.id,
        title=SUMMARY.title,
        company="Example",
        url=SUMMARY.url,
        description_hash="hash",
        search_query=SUMMARY.search_query,
        discovered_at=NOW,
    )
    assert database.request_approval(
        job_id=SUMMARY.id,
        cover_letter="Letter",
        llm_decision=True,
        llm_reason="Relevant",
        confidence=0.9,
        now=NOW,
    )
    hh_client = FakeHHClient(
        VacancyDetails(SUMMARY, PageState.VACANCY_LOADED, "Example", "Description")
    )
    analyzer = FakeAnalyzer()
    telegram = FakeTelegram()

    result = asyncio.run(
        process_vacancy(
            replace(SUMMARY, previously_sent=True),
            app_settings,
            database,
            hh_client,
            analyzer,
            telegram,
            now_factory=lambda: NOW,
        )
    )

    assert result.outcome == "telegram_card"
    assert hh_client.read_calls == 0
    assert analyzer.assess_calls == 0
    assert database.get(SUMMARY.id).status is VacancyStatus.PENDING_APPROVAL
    assert telegram.previews == [(SUMMARY.id, True)]


def test_search_cycle_resends_pending_card_after_review_interval(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path, "approval")
    database = Database(app_settings.database_path)
    database.init()
    assert database.discover(
        job_id=SUMMARY.id,
        title=SUMMARY.title,
        company="Example",
        url=SUMMARY.url,
        description_hash="hash",
        search_query=SUMMARY.search_query,
        discovered_at=NOW,
    )
    assert database.request_approval(
        job_id=SUMMARY.id,
        cover_letter="Letter",
        llm_decision=True,
        llm_reason="Relevant",
        confidence=0.9,
        now=NOW,
    )
    repeated = replace(SUMMARY, previously_sent=True)
    hh_client = FakeHHClient(
        VacancyDetails(SUMMARY, PageState.VACANCY_LOADED),
        [VacancySearchResult([repeated], found_results=1, duplicates=1)],
    )
    telegram = FakeTelegram()

    run = asyncio.run(
        run_search_cycle(
            app_settings,
            database,
            hh_client,
            FakeAnalyzer(),
            telegram,
            AgentControl(),
            now_factory=lambda: NOW + timedelta(minutes=31),
        )
    )

    assert run.telegram_cards == 1
    assert database.get(SUMMARY.id).status is VacancyStatus.PENDING_APPROVAL
    assert telegram.previews == [(SUMMARY.id, True)]


def test_search_cycle_resumes_interrupted_discovered_row(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, "approval")
    database = Database(app_settings.database_path)
    database.init()
    assert database.discover(
        job_id=SUMMARY.id,
        title=SUMMARY.title,
        company="Example",
        url=SUMMARY.url,
        description_hash="",
        search_query=SUMMARY.search_query,
        discovered_at=NOW,
    )
    hh_client = FakeHHClient(
        VacancyDetails(SUMMARY, PageState.VACANCY_LOADED, "Example", "Description")
    )
    telegram = FakeTelegram()

    run = asyncio.run(
        run_search_cycle(
            app_settings,
            database,
            hh_client,
            FakeAnalyzer(),
            telegram,
            AgentControl(),
            now_factory=lambda: NOW,
        )
    )

    assert database.get(SUMMARY.id).status is VacancyStatus.PENDING_APPROVAL
    assert hh_client.read_calls == 1
    assert run.telegram_cards == 1


def test_search_cycle_saves_counts_and_notifies_when_no_cards(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, "dry_run")
    database = Database(app_settings.database_path)
    database.init()
    hh_client = FakeHHClient(
        VacancyDetails(SUMMARY, PageState.VACANCY_LOADED),
        [VacancySearchResult([], found_results=7, duplicates=7)],
    )
    telegram = FakeTelegram()

    run = asyncio.run(
        run_search_cycle(
            app_settings,
            database,
            hh_client,
            FakeAnalyzer(),
            telegram,
            AgentControl(),
            now_factory=lambda: NOW,
        )
    )

    assert run.state == "completed"
    assert run.query_count == 1
    assert run.found_results == 7
    assert run.duplicates == 7
    assert run.telegram_cards == 0
    assert len(telegram.notifications) == 1


def test_consecutive_search_errors_open_circuit_breaker(tmp_path: Path) -> None:
    app_settings = replace(
        settings(tmp_path, "dry_run"), circuit_breaker_page_errors=2
    )
    database = Database(app_settings.database_path)
    database.init()
    hh_client = FakeHHClient(
        VacancyDetails(SUMMARY, PageState.VACANCY_LOADED),
        [
            VacancySearchResult([], 0, 0, "search_error:TimeoutError"),
            VacancySearchResult([], 0, 0, "search_error:TimeoutError"),
        ],
    )
    telegram = FakeTelegram()
    control = AgentControl()

    first = asyncio.run(
        run_search_cycle(
            app_settings,
            database,
            hh_client,
            FakeAnalyzer(),
            telegram,
            control,
            now_factory=lambda: NOW,
        )
    )
    second = asyncio.run(
        run_search_cycle(
            app_settings,
            database,
            hh_client,
            FakeAnalyzer(),
            telegram,
            control,
            now_factory=lambda: NOW,
        )
    )

    assert first.state == "completed"
    assert second.state == "paused_by_circuit_breaker"
    assert second.circuit_reason == "search_errors"
    assert control.paused


def test_search_run_stats_opens_for_repeated_page_structure_changes(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path, "dry_run")
    stats = SearchRunStats()

    for _ in range(app_settings.circuit_breaker_page_errors):
        stats.record(
            VacancyProcessResult(
                "other_error",
                "page_structure_changed",
                PageState.PAGE_STRUCTURE_CHANGED,
            )
        )

    assert stats.circuit_reason(app_settings) == "page_structure_changed"


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
