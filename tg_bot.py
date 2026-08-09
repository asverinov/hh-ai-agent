from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from approval import ApprovalService
from config import Settings
from database import Database, Vacancy
from llm.errors import LLMError
from llm.mistral_keys import MistralKeyCheckResult, MistralKeyManager, MistralKeyView


logger = logging.getLogger(__name__)
PRIVATE_REPLY = "This bot is private."
MISTRAL_KEY_INPUT_TTL = timedelta(minutes=15)


@dataclass
class AgentControl:
    paused: bool = False
    wake_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)




@dataclass
class MistralKeyInputSession:
    expires_at: datetime


class TelegramService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        approval_service: ApprovalService,
        control: AgentControl,
        *,
        mistral_keys: MistralKeyManager | None = None,
        bot: Any | None = None,
        dispatcher: Dispatcher | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.database = database
        self.approval_service = approval_service
        self.control = control
        self.mistral_keys = mistral_keys
        self.bot = bot or Bot(token=settings.tg_bot_token)
        self.dispatcher = dispatcher or Dispatcher()
        self.now_factory = now_factory or (lambda: datetime.now().astimezone())
        self._captcha_future: asyncio.Future[str | None] | None = None
        self._edit_job_id: str | None = None
        self._edit_future: asyncio.Future[str | None] | None = None
        self._mistral_key_input: MistralKeyInputSession | None = None
        self._register_handlers()

    def authorized(self, user_id: int) -> bool:
        return user_id == self.settings.tg_user_id

    def command(self, name: str, user_id: int) -> str:
        if not self.authorized(user_id):
            return PRIVATE_REPLY
        if name == "start":
            return "Personal HH assistant is ready. Use /status to inspect it."
        if name == "pause":
            self.control.paused = True
            logger.info("agent_paused")
            return "Agent paused."
        if name == "resume":
            self.control.paused = False
            self.control.wake_event.set()
            logger.info("agent_resumed")
            return "Agent resumed."
        if name == "status":
            stats = self.database.stats()
            processed = sum(stats.values())
            return (
                f"mode: {self.settings.app_mode}\n"
                f"state: {'paused' if self.control.paused else 'running'}\n"
                f"processed: {processed}\n"
                f"applied today: {self.database.applied_today(self.now_factory())}"
            )
        if name == "pending":
            pending = self.database.pending()
            return (
                "No pending vacancies."
                if not pending
                else "\n".join(f"{item.id}: {item.title}" for item in pending)
            )
        if name == "stats":
            return "\n".join(
                f"{status}: {count}"
                for status, count in self.database.stats().items()
                if count
            ) or "No vacancies recorded."
        if name == "cancel":
            if self._captcha_future and not self._captcha_future.done():
                self._captcha_future.set_result(None)
            if self._edit_future and not self._edit_future.done():
                self._edit_future.set_result(None)
            return "Current input request cancelled."
        return "Unknown command."

    def _register_handlers(self) -> None:
        for name in ("start", "status", "pause", "resume", "pending", "stats", "mistral_keys", "cancel"):
            self.dispatcher.message.register(self._command_handler, Command(name))
        self.dispatcher.callback_query.register(
            self._callback_handler,
            F.data.startswith("apply:")
            | F.data.startswith("skip:")
            | F.data.startswith("edit:")
            | F.data.startswith("mk:"),
        )
        self.dispatcher.message.register(self._text_handler)

    async def _command_handler(self, message: Message) -> None:
        name = (message.text or "").split()[0].lstrip("/").split("@")[0]
        if not self.authorized(message.from_user.id):
            await message.answer(PRIVATE_REPLY)
            return
        if name == "mistral_keys":
            await self._send_mistral_key_menu(message)
            return
        if name == "cancel" and self._mistral_key_input is not None:
            self._mistral_key_input = None
            await message.answer("Current Mistral key input cancelled.")
            return
        await message.answer(self.command(name, message.from_user.id))

    async def _callback_handler(self, callback: CallbackQuery) -> None:
        if not self.authorized(callback.from_user.id):
            await self._answer_callback(callback, PRIVATE_REPLY, show_alert=True)
            return
        action, separator, job_id = (callback.data or "").partition(":")
        if len((callback.data or "").encode()) > 64:
            await self._answer_callback(
                callback, "Некорректное действие.", show_alert=True
            )
            return
        if action == "mk":
            await self._mistral_key_callback(callback, (callback.data or "").split(":"))
            return
        if not separator or action not in {"apply", "skip", "edit"} or not job_id:
            await self._answer_callback(
                callback, "Некорректное действие.", show_alert=True
            )
            return
        if action == "apply":
            # Отвечаем на callback немедленно — Telegram требует ответ в течение ~10 сек,
            # а браузерный отклик может занять значительно больше времени.
            await callback.answer("⏳ Отправляем отклик...", show_alert=False)
            result = await self.approval_service.approve_and_apply(
                job_id, callback.from_user.id
            )
            vacancy = self.database.get(job_id)
            if result.ok:
                await self.notify("✓ Отклик отправлен")
                if callback.message:
                    await callback.message.edit_reply_markup(reply_markup=None)
            else:
                if vacancy and vacancy.error_text == "questionnaire_required":
                    await self.notify_questionnaire_required(vacancy.title, vacancy.url)
                    if callback.message:
                        await callback.message.edit_reply_markup(reply_markup=None)
                else:
                    await self.notify(f"✗ {result.message}")
        elif action == "edit":
            self._mistral_key_input = None
            await callback.answer("Пришлите новый текст сопроводительного письма сообщением (или /cancel):", show_alert=True)
            loop = asyncio.get_running_loop()
            self._edit_job_id = job_id
            self._edit_future = loop.create_future()
            try:
                new_letter = await asyncio.wait_for(self._edit_future, timeout=300)
                if new_letter:
                    self.database.update_cover_letter(job_id, new_letter)
                    await self.notify("✓ Сопроводительное письмо обновлено!")
                    updated_vacancy = self.database.get(job_id)
                    if updated_vacancy:
                        await self.send_preview(updated_vacancy, include_actions=True)
                else:
                    await self.notify("Редактирование письма отменено.")
            except TimeoutError:
                await self.notify("Время ожидания редактирования письма истекло.")
            finally:
                self._edit_job_id = None
                self._edit_future = None
        else:
            # skip выполняется быстро — можно отвечать обычным способом.
            result = self.approval_service.skip(job_id, callback.from_user.id)
            await self._answer_callback(
                callback, result.message, show_alert=not result.ok
            )
            if result.ok and callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)

    async def _send_mistral_key_menu(self, message: Any) -> None:
        if self.mistral_keys is None:
            await message.answer("Управление ключами Mistral недоступно.")
            return
        try:
            keys = self.mistral_keys.list_keys()
        except LLMError as exc:
            await message.answer(f"Ключи Mistral недоступны: {exc.category}.")
            return
        text, keyboard = self._mistral_key_menu(keys)
        await message.answer(text, reply_markup=keyboard)

    @classmethod
    def _mistral_key_menu(
        cls, keys: tuple[MistralKeyView, ...]
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        lines = ["Ключи Mistral:"]
        rows: list[list[InlineKeyboardButton]] = []
        for key in keys:
            marker = (
                "✅ текущий"
                if key.is_current
                else {
                    "ready": "✅ рабочий",
                    "cooldown": f"⏸ до {key.cooldown_until.astimezone():%H:%M}",
                    "disabled": "❌ отключён",
                }[key.status]
            )
            lines.append(f"{key.id}. {marker} ····{key.suffix}")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Проверить {key.id}",
                        callback_data=f"mk:check:{key.id}",
                    ),
                    InlineKeyboardButton(
                        text=f"Удалить {key.id}",
                        callback_data=f"mk:delete:{key.id}",
                    ),
                ]
            )
        if not keys:
            lines.append("Ключей нет.")
        rows.append(
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data="mk:add"),
                InlineKeyboardButton(text="Проверить все", callback_data="mk:check"),
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="Обновить", callback_data="mk:list")]
        )
        return "\n".join(lines), cls._mistral_keyboard(rows)

    async def _mistral_key_callback(
        self, callback: CallbackQuery, parts: list[str]
    ) -> None:
        if self.mistral_keys is None:
            await self._answer_callback(
                callback, "Управление ключами Mistral недоступно.", show_alert=True
            )
            return
        if parts == ["mk", "add"]:
            if self._captcha_future and not self._captcha_future.done():
                await self._answer_callback(
                    callback, "Сначала завершите CAPTCHA.", show_alert=True
                )
                return
            if self._edit_future is not None:
                await self._answer_callback(
                    callback,
                    "Сначала завершите редактирование письма.",
                    show_alert=True,
                )
                return
            self._mistral_key_input = MistralKeyInputSession(
                self.now_factory() + MISTRAL_KEY_INPUT_TTL
            )
            await self._answer_callback(
                callback, "Отправьте ключ одним сообщением."
            )
            return
        if parts == ["mk", "list"]:
            try:
                text, keyboard = self._mistral_key_menu(self.mistral_keys.list_keys())
            except LLMError as exc:
                await self._answer_callback(
                    callback, f"Ключи Mistral недоступны: {exc.category}.", show_alert=True
                )
                return
            await self._edit_mistral_message(callback, text, reply_markup=keyboard)
            return
        if parts == ["mk", "check"]:
            try:
                results = await self.mistral_keys.check_all()
            except LLMError as exc:
                await self._answer_callback(
                    callback, f"Проверка не выполнена: {exc.category}.", show_alert=True
                )
                return
            text = "\n".join(self._mistral_check_text(result) for result in results)
            await self._edit_mistral_message(
                callback, text or "Ключей для проверки нет."
            )
            return
        if len(parts) != 3 or not parts[2].isdecimal():
            await self._answer_callback(
                callback, "Некорректное действие.", show_alert=True
            )
            return
        action, key_id = parts[1], int(parts[2])
        if action == "delete":
            keyboard = self._mistral_keyboard(
                [
                    [
                        InlineKeyboardButton(
                            text="Удалить", callback_data=f"mk:confirm:{key_id}"
                        ),
                        InlineKeyboardButton(text="Отмена", callback_data="mk:list"),
                    ]
                ]
            )
            await self._edit_mistral_message(
                callback, f"Удалить ключ {key_id}?", reply_markup=keyboard
            )
            return
        if action == "check":
            try:
                result = await self.mistral_keys.check_key(key_id)
            except LLMError as exc:
                await self._answer_callback(
                    callback, f"Проверка не выполнена: {exc.category}.", show_alert=True
                )
                return
            if result is None:
                await self._answer_callback(
                    callback, "Ключ не найден.", show_alert=True
                )
                return
            await self._edit_mistral_message(callback, self._mistral_check_text(result))
            return
        if action == "confirm":
            try:
                deleted = await self.mistral_keys.delete_key(key_id)
            except LLMError as exc:
                await self._answer_callback(
                    callback, f"Ключ не удалён: {exc.category}.", show_alert=True
                )
                return
            if not deleted:
                await self._answer_callback(
                    callback, "Ключ не найден.", show_alert=True
                )
                return
            await self._edit_mistral_message(callback, f"Ключ {key_id} удалён.")
            return
        await self._answer_callback(
            callback, "Некорректное действие.", show_alert=True
        )

    @staticmethod
    def _mistral_check_text(result: MistralKeyCheckResult) -> str:
        outcome = "работает" if result.success else result.error_type
        return f"Ключ {result.key.id} ····{result.key.suffix}: {outcome}."

    @staticmethod
    def _mistral_keyboard(
        rows: list[list[InlineKeyboardButton]],
    ) -> InlineKeyboardMarkup | None:
        if any(
            len(button.callback_data.encode()) > 64
            for row in rows
            for button in row
        ):
            return None
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _edit_mistral_message(
        self,
        callback: CallbackQuery,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if callback.message is None:
            await self._answer_callback(
                callback, "Меню недоступно.", show_alert=True
            )
            return
        try:
            await callback.message.edit_text(text=text, reply_markup=reply_markup)
        except TelegramAPIError:
            await self._answer_callback(
                callback, "Действие выполнено, но меню не обновлено.", show_alert=True
            )
            return
        await self._answer_callback(callback, "Готово.")

    @staticmethod
    async def _answer_callback(
        callback: CallbackQuery, text: str, *, show_alert: bool = False
    ) -> None:
        try:
            await callback.answer(text, show_alert=show_alert)
        except TelegramAPIError:
            logger.warning("telegram_callback_answer_failed")

    async def _text_handler(self, message: Message) -> None:
        if not self.authorized(message.from_user.id):
            await message.answer(PRIVATE_REPLY)
            return
        text = message.text
        if text is None or (
            text.startswith("/") and self._mistral_key_input is None
        ):
            return
        if self._captcha_future and not self._captcha_future.done():
            self._captcha_future.set_result(text.strip())
            await message.answer("CAPTCHA input received.")
            return
        if self._edit_future and not self._edit_future.done() and message.text:
            self._edit_future.set_result(message.text.strip())
            return
        key_session = self._mistral_key_input
        if key_session is None:
            return
        if self.now_factory() >= key_session.expires_at:
            self._mistral_key_input = None
            await message.answer("Время ввода ключа истекло.")
            return
        raw_key = text.strip()
        try:
            await message.delete()
        except TelegramAPIError:
            self._mistral_key_input = None
            await message.answer(
                "Не удалось удалить сообщение. Удалите его вручную и повторите добавление."
            )
            return
        self._mistral_key_input = None
        try:
            result = await self.mistral_keys.add_key(raw_key)
        except LLMError as exc:
            await message.answer(f"Ключ не добавлен: {exc.category}.")
            return
        await message.answer(f"Ключ {result.id} ····{result.suffix} добавлен.")

    async def send_preview(self, vacancy: Vacancy, include_actions: bool) -> None:
        keyboard = None
        if include_actions:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Откликнуться", callback_data=f"apply:{vacancy.id}"
                        ),
                        InlineKeyboardButton(
                            text="Пропустить", callback_data=f"skip:{vacancy.id}"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="✏️ Изменить письмо", callback_data=f"edit:{vacancy.id}"
                        ),
                    ],
                ]
            )
        confidence = "n/a" if vacancy.confidence is None else f"{vacancy.confidence:.0%}"
        text = (
            f"<b>{html.escape(vacancy.title)}</b>\n"
            f"Company: {html.escape(vacancy.company or 'unknown')}\n"
            f"<a href=\"{html.escape(vacancy.url, quote=True)}\">Open vacancy</a>\n\n"
            f"Why it fits: {html.escape(vacancy.llm_reason)}\n"
            f"Confidence: {confidence}\n\n"
            f"<b>Cover letter</b>\n{html.escape(vacancy.cover_letter)}"
        )
        await self.bot.send_message(
            chat_id=self.settings.tg_user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        logger.info(
            "%s job_id=%s",
            "approval_requested" if include_actions else "preview_sent",
            vacancy.id,
        )

    async def notify(self, text: str) -> None:
        await self.bot.send_message(chat_id=self.settings.tg_user_id, text=text)

    async def notify_analysis_failed(self, title: str, url: str, error_type: str) -> None:
        text = (
            f"⚠️ <b>Ошибка AI-анализа вакансии</b>\n"
            f"<b>{html.escape(title)}</b>\n"
            f"<a href=\"{html.escape(url, quote=True)}\">Открыть вакансию</a>\n\n"
            f"Причина: <code>{html.escape(error_type)}</code>"
        )
        await self.bot.send_message(
            chat_id=self.settings.tg_user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def notify_questionnaire_required(self, title: str, url: str) -> None:
        text = (
            f"📋 <b>Требуется ручной отклик (тестовое / анкета)</b>\n"
            f"<b>{html.escape(title)}</b>\n"
            f"<a href=\"{html.escape(url, quote=True)}\">Открыть вакансию на HH.ru</a>\n\n"
            f"Работодатель требует заполнении анкеты или выполнение тестового задания."
        )
        await self.bot.send_message(
            chat_id=self.settings.tg_user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def request_captcha(
        self, screenshot: Path, title: str, timeout_seconds: int
    ) -> str | None:
        self._mistral_key_input = None
        loop = asyncio.get_running_loop()
        self._captcha_future = loop.create_future()
        await self.bot.send_photo(
            chat_id=self.settings.tg_user_id,
            photo=FSInputFile(screenshot),
            caption=f"CAPTCHA detected for: {title}\nReply with the text or use /cancel.",
        )
        try:
            return await asyncio.wait_for(self._captcha_future, timeout_seconds)
        except TimeoutError:
            return None
        finally:
            self._captcha_future = None

    async def start_polling(self) -> None:
        await self.dispatcher.start_polling(self.bot)

    async def stop(self) -> None:
        session = getattr(self.bot, "session", None)
        if session is not None:
            await session.close()
