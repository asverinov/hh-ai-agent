# HH AI Agent

Агент автоматически ищет вакансии на HH.ru, оценивает их через LLM, генерирует сопроводительные письма и присылает подходящие в Telegram.

## Быстрый старт

Запусти мастер настройки — он проведёт тебя через все шаги:

```bash
python setup_wizard.py
```

Wizard спросит:
1. Токен Telegram-бота и твой User ID
2. Какой AI-провайдер использовать (Ollama локально, Mistral API или любой OpenAI-compatible)
3. Данные твоего профиля для анализа вакансий
4. Режим работы

После этого создаст `.env` и `profile.yaml`, проверит конфигурацию и покажет что делать дальше.

**Изменить настройки позже:**

```bash
python setup_wizard.py --edit
```

---

## Требования

- Python 3.11+
- Telegram Bot (создаётся через [@BotFather](https://t.me/BotFather))
- Один из LLM-провайдеров (подробнее ниже)
- [CloakBrowser](https://cloakbrowser.com/) (устанавливается автоматически через wizard)

---

## Режимы работы

| Режим | Описание |
|---|---|
| `dry_run` | Ищет и анализирует вакансии, присылает превью в Telegram — **без реальных откликов** |
| `approval` | Присылает вакансию с кнопкой «Откликнуться» — отклик только после твоего нажатия |

Начинай с `dry_run`. Переходи на `approval` после того как убедишься что всё работает.

---

## LLM-провайдеры

### Ollama (рекомендуется — локально, бесплатно)

1. Установи [Ollama](https://ollama.com/download)
2. Загрузи модель:
   ```bash
   ollama pull llama3
   ```
3. В wizard выбери **Ollama**

### Mistral API (облачный)

1. Зарегистрируйся на [console.mistral.ai](https://console.mistral.ai/)
2. Создай API ключ
3. В wizard выбери **Mistral API** и введи ключ

> ⚠️ При Mistral текст вакансий и твой профиль уходят во внешний API.

### OpenAI-compatible (любой совместимый)

Поддерживается любой сервис с эндпоинтом `/chat/completions` (LocalAI, LM Studio, Groq и т.п.).
В wizard выбери **OpenAI-compatible** и укажи URL + ключ.

---

## Telegram-команды

| Команда | Описание |
|---|---|
| `/start` | Краткая справка |
| `/status` | Режим, состояние, статистика |
| `/pause` | Приостановить поиск |
| `/resume` | Возобновить поиск |
| `/pending` | Вакансии, ожидающие решения |
| `/stats` | Статистика по статусам |
| `/cancel` | Отменить ввод CAPTCHA |

---

## Архитектура

| Файл | Ответственность |
|---|---|
| `config.py` | Валидация `.env` и `profile.yaml` |
| `browser_backend.py` | CloakBrowser / Playwright адаптер |
| `hh_client.py` | Поиск, чтение страниц, отправка откликов |
| `llm/` | Ollama / Mistral / OpenAI-compatible адаптеры, retry, квота |
| `ai_analyzer.py` | Анализ вакансий, генерация писем |
| `database.py` | SQLite-состояние, лимиты, переходы статусов |
| `approval.py` | Единственный разрешённый инициатор реального отклика |
| `tg_bot.py` | Telegram-команды, превью, inline-кнопки |
| `main.py` | Основной цикл агента |
| `setup_wizard.py` | Интерактивный мастер настройки |

---

## Безопасность

- Реальный отклик требует **трёх одновременных условий**: `APP_MODE=approval` + `ENABLE_REAL_APPLY=true` + нажатие кнопки твоим Telegram ID в течение 30 минут
- Массового автоматического режима нет
- `.env`, `profile.yaml` и `.browser-profile/` исключены из Git
- Токены, cookies и полный `.env` не записываются в логи

---

## Типичные ошибки

| Ошибка | Решение |
|---|---|
| `Configuration error` | Заполни все обязательные поля через `python setup_wizard.py --edit` |
| `CloakBrowser failed to start` | Проверь `python -m cloakbrowser info`, при необходимости смени на `BROWSER_BACKEND=playwright` |
| `HH.ru login is required` | Запусти с `BROWSER_HEADLESS=false` и войди вручную |
| `LLM check failed` | Проверь endpoint, ключ и дневную квоту через `python main.py --check-llm` |
| `Invalid model response` | Проверь провайдер и модель — вакансия безопасно пропускается |

---

## Разработка

Тесты не обращаются к HH.ru, Telegram или внешним LLM:

```bash
python -m compileall .
pytest -q
```

---

## Ограничения

- Автоматизация может нарушать правила HH.ru — ответственность за аккаунт несёт пользователь
- CloakBrowser не гарантирует отсутствие детектирования или CAPTCHA
- Нет proxy, GeoIP-ротации и внешних CAPTCHA-сервисов
- Рассчитано на одного владельца и одну SQLite-базу
- Письмо всегда нужно читать в Telegram перед откликом

---

## Благодарности

Огромное спасибо **[kkonstantin08](https://github.com/kkonstantin08)** за разработку этой архитектуры — именно он спроектировал весь безопасный конвейер от поиска вакансий до approval-механизма с permit-токенами.

Также благодарность **[danscMax](https://github.com/danscMax)** — он реализовал базовые проверки и валидацию конфигурации, которые легли в основу надёжной работы агента.

---

## Контакты

Вопросы и предложения: **@fikstt3 (telegram)**
