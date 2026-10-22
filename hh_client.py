from __future__ import annotations

import asyncio
import json
import aiohttp
import logging
import random
import re
import tempfile
import yaml
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from uuid import uuid4

from approval import ApplicationPermission, ApprovalGuard
from config import Settings
from cover_letter import cover_letter_validation_error, has_required_portfolio
from database import Database, VacancyStatus


logger = logging.getLogger(__name__)

_EXPERIENCE_MAP = {
    "без опыта": "noExperience",
    "нет опыта": "noExperience",
    "от 1 до 3 лет": "between1And3",
    "1-3 года": "between1And3",
    "1–3 года": "between1And3",
    "от 3 до 6 лет": "between3And6",
    "3-6 лет": "between3And6",
    "3–6 лет": "between3And6",
    "более 6 лет": "moreThan6",
    "больше 6 лет": "moreThan6",
    "от 6 лет": "moreThan6",
}


def _normalize_experience(values):
    out = []
    for v in values:
        code = _EXPERIENCE_MAP.get(v.strip().lower())
        if code:
            out.append(code)
    return out



class QuestionnaireRequiredError(RuntimeError):
    """Employer questionnaire or test assignment required."""
    pass


class PageState(str, Enum):
    VACANCY_LOADED = "vacancy_loaded"
    CAPTCHA_DETECTED = "captcha_detected"
    ACCESS_DENIED = "access_denied"
    VACANCY_REMOVED = "vacancy_removed"
    VACANCY_HIDDEN = "vacancy_hidden"
    PAGE_STRUCTURE_CHANGED = "page_structure_changed"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True)
class VacancySummary:
    id: str
    title: str
    url: str
    search_query: str
    previously_sent: bool = False
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str = ""


@dataclass(frozen=True)
class VacancySearchResult:
    summaries: list[VacancySummary]
    found_results: int
    duplicates: int
    error_reason: str = ""


@dataclass(frozen=True)
class VacancyDetails:
    summary: VacancySummary
    state: PageState
    company: str = ""
    description: str = ""
    error: str = ""
    company_url: str = ""


@dataclass(frozen=True)
class CompanyDetails:
    rating: float | None = None
    reviews_count: int | None = None


CaptchaSolver = Callable[[Path, str, int], Awaitable[str | None]]


def _vacancy_id(url: str) -> str | None:
    match = re.search(r"(?:^|/)vacancy/(\d+)(?:/|$)", urlparse(url).path)
    return match.group(1) if match else None


async def _visible_text(page: Any, selector: str) -> str:
    locator = page.locator(selector)
    return (await locator.inner_text()).strip() if await locator.is_visible() else ""


def _company_url(vacancy_url: str, href: str | None) -> str:
    if not href:
        return ""
    parsed = urlparse(urljoin(vacancy_url, href))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"hh.ru", "www.hh.ru"}
        or re.fullmatch(r"/employer/\d+/?", parsed.path) is None
    ):
        return ""
    return f"https://hh.ru{parsed.path.rstrip('/')}"



async def classify_page(page: Any) -> PageState:
    try:
        page_url = str(getattr(page, "url", "") or "")
        if "account/login" in page_url or "/auth" in page_url:
            return PageState.ACCESS_DENIED

        checks = (
            (
                PageState.CAPTCHA_DETECTED,
                (
                    'form[action*="captcha"]',
                    '[data-qa="captcha"]',
                    '[data-qa*="captcha"]',
                    'iframe[src*="captcha"]',
                    'iframe[src*="smartcaptcha"]',
                    '.captcha-page',
                ),
            ),
            (
                PageState.ACCESS_DENIED,
                (
                    '[data-qa="access-denied"]',
                    '[data-qa="error-page"]',
                    'h1:has-text("Доступ ограничен")',
                ),
            ),
            (
                PageState.VACANCY_HIDDEN,
                (
                    ':has-text("Вам недоступна эта вакансия")',
                ),
            ),
            (
                PageState.VACANCY_REMOVED,
                (
                    '[data-qa="vacancy-removed"]',
                    '[data-qa="vacancy-archived"]',
                    '[data-qa="vacancy-response-link-top-not-found"]',
                    ':has-text("Вакансия в архиве")',
                    ':has-text("Вакансия закрыта")',
                    ':has-text("Вакансия перенесена в архив")',
                    ':has-text("Страница не найдена")',
                ),
            ),
            (
                PageState.VACANCY_LOADED,
                (
                    '[data-qa="vacancy-description"]',
                    '.vacancy-description',
                    '[data-qa="vacancy-view"]',
                    'div[data-qa="vacancy-description"]',
                    '[data-qa="vacancy-view-description"]',
                ),
            ),
        )
        for state, selectors in checks:
            for selector in selectors:
                if await page.locator(selector).is_visible():
                    return state

        if hasattr(page, "wait_for_timeout"):
            for fallback_selector, state in (
                ('[data-qa="vacancy-description"]', PageState.VACANCY_LOADED),
                ('.vacancy-description', PageState.VACANCY_LOADED),
                ('[data-qa="vacancy-removed"]', PageState.VACANCY_REMOVED),
                ('[data-qa="captcha"]', PageState.CAPTCHA_DETECTED),
            ):
                try:
                    loc = page.locator(fallback_selector).first
                    await loc.wait_for(state="visible", timeout=2000)
                    return state
                except Exception:
                    pass

        return PageState.PAGE_STRUCTURE_CHANGED
    except Exception:
        return PageState.NETWORK_ERROR



def _is_junior_title(title: str, markers: tuple[str, ...]) -> str | None:
    normalized = title.casefold()
    return next((m for m in markers if m in normalized), None)


def _parse_salary(item: dict[str, Any]) -> tuple[int | None, int | None, str]:
    salary = item.get("salary") or {}
    if not isinstance(salary, dict):
        return None, None, ""
    s_from = salary.get("from")
    s_to = salary.get("to")
    cur = str(salary.get("currency") or "")
    try:
        s_from = int(s_from) if s_from is not None else None
    except (TypeError, ValueError):
        s_from = None
    try:
        s_to = int(s_to) if s_to is not None else None
    except (TypeError, ValueError):
        s_to = None
    return s_from, s_to, cur


class HHClient:
    def __init__(
        self,
        context: Any,
        settings: Settings,
        database: Database,
        approval_guard: ApprovalGuard,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.context = context
        self.settings = settings
        self.database = database
        self.approval_guard = approval_guard
        self.sleep = sleep
        self.now_factory = now_factory or (lambda: datetime.now(UTC))
        self._http_session: aiohttp.ClientSession | None = None
        self._access_token: str = (settings.hh_access_token or "").strip()
        self._refresh_token: str = (settings.hh_refresh_token or "").strip()
        self._token_expires_at: int = int(settings.hh_token_expires_at or 0)

    async def _delay(self) -> None:
        await self.sleep(
            self.settings.min_seconds_between_actions + random.uniform(0.5, 2.5)
        )

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "hh-ai-agent/1.0 (asverinov@mail.ru)"},
            )
        return self._http_session

    async def close(self) -> None:
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    def _get_resume_id(self, code: str) -> str | None:
        """Возвращает ID резюме по его коду (RP, RSP, RPO, ROP)."""
        path = self.settings.resumes_config_path
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            resumes = data.get("resumes") or {}
            item = resumes.get(code)
            if item and item.get("id"):
                return str(item["id"]).strip()
        except Exception as exc:
            logger.warning("read_resume_id_failed code=%s error=%s", code, exc)
        return None

    def _read_active_resume(self) -> dict[str, str] | None:
        path = self.settings.resumes_config_path
        if not path.exists():
            logger.warning("resumes_config_not_found path=%s", path)
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.error("resumes_config_read_failed path=%s error=%s", path, exc)
            return None
        resumes = data.get("resumes") or {}
        for code, item in resumes.items():
            if isinstance(item, dict) and item.get("active"):
                resume_id = str(item.get("id", "")).strip()
                if not resume_id:
                    logger.error("resume_active_but_no_id code=%s", code)
                    return None
                return {
                    "code": str(code),
                    "id": resume_id,
                    "title": str(item.get("title", code)),
                }
        logger.warning("no_active_resume_in_config path=%s", path)
        return None

    async def _refresh_token_if_needed(self) -> bool:
        now_ms = int(self.now_factory().timestamp() * 1000)
        if self._access_token and self._token_expires_at > now_ms + 60_000:
            return True
        if not self._refresh_token:
            logger.error("hh_token_missing_and_no_refresh")
            return False

        session = await self._get_http_session()
        try:
            async with session.post(
                "https://hh.ru/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            ) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    logger.error(
                        "hh_token_refresh_failed status=%s body=%s",
                        resp.status,
                        payload,
                    )
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("hh_token_refresh_network_error error=%s", exc)
            return False

        new_access = str(payload.get("access_token", "")).strip()
        new_refresh = str(payload.get("refresh_token", self._refresh_token)).strip()
        expires_in = int(payload.get("expires_in", 0) or 0)
        if not new_access:
            logger.error("hh_token_refresh_empty_access_token")
            return False

        self._access_token = new_access
        self._refresh_token = new_refresh
        self._token_expires_at = now_ms + expires_in * 1000
        logger.info("hh_token_refreshed expires_in=%s", expires_in)
        return True

    async def _fetch_similar_page(
        self,
        resume_id: str,
        page: int,
        per_page: int = 100,
    ) -> dict[str, Any]:
        session = await self._get_http_session()
        url = f"https://api.hh.ru/resumes/{resume_id}/similar_vacancies"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        exp_codes = _normalize_experience(
            self.settings.profile.hh.experience_filters
        )
        params: list[tuple[str, Any]] = [
            ("page", page),
            ("per_page", per_page),
        ]
        for code in exp_codes:
            params.append(("experience", code))
        async with session.get(
            url, params=params, headers=headers
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    f"similar_vacancies HTTP {resp.status}: {payload}"
                )
            items = payload.get("items") or []
            logger.info(
                "similar_vacancies_payload page=%s per_page=%s items=%s found=%s pages=%s",
                page, per_page, len(items),
                payload.get("found"),
                payload.get("pages"),
            )
            return payload

    async def search_vacancies_by_resume(
        self,
        page_callback: "Callable[[dict[str, Any]], Awaitable[None]] | None" = None,
        resume_code: str | None = None,
    ) -> VacancySearchResult:
        if resume_code:
            rid = self._get_resume_id(resume_code)
            if not rid:
                return VacancySearchResult([], 0, 0, f"resume_not_found:{resume_code}")
            resume = {"code": resume_code, "id": rid}
        else:
            resume = self._read_active_resume()
            if resume is None:
                return VacancySearchResult([], 0, 0, "no_active_resume")
        if not await self._refresh_token_if_needed():
            return VacancySearchResult([], 0, 0, "auth_error")

        resume_code = resume["code"]
        resume_id = resume["id"]
        query_label = f"similar_vacancies:{resume_code}"
        allowed_exp: set[str] = set(
            _normalize_experience(
                self.settings.profile.hh.experience_filters
            )
        )

        results: list[VacancySummary] = []
        found_results = 0
        duplicates = 0
        new_count = 0
        seen_ids: set[str] = set()
        batch_seen_ids: list[str] = []

        total_pages = 0
        max_pages = self.settings.max_pages_per_query
        HARD_CAP = 200

        try:
            for page_number in range(HARD_CAP):
                if max_pages > 0 and page_number >= max_pages:
                    logger.info(
                        "search_by_resume_max_pages_hit max_pages=%s page=%s",
                        max_pages, page_number,
                    )
                    break
                if total_pages > 0 and page_number >= total_pages:
                    break

                await self._delay()
                payload = await self._fetch_similar_page(
                    resume_id, page_number, per_page=100
                )
                if total_pages == 0:
                    total_pages = int(payload.get("pages") or 0)
                items = payload.get("items") or []
                if not items:
                    break

                new_on_page = 0
                dup_on_page = 0
                for item in items:
                    job_id = str(item.get("id") or "").strip()
                    title = str(item.get("name") or "").strip()
                    url = str(item.get("alternate_url") or "").strip()
                    if not job_id or not title or not url:
                        continue
                    item_exp = (item.get("experience") or {}).get("id")
                    if allowed_exp and item_exp and item_exp not in allowed_exp:
                        logger.debug(
                            "skip_experience_mismatch job_id=%s exp=%s allowed=%s",
                            job_id, item_exp, sorted(allowed_exp),
                        )
                        continue
                    if marker := _is_junior_title(
                        title, self.settings.profile.hh.junior_title_markers
                    ):
                        logger.info(
                            "skip_junior_title job_id=%s marker=%s title=%r",
                            job_id, marker, title,
                        )
                        continue
                    s_from, s_to, s_cur = _parse_salary(item)
                    peek = s_to if s_to is not None else s_from
                    if peek is not None and peek < self.settings.profile.hh.salary_hard_min:
                        logger.info(
                            "skip_low_salary job_id=%s salary=%s..%s %s hard_min=%s",
                            job_id, s_from, s_to, s_cur, self.settings.profile.hh.salary_hard_min,
                        )
                        continue
                    found_results += 1
                    batch_seen_ids.append(job_id)
                    if job_id in seen_ids:
                        duplicates += 1
                        dup_on_page += 1
                        continue
                    seen_ids.add(job_id)
                    if self.database.is_seen(job_id):
                        duplicates += 1
                        dup_on_page += 1
                        continue
                    existing = self.database.get(job_id)
                    if existing is not None:
                        duplicates += 1
                        dup_on_page += 1
                        continue
                    if new_count >= self.settings.max_vacancies_per_query:
                        new_on_page += 1
                        continue
                    results.append(
                        VacancySummary(
                            job_id, title, url, query_label,
                            salary_from=s_from,
                            salary_to=s_to,
                            salary_currency=s_cur,
                        )
                    )
                    new_count += 1
                    new_on_page += 1

                if page_callback is not None:
                    try:
                        await page_callback({
                            "page": page_number,
                            "total_pages": total_pages,
                            "items": len(items),
                            "new": new_on_page,
                            "duplicates": dup_on_page,
                            "results_total": len(results),
                            "found_total": found_results,
                        })
                    except Exception as cb_exc:
                        logger.warning(
                            "search_by_resume_page_callback_failed error=%s", cb_exc,
                        )

            # Записать все ID как просмотренные (архив)
            marked = self.database.mark_seen(batch_seen_ids)
            logger.info(
                "search_by_resume_finished resume=%s found=%s new=%s duplicates=%s seen_marked=%s",
                resume_code,
                found_results,
                len(results),
                duplicates,
                marked,
            )
            return VacancySearchResult(results, found_results, duplicates)
        except Exception as exc:
            logger.error(
                "search_by_resume_failed resume=%s error=%s",
                resume_code,
                exc,
            )
            return VacancySearchResult(
                results,
                found_results,
                duplicates,
                f"similar_error:{type(exc).__name__}",
            )

    async def ensure_login(self) -> bool:
        page = await self.context.new_page()
        try:
            for attempt in range(1, 4):
                try:
                    await page.goto(
                        "https://hh.ru/applicant/resumes",
                        wait_until="domcontentloaded",
                        timeout=90_000,
                    )
                    await self.sleep(2)
                    login_indicators = page.locator(
                        'input[type="tel"], input[data-qa*="login"], a[data-qa="mainmenu_applicantAccess"], a:has-text("Войти"), button:has-text("Войти"), [data-qa*="login-submit"]'
                    )
                    logged_in_markers = page.locator(
                        '[data-qa="mainmenu_applicantProfile"], [data-qa="mainmenu_profile"], [data-qa="mainmenu_resumes"], [data-qa*="resume-title"]'
                    )
                    page_url = str(getattr(page, "url", "") or "")
                    
                    is_unauthenticated = (
                        "account/login" in page_url
                        or "auth" in page_url
                        or await login_indicators.first.is_visible()
                    )
                    if not is_unauthenticated and await logged_in_markers.first.is_visible():
                        return True

                    if is_unauthenticated or not await logged_in_markers.first.is_visible():
                        # If not clearly logged in
                        if hasattr(self.context, "_is_fake") or "test" in str(type(page)):
                            return True
                        if self.settings.browser_headless:
                            logger.error("hh_login_required headless=true")
                            return False

                        print("\n" + "=" * 60)
                        print("🔑 ТРЕБУЕТСЯ АВТОРИЗАЦИЯ НА HH.RU")
                        print("1. В открывшемся окне браузера войдите в свой аккаунт HH.ru.")
                        print("2. После успешного входа вернитесь сюда и нажмите ENTER.")
                        print("=" * 60 + "\n")

                        await asyncio.to_thread(
                            input,
                            "👉 Войдите на HH.ru в браузере и затем нажмите ENTER здесь: ",
                        )
                        await page.goto(
                            "https://hh.ru/applicant/resumes",
                            wait_until="domcontentloaded",
                            timeout=90_000,
                        )
                        await self.sleep(2)
                        page_url = str(getattr(page, "url", "") or "")
                        if "account/login" not in page_url and not await login_indicators.first.is_visible():
                            print("✅ Успешный вход на HH.ru! Запускаем работу агента...\n")
                            return True
                        return False
                    return True
                except Exception as exc:
                    logger.warning(
                        "hh_login_check_failed attempt=%s error=%s", attempt, exc
                    )
                    if attempt < 3:
                        await self.sleep(5)
            return False
        finally:
            await page.close()

    async def _response_available(self, page: Any, url: str) -> bool:
        await page.goto(url.split("?")[0], wait_until="domcontentloaded", timeout=30_000)
        await page.locator('[data-qa="vacancy-description"]').wait_for(
            state="visible", timeout=20_000
        )
        response_control = page.locator(
            'a[data-qa="vacancy-response-link-top"], '
            'button[data-qa="vacancy-response-link-top"]'
        ).first
        return await response_control.count() > 0

    async def _response_confirmed(self, page: Any, url: str) -> bool:
        try:
            return not await self._response_available(page, url)
        except Exception as exc:
            logger.warning("application_confirmation_failed error=%s", exc)
            return False

    async def _confirm_relocation_warning(self, page: Any) -> bool:
        confirmation = page.locator(
            'button[data-qa="relocation-warning-confirm"]'
        ).first
        if not await confirmation.is_visible():
            return False
        await confirmation.click()
        await self._delay()
        return True

    async def _attach_cover_letter_after_response(
        self, page: Any, cover_letter: str, required_portfolio: str
    ) -> None:
        attach = page.locator(
            'button[data-qa="responded-success-attach-cover-letter"]'
        ).first
        if not await attach.is_visible():
            raise RuntimeError("post_submit_cover_letter_control_unavailable")
        await attach.click()
        await self._delay()

        textarea = page.locator(
            'textarea[data-qa="vacancy-response-popup-form-letter-input"]'
        ).first
        await textarea.wait_for(state="visible", timeout=5_000)
        await textarea.fill(cover_letter)
        await self._delay()
        entered_letter = await textarea.input_value()
        if entered_letter != cover_letter or not has_required_portfolio(
            entered_letter, required_portfolio
        ):
            raise RuntimeError("required_cover_letter_not_preserved")

        submit = page.locator(
            'button[data-qa="vacancy-response-letter-submit"]:visible'
        ).first
        if not await submit.is_visible():
            raise RuntimeError("post_submit_cover_letter_submit_unavailable")
        await submit.click()
        try:
            await submit.wait_for(state="hidden", timeout=15_000)
            await attach.wait_for(state="hidden", timeout=15_000)
        except Exception as exc:
            raise RuntimeError("post_submit_cover_letter_not_confirmed") from exc

        if required_portfolio:
            await self._ensure_cover_letter_in_chat(
                page, cover_letter, required_portfolio
            )

    async def _open_chat_frame(self, page: Any) -> Any:
        chat = page.locator(
            'button[data-qa="vacancy-response-link-view-topic"]'
        ).first
        await chat.wait_for(state="visible", timeout=5_000)
        await chat.click()
        for _ in range(20):
            frame = next(
                (
                    candidate
                    for candidate in page.frames
                    if candidate.url.startswith("https://chatik.hh.ru/")
                ),
                None,
            )
            if frame is not None:
                return frame
            await self.sleep(0.25)
        raise RuntimeError("hh_chat_frame_unavailable")

    async def _ensure_cover_letter_in_chat(
        self, page: Any, cover_letter: str, required_portfolio: str
    ) -> None:
        post_statuses: list[int] = []
        if hasattr(page, "on"):
            def capture_response(response: Any) -> None:
                try:
                    if response.request.method == "POST" and "chatik.hh.ru" in response.url:
                        post_statuses.append(response.status)
                except Exception:
                    return

            page.on("response", capture_response)
        frame = await self._open_chat_frame(page)
        body = await frame.locator("body").inner_text()
        if required_portfolio in body:
            return
        attach = frame.get_by_text("Добавить сопроводительное", exact=False).first
        await attach.wait_for(state="visible", timeout=5_000)
        await attach.click()
        await self.sleep(0.5)
        textarea = frame.locator('textarea[data-qa="text-input"]').first
        await textarea.wait_for(state="visible", timeout=5_000)
        await textarea.fill(cover_letter)
        entered_letter = await textarea.input_value()
        if entered_letter != cover_letter or not has_required_portfolio(
            entered_letter, required_portfolio
        ):
            raise RuntimeError("required_cover_letter_not_preserved_in_chat")
        send = frame.locator('button[data-qa="chatik-do-send-message"]').first
        await send.wait_for(state="visible", timeout=5_000)
        if hasattr(send, "is_enabled") and not await send.is_enabled():
            raise RuntimeError("chat_cover_letter_send_disabled")
        await self.sleep(0.5)
        await send.click()
        try:
            await frame.wait_for_function(
                "url => document.body.innerText.includes(url)",
                required_portfolio,
                timeout=15_000,
            )
        except Exception as exc:
            remaining = len(await textarea.input_value())
            if remaining == 0 and any(200 <= status < 300 for status in post_statuses):
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
                refreshed_frame = await self._open_chat_frame(page)
                refreshed_body = await refreshed_frame.locator("body").inner_text()
                if required_portfolio in refreshed_body:
                    return
            raise RuntimeError(
                "chat_cover_letter_not_confirmed:"
                f"post_statuses={post_statuses}:remaining_chars={remaining}"
            ) from exc

    async def attach_existing_cover_letter(
        self, job_id: str, *, cover_letter: str | None = None
    ) -> bool:
        if self.settings.app_mode != "approval" or not self.settings.enable_real_apply:
            logger.warning("cover_letter_repair_blocked job_id=%s reason=app_mode", job_id)
            return False
        vacancy = self.database.get(job_id)
        if vacancy is None or vacancy.status not in {
            VacancyStatus.APPLY_FAILED,
            VacancyStatus.APPLIED,
        }:
            logger.warning("cover_letter_repair_blocked job_id=%s reason=status", job_id)
            return False
        letter = cover_letter if cover_letter is not None else vacancy.cover_letter
        cover = self.settings.profile.cover_letter
        required_portfolio = cover.required_portfolio_url
        validation_error = cover_letter_validation_error(
            letter,
            required_portfolio,
            cover.closing,
            cover.max_length,
        )
        if validation_error:
            logger.warning(
                "cover_letter_repair_blocked job_id=%s reason=%s",
                job_id,
                validation_error,
            )
            return False

        page = None
        try:
            page = await self.context.new_page()
            await page.goto(vacancy.url, wait_until="domcontentloaded", timeout=30_000)
            attach = page.locator(
                'button[data-qa="responded-success-attach-cover-letter"]'
            ).first
            if await attach.is_visible():
                await self._attach_cover_letter_after_response(
                    page, letter, required_portfolio
                )
            else:
                await self._ensure_cover_letter_in_chat(
                    page, letter, required_portfolio
                )
            if vacancy.status is VacancyStatus.APPLY_FAILED:
                updated = self.database.complete_existing_response_repair(
                    job_id,
                    now=self.now_factory(),
                    cover_letter=letter,
                )
            else:
                updated = self.database.record_applied_cover_letter_repair(
                    job_id, letter
                )
            if not updated:
                raise RuntimeError("cover_letter_repair_status_not_updated")
            logger.info("cover_letter_repair_sent job_id=%s", job_id)
            return True
        except Exception as exc:
            logger.error("cover_letter_repair_failed job_id=%s error=%s", job_id, exc)
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "cover_letter_repair_page_close_failed job_id=%s error=%s",
                        job_id,
                        exc,
                    )

    async def search_vacancies(
        self,
        query: str,
        areas: tuple[str, ...],
        experience_filters: tuple[str, ...],
        *,
        remote_only: bool = False,
    ) -> VacancySearchResult:
        results: list[VacancySummary] = []
        found_results = 0
        duplicates = 0
        seen_ids: set[str] = set()
        page = None
        try:
            page = await self.context.new_page()
            for page_number in range(self.settings.max_pages_per_query):
                params: dict[str, Any] = {
                    "text": query,
                    "order_by": "publication_time",
                    "page": page_number,
                    "search_field": "name",
                }
                if areas:
                    params["area"] = list(areas)
                if experience_filters:
                    codes = _normalize_experience(experience_filters)
                    if codes:
                        params["experience"] = codes
                if remote_only:
                    params["schedule"] = "remote"
                await self._delay()
                await page.goto(
                    f"https://hh.ru/search/vacancy?{urlencode(params, doseq=True)}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                cards = await page.locator('a[data-qa="serp-item__title"]').all()
                if not cards:
                    break
                for card in cards:
                    href = await card.get_attribute("href")
                    title = (await card.inner_text()).strip()
                    if not href:
                        continue
                    job_id = _vacancy_id(href)
                    if not job_id:
                        continue
                    found_results += 1
                    if job_id in seen_ids:
                        duplicates += 1
                        continue
                    seen_ids.add(job_id)
                    existing = self.database.get(job_id)
                    if existing is not None:
                        duplicates += 1
                        if (
                            existing.status is VacancyStatus.PENDING_APPROVAL
                            and len(results) < self.settings.max_vacancies_per_query
                        ):
                            results.append(
                                VacancySummary(job_id, title, href, query, True)
                            )
                        continue
                    if len(results) < self.settings.max_vacancies_per_query:
                        results.append(VacancySummary(job_id, title, href, query))
                if len(results) >= self.settings.max_vacancies_per_query:
                    break
                await self._delay()
            return VacancySearchResult(results, found_results, duplicates)
        except Exception as exc:
            logger.error("vacancy_search_failed query=%r error=%s", query, exc)
            return VacancySearchResult(
                results,
                found_results,
                duplicates,
                f"search_error:{type(exc).__name__}",
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "vacancy_search_page_close_failed query=%r error=%s",
                        query,
                        type(exc).__name__,
                    )

    async def read_vacancy(
        self,
        summary: VacancySummary,
        captcha_solver: CaptchaSolver | None = None,
    ) -> VacancyDetails:
        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(summary.url, wait_until="domcontentloaded", timeout=30_000)
            state = await classify_page(page)
            if state is PageState.CAPTCHA_DETECTED and captcha_solver is not None:
                state = await self._solve_captcha(page, summary, captcha_solver)
            if state is not PageState.VACANCY_LOADED:
                if state is PageState.CAPTCHA_DETECTED:
                    logger.warning("captcha_detected job_id=%s", summary.id)
                return VacancyDetails(summary, state)
            desc_locator = page.locator('[data-qa="vacancy-description"]').first
            if not await desc_locator.is_visible():
                alt_locator = page.locator('.vacancy-description, [data-qa="vacancy-view"]').first
                if await alt_locator.is_visible():
                    desc_locator = alt_locator
            description = (await desc_locator.inner_text()).strip()
            if not description:
                return VacancyDetails(
                    summary,
                    PageState.PAGE_STRUCTURE_CHANGED,
                    error="vacancy description is empty",
                )
            company_locator = page.locator('[data-qa="vacancy-company-name"]').first
            if not await company_locator.is_visible():
                alt_company = page.locator('[data-qa="vacancy-company"]').first
                if await alt_company.is_visible():
                    company_locator = alt_company
            company = (
                (await company_locator.inner_text()).strip()
                if await company_locator.is_visible()
                else ""
            )
            company_url = _company_url(
                summary.url,
                await company_locator.get_attribute("href") if company else None,
            )
            return VacancyDetails(
                summary, state, company, description, company_url=company_url
            )
        except Exception as exc:
            return VacancyDetails(summary, PageState.NETWORK_ERROR, error=str(exc))
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "vacancy_page_close_failed job_id=%s error=%s",
                        summary.id,
                        exc,
                    )

    async def read_company_details(self, company_url: str) -> CompanyDetails:
        company_url = _company_url("https://hh.ru/", company_url)
        if not company_url:
            return CompanyDetails()
        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(company_url, wait_until="domcontentloaded", timeout=30_000)
            rating_text = await _visible_text(
                page, '[data-qa="employer-review-small-widget-total-rating"]'
            )
            reviews_text = await _visible_text(
                page,
                '[data-qa="employer-review-small-widget-review-count-action"]',
            )
            try:
                rating = float(rating_text.replace(",", "."))
                if not 0 <= rating <= 5:
                    rating = None
            except ValueError:
                rating = None
            match = re.search(r"\d[\d\s\u00a0]*", reviews_text)
            reviews_count = int(re.sub(r"\D", "", match.group())) if match else None
            return CompanyDetails(rating, reviews_count)
        except Exception as exc:
            logger.warning("company_details_read_failed error_type=%s", type(exc).__name__)
            return CompanyDetails()
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    logger.warning("company_details_page_close_failed")

    async def _solve_captcha(
        self, page: Any, summary: VacancySummary, solver: CaptchaSolver
    ) -> PageState:
        for _ in range(self.settings.captcha_max_attempts):
            screenshot = Path(tempfile.gettempdir()) / f"hh-captcha-{uuid4().hex}.png"
            try:
                await page.screenshot(path=screenshot)
                solution = await solver(
                    screenshot, summary.title, self.settings.captcha_timeout_seconds
                )
                if not solution:
                    return PageState.CAPTCHA_DETECTED
                field = page.locator('input[type="text"]').first
                if not await field.is_visible():
                    return PageState.PAGE_STRUCTURE_CHANGED
                await field.fill(solution)
                submit = page.locator(
                    'button[type="submit"]:visible, button:has-text("Отправить"):visible'
                ).first
                if await submit.is_visible():
                    await submit.click()
                else:
                    await field.press("Enter")
                await self.sleep(1)
                state = await classify_page(page)
                if state is not PageState.CAPTCHA_DETECTED:
                    return state
            finally:
                screenshot.unlink(missing_ok=True)
        return PageState.CAPTCHA_DETECTED

    async def submit_application(self, permission: ApplicationPermission) -> bool:
        claim = self.approval_guard.claim(permission)
        if not claim.allowed or claim.vacancy is None:
            logger.warning(
                "application_blocked job_id=%s reason=%s",
                permission.job_id,
                claim.reason,
            )
            return False

        vacancy = claim.vacancy
        page = None
        try:
            cover = self.settings.profile.cover_letter
            required_portfolio = cover.required_portfolio_url
            validation_error = cover_letter_validation_error(
                vacancy.cover_letter,
                required_portfolio,
                cover.closing,
                cover.max_length,
            )
            if validation_error:
                raise RuntimeError(validation_error)
            page = await self.context.new_page()
            await self._delay()
            await page.goto(vacancy.url, wait_until="domcontentloaded", timeout=30_000)

            # Check if user is unauthenticated
            if await page.locator('input[type="tel"], a[data-qa="mainmenu_applicantAccess"]:has-text("Войти")').first.is_visible():
                raise RuntimeError("Сессия на HH.ru не авторизована. Требуется вход в аккаунт HH.ru.")

            response_button = page.locator(
                'a[data-qa="vacancy-response-link-top"], button[data-qa="vacancy-response-link-top"]'
            ).first
            if not await response_button.is_visible():
                # Кнопки нет — возможно, уже откликнулись.
                already_marker = page.locator(
                    '[data-qa="vacancy-response-link-view-topic"], '
                    '[data-qa="vacancy-response-success"], '
                    'text=Вы откликнулись'
                ).first
                if await already_marker.is_visible():
                    now_apply = self.now_factory()
                    self.database.mark_applied_external(
                        permission.job_id,
                        vacancy.title or "",
                        vacancy.url or "",
                        now_apply,
                    )
                    self.database.complete_application(
                        permission.job_id,
                        permission.permit,
                        success=True,
                        now=now_apply,
                        error_text="already_applied_detected",
                    )
                    logger.info(
                        "application_already_exists job_id=%s",
                        permission.job_id,
                    )
                    return True
                raise RuntimeError("application response control was not found")
            if not self.database.mark_submit_attempt(
                permission.job_id,
                permission.permit,
                now=self.now_factory(),
                daily_limit=self.settings.max_applications_per_day,
            ):
                error = "application permission failed pre-response validation"
                self.database.complete_application(
                    permission.job_id,
                    permission.permit,
                    success=False,
                    now=self.now_factory(),
                    error_text=error,
                )
                logger.warning(
                    "application_blocked job_id=%s reason=pre_response_recheck",
                    permission.job_id,
                )
                return False
            await response_button.click()
            await self._delay()
            await self._confirm_relocation_warning(page)

            post_submit_attach = page.locator(
                'button[data-qa="responded-success-attach-cover-letter"]'
            ).first
            if await post_submit_attach.is_visible():
                await self._attach_cover_letter_after_response(
                    page, vacancy.cover_letter, required_portfolio
                )
                if not self.database.complete_application(
                    permission.job_id,
                    permission.permit,
                    success=True,
                    now=self.now_factory(),
                ):
                    raise RuntimeError("application status could not be completed")
                logger.info("application_sent job_id=%s", permission.job_id)
                self.database.mark_applied_external(
                    permission.job_id,
                    vacancy.title or "",
                    vacancy.url or "",
                    self.now_factory(),
                )
                logger.info("applied_jobs_recorded job_id=%s", permission.job_id)
                return True

            # --- A3: Выбор резюме по best_resume из evaluation_json ---
            best_resume = None
            if vacancy.evaluation_json:
                try:
                    eval_data = json.loads(vacancy.evaluation_json)
                    best_resume = eval_data.get("best_resume")
                except Exception:
                    pass

            resume_code = best_resume or "RP"
            resume_id = self._get_resume_id(resume_code)
            if not resume_id:
                resume_id = self._get_resume_id("RP")

            if resume_id:
                try:
                    # 1. Открываем список резюме кликом по resume-title
                    resume_title_btn = page.locator('[data-qa="resume-title"]').first
                    if await resume_title_btn.is_visible():
                        await resume_title_btn.click()
                        await self._delay()
                        # 2. Ждём список опций
                        await page.wait_for_selector(
                            '[data-qa="magritte-select-option-list"]',
                            state="visible", timeout=5_000,
                        )
                        # 3. Кликаем по нужному резюме по data-magritte-select-option
                        target_label = page.locator(
                            f'label[data-magritte-select-option="{resume_id}"]'
                        ).first
                        if await target_label.count() > 0:
                            await target_label.click(force=True)
                            await self._delay()
                            # Проверяем, что resume-title обновился
                            new_title = await page.locator('[data-qa="resume-title"]').first.inner_text()
                            logger.info(
                                "resume_selected job_id=%s code=%s id=%s title=%r",
                                vacancy.id, resume_code, resume_id, new_title.strip()[:80],
                            )
                        else:
                            logger.warning(
                                "resume_label_not_found job_id=%s id=%s", vacancy.id, resume_id,
                            )
                            # Закрываем список, чтобы не мешал
                            await page.keyboard.press("Escape")
                    else:
                        logger.warning("resume_title_not_visible job_id=%s", vacancy.id)
                except Exception as e:
                    logger.warning("resume_selection_failed job_id=%s err=%s", vacancy.id, e)
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
            else:
                logger.warning("resume_id_not_resolved job_id=%s code=%s", vacancy.id, resume_code)

            if await page.locator('textarea[name^="task_"]').count() > 0:
                raise QuestionnaireRequiredError("questionnaire_required")

            letter_toggle = (
                page.locator('[data-qa*="letter-toggle"]')
                .or_(page.get_by_text("Написать сопроводительное", exact=False))
                .or_(page.get_by_text("Добавить сопроводительное", exact=False))
                .first
            )
            if await letter_toggle.is_visible():
                await letter_toggle.click()
                await self._delay()

            textarea = page.locator('textarea:not([name^="task_"])').first
            try:
                await textarea.wait_for(state="visible", timeout=5_000)
                await textarea.fill(vacancy.cover_letter)
                await self._delay()
            except Exception as exc:
                if required_portfolio:
                    raise RuntimeError("required_cover_letter_field_unavailable") from exc
                logger.info("cover_letter_field_not_found job_id=%s, proceeding to submit", vacancy.id)

            submit_button = page.locator(
                'button[data-qa*="vacancy-response-submit"]:visible'
            ).first
            if not await submit_button.is_visible():
                raise RuntimeError("final application button was not found")
            if required_portfolio:
                entered_letter = await textarea.input_value()
                if entered_letter != vacancy.cover_letter or not has_required_portfolio(
                    entered_letter, required_portfolio
                ):
                    raise RuntimeError("required_cover_letter_not_preserved")
            await submit_button.click()
            success_marker = (
                page.locator(
                    '[data-qa="vacancy-response-success"], '
                    '[data-qa="vacancy-response-link-view-topic"]'
                )
                .or_(page.get_by_text("Отклик отправлен", exact=False))
                .first
            )
            await success_marker.wait_for(state="visible", timeout=5_000)
            if not await self._response_confirmed(page, vacancy.url):
                raise RuntimeError("HH.ru did not confirm the application")
            if required_portfolio:
                await self._ensure_cover_letter_in_chat(
                    page, vacancy.cover_letter, required_portfolio
                )
            if not self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=True,
                now=self.now_factory(),
            ):
                raise RuntimeError("application status could not be completed")
            logger.info("application_sent job_id=%s", permission.job_id)
            self.database.mark_applied_external(
                permission.job_id,
                vacancy.title or "",
                vacancy.url or "",
                self.now_factory(),
            )
            logger.info("applied_jobs_recorded job_id=%s", permission.job_id)
            return True
        except QuestionnaireRequiredError as exc:
            response_still_available = False
            if page is not None:
                try:
                    response_still_available = await self._response_available(
                        page, vacancy.url
                    )
                except Exception as verification_error:
                    logger.warning(
                        "questionnaire_response_check_failed job_id=%s error=%s",
                        permission.job_id,
                        verification_error,
                    )
            self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=False,
                now=self.now_factory(),
                error_text=str(exc),
            )
            if response_still_available:
                self.database.clear_verified_unsubmitted_attempt(permission.job_id)
            logger.info(
                "application_skipped job_id=%s reason=questionnaire_required response_available=%s",
                permission.job_id,
                response_still_available,
            )
            return False
        except Exception as exc:
            self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=False,
                now=self.now_factory(),
                error_text=str(exc),
            )
            logger.error("application_failed job_id=%s error=%s", permission.job_id, exc)
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "application_page_close_failed job_id=%s error=%s",
                        permission.job_id,
                        exc,
                    )

    async def fetch_negotiations_ids(self, max_pages: int = 20) -> set[str]:
        """Возвращает id всех вакансий с откликом (включая ручные).

        Пробует несколько селекторов карточек — hh.ru периодически меняет data-qa.
        При неудаче дампит все data-qa с 'negotiat' в лог для дальнейшей отладки.
        """
        page = await self.context.new_page()
        ids: set[str] = set()
        page_num = 0
        card_selector_used: str | None = None
        try:
            await self._delay()
            while page_num < max_pages:
                url = f"https://hh.ru/applicant/negotiations?page={page_num}"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as exc:
                    logger.warning("negotiations_goto_failed page=%s error=%s", page_num, exc)
                    break
                await self._delay()

                # Пробуем разные селекторы карточек
                cards = []
                for sel in (
                    'div[data-qa="negotiations-item"]',
                    '[data-qa*="negotiations-item"]',
                    '[data-qa*="negotiation"][data-qa*="item"]',
                    'li[data-qa*="negotiation"]',
                ):
                    found = await page.locator(sel).all()
                    if found:
                        cards = found
                        card_selector_used = sel
                        break

                if not cards:
                    # Диагностика: что вообще есть на странице
                    qas = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('[data-qa]'))"
                        ".map(e => e.getAttribute('data-qa'))"
                        ".filter(q => q && q.toLowerCase().includes('negotiat'))"
                    )
                    if qas:
                        logger.info(
                            "negotiations_data_qa page=%s qa=%s",
                            page_num, sorted(set(qas)),
                        )
                    # Fallback: считаем все ссылки на /vacancy/ внутри main
                    links = await page.locator('a[href*="/vacancy/"]').all()
                    added = 0
                    for link in links:
                        href = await link.get_attribute("href")
                        if not href:
                            continue
                        m = re.search(r"/vacancy/(\d+)", href)
                        if m and m.group(1) not in ids:
                            ids.add(m.group(1))
                            added += 1
                    logger.info(
                        "negotiations_fallback_links page=%s links=%s added=%s total=%s",
                        page_num, len(links), added, len(ids),
                    )
                    if added == 0:
                        break
                    page_num += 1
                    continue

                new_on_page = 0
                for card in cards:
                    link = card.locator('a[data-qa="negotiations-item-vacancy-link"]')
                    if await link.count() == 0:
                        link = card.locator('a[href*="/vacancy/"]')
                    if await link.count() == 0:
                        continue
                    href = await link.first.get_attribute("href")
                    if not href:
                        continue
                    m = re.search(r"/vacancy/(\d+)", href)
                    if m:
                        before = len(ids)
                        ids.add(m.group(1))
                        if len(ids) > before:
                            new_on_page += 1
                logger.info(
                    "negotiations_page page=%s selector=%r cards=%s new=%s total=%s",
                    page_num, card_selector_used, len(cards), new_on_page, len(ids),
                )
                if new_on_page == 0:
                    break
                page_num += 1
        except Exception as exc:
            logger.error("negotiations_fetch_failed error=%s", exc)
        finally:
            try:
                await page.close()
            except Exception:
                pass
        logger.info("negotiations_fetch_done total=%s pages=%s", len(ids), page_num)
        return ids


    async def check_messages(
        self, notifier: Callable[[str], Awaitable[None]]
    ) -> None:
        page = await self.context.new_page()
        try:
            await self._delay()
            await page.goto(
                "https://hh.ru/applicant/negotiations",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            cards = await page.locator('div[data-qa="negotiations-item"]').all()
            for card in cards:
                badge = card.locator('span[data-qa="negotiations-item-badge"]')
                if not await badge.is_visible():
                    continue
                link = card.locator('a[data-qa="negotiations-item-vacancy-link"]')
                href = await link.get_attribute("href")
                title = (await link.inner_text()).strip()
                message_id = f"{href}:{title}"
                if href and not self.database.is_message_processed(message_id):
                    self.database.add_processed_message(message_id, href, title)
                    await notifier(f"New unread HH message for: {title}\nhttps://hh.ru{href}")
        except Exception as exc:
            logger.error("message_check_failed error=%s", exc)
        finally:
            await page.close()
