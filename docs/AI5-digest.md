Дайджест сессии AI5/AI6
Последнее обновление: 22 октября 2026, после коммита 8c4618a (+ docs/HH-OAUTH-RECOVERY.md).
Как использовать: в новой сессии скажи «дайджест» — тебе дадут команду cat docs/AI5-digest.md. Копируешь её вывод целиком и присылаешь. Это единственное исключение из правила «никогда не cat целые файлы».

🔴 ПРАВИЛА РАБОТЫ С ОПЕРАТОРОМ
Александр Сверинов — заказчик и оператор. Не программист.

Команды — целиком, одним bash-блоком. Оператор копирует, вставляет, присылает вывод.

Патчи файлов — python-скриптом, который сам меняет + проверяет + откатывает при ошибке.

Цикл: ИИ даёт команду → оператор запускает, присылает вывод → ИИ анализирует → следующая.

Оператор не должен искать факты сам — ИИ просит конкретную команду.

Вывод можно присылать вместе с командой.

Когда оператор просит «через nano» — давать --начало-- / --конец-- без вложенных markdown-блоков (внутри использовать ~~~bash, не ```).

Жёсткие правила экономии контекста:

❌ Никогда не cat целые файлы. Исключение: docs/AI5-digest.md.

✅ Только: grep -n, sed -n 'X,Yp', head -N, tail -N, awk, wc -l.

✅ git diff → только --stat или git show <commit> -- file | head -80.

✅ Логи — только хвост: tail -50 agent.log | grep -E "ERROR|WARNING".

✅ Одна команда — один вывод.

✅ cp file file.bak-<что-то> перед изменением.

✅ Обновлять дайджест каждые 6–8 часов активной работы.

⚠️ При записи больших файлов — маленькими блоками через cat >> по 60-80 строк. Длинные вставки рвутся.

⚠️ В названиях задач/фаз писать полностью — не «B1», а «генерация сопроводительных писем через AITUNNEL». Аббревиатуры понимают не все.

🎯 ЦЕЛЬ И РОЛЬ
Автоматизация поиска и оценки вакансий на hh.ru через ИИ-агента:
поиск similar_vacancies → LLM-оценка → сопровод → TG-карточка →
ручной отклик (ENABLE_REAL_APPLY=true, AUTO_APPLY_ENABLED=false).

Роль: Александр Сверинов — заказчик и оператор (ищет работу).
Upstream: fikstt2 (Евгений), github.com/fikstt2/hh-ai-agent — источник cherry-pick, PR не делаем.
Форк: github.com/asverinov/hh-ai-agent — основная линия.
Ветка работы: my-local-work.

🏗️ АРХИТЕКТУРА
Хост: Windows, ноут HP, i5, 32 ГБ. Hyper-V → Ubuntu 24.04 (виртуалка hh-ag).
Сеть: InternalSwitch, NAT, IP 192.168.100.10. SSH: ssh a@192.168.100.10.
Remote'ы git:

origin = asverinov/hh-ai-agent

upstream = fikstt2/hh-ai-agent

Ветка my-local-work привязана к origin/my-local-work

Credentials: токен в ~/.git-credentials (mode 600). Push без пароля.

Telegram: @asverinov_bot, user_id=215424700.

gpt2giga: внутри виртуалки на localhost:8090 (Anthropic-compatible /v1/messages). Legacy — GigaChat-2-Pro больше не используется в production, но сервис жив.

AITUNNEL: внешний OpenAI-совместимый шлюз, https://api.aitunnel.ru/v1.
Ключ в .env (AITUNNEL_API_KEY, AITUNNEL_BASE_URL).
Баланс на 22.10.2026: ~1027 ₽ (было 1079, потрачено ~52₽ на DeepSeek за прогон 96 вызовов).

4 резюме на hh.ru (config/resumes.yaml):

RP — Руководитель проектов (активное, поиск идёт по нему)

RSP — Руководитель сервисных проектов

RPO — Руководитель проектного офиса

ROP — Руководитель отдела поддержки

Search: пока только по RP. A2 (все 4) — не начато.

⚙️ АВТОСТАРТ СЕРВИСОВ
4 systemd-user-юнита в ~/.config/systemd/user/:

Юнит	Что запускает	Порт/дисплей
hh-xvfb.service	Xvfb :99 (1280x800x24) + ExecStartPre чистит lock	:99
hh-fluxbox.service	fluxbox WM	:99
hh-x11vnc.service	x11vnc (VNC-сервер)	:5900
hh-gpt2giga.service	gpt2giga --port 8090 --mode DEV	:8090
Проверка:

bash
systemctl --user --no-pager -l status hh-xvfb hh-fluxbox hh-x11vnc hh-gpt2giga | grep -E "●|Active:"
ss -tlnp 2>/dev/null | grep -E ":(8090|5900)\b"
VNC-вход на hh.ru (если сессия протухла): TightVNC Viewer → 192.168.100.10:5900 (пароль пустой) → Chromium → войти на hh.ru → вернуться в SSH, ENTER.

Агента запускаем вручную:

bash
cd ~/hh-ai-agent && source venv/bin/activate
unset HH_ACCESS_TOKEN HH_REFRESH_TOKEN HH_TOKEN_EXPIRES_AT
export DISPLAY=:99
python3 main.py
Остановка — Ctrl+C в его вкладке или pkill -INT -f "python3 main.py".

🧠 МОДЕЛИ — D1 и D2 сделаны (22 окт)
Сопроводы (D1, коммит 7522911):

COVER_LETTER_MODEL=qwen3.7-flash (было deepseek-v3.2)

Через AITUNNEL + system_override=ds_cover_letter_digest.md (~32K символов)

session_id="cover-letter-main" для кэша

В main.py: _cl_kwargs["model_override"] = settings.llm.cover_letter_model

Оценка (D2, коммит 8c4618a):

ASSESS_MODEL=deepseek-v3.2

Через AITUNNEL + system_override=ds_context_digest.md (~34K символов)

session_id="context-main" для кэша

В main.py: глобалы _context_analyzer, _context_digest, _context_session_id; assess() использует их с model_override/session_id/system_override

_load_context_digest() + _create_cover_letter_analyzer() (переиспользуется как фабрика)

GigaChat-2-Pro: legacy, в LLM_MODEL остался, но production не использует.

Результаты калибровки сопроводов:

Модель	v3.1	v3.2	v3.3.1 (прогноз)
qwen3.7-flash	4.58	4.58	—
GigaChat-3-Ultra	4.08	4.50	—
qwen выбран production-моделью сопроводов. gc3ultra подтянулся после v3.2 — можно как fallback, но qwen стабильнее (не галлюцинирует).

Стоимость DeepSeek v3.2: ~36₽/1M input, ~54₽/1M output. input_tokens ~14–19K на вакансию (с дайджестом). Кэш через session_id работает, но код не сохраняет cached_tokens — только prompt_tokens/completion_tokens.

AITUNNEL возвращает cost_rub в usage — можно мониторить точную стоимость. Пока не парсим.

✅ ЧТО СДЕЛАНО В AI5/AI6
До 22.10 (17 коммитов):

Observability: description, vacancy_id, prompt_version, raw_response в llm_requests

Правила 18a/23/24 в промпте оценки

Пагинация до конца + seen_vacancies + evaluations

P4 калибровка (19 вакансий, 11 моделей)

A1/A3 (best_resume + dropdown hh.ru)

B1/B2 (сопроводы через AITUNNEL)

B3 (сопроводы в pipeline, 27 pending)

Тест 5 моделей сопроводов (60 писем) + ретест 2 моделей (24 письма)

v3.2 digest (правила конкретики и «только реальный опыт»)

v3.3.1 digest (грейд/масштаб, P&L, betting, годы, дубли, Example 8)

За 22.10 (коммиты 7522911–8c4618a):

7522911 D1: cover_letter_model=qwen3.7-flash

021f6bf fix(tg_bot): кнопка «Показать рекомендованные» (не проходила фильтр регистрации)

7cd3041 docs: ds_cover_letter_digest.md v3.3.1

8c4618a D2 + closed-vacancy + OAuth endpoint:

config.py: assess_model + парсинг

main.py: _load_context_digest + _context_analyzer глобалы + assess через override

main.py: VACANCY_REMOVED/VACANCY_HIDDEN → EXPIRED (без circuit breaker)

hh_client.py: PageState.VACANCY_HIDDEN + classify_page ловит «Вам недоступна»

hh_client.py: endpoint api.hh.ru/oauth/token → hh.ru/oauth/token

database.py: ALLOWED_TRANSITIONS[DISCOVERED] += EXPIRED

scripts/evaluate_discovered.py — оценка discovered без CloakBrowser

docs/HH-OAUTH-RECOVERY.md — процедура восстановления токена (169 строк)

Не закоммичено (на момент обновления): profile.yaml (в .gitignore) — восстановлены ЦИК России, Министерство обороны, Джет (без запрета), «участвовал в подготовке к аудитам», «организовал производство ПАК», Память народа + МО.

📊 P4: МУЛЬТИ-LLM КАЛИБРОВКА (краткая ссылка)
Калибровка 19 вакансий (10 positive / 10 negative):

deepseek-v3.2 (AITUNNEL + обогащённый промпт): F1=1.00

qwen3.7-flash (AITUNNEL + обогащённый промпт): F1=1.00

GigaChat-2-Pro: F1=0.50 — аутсайдер, отказ от production

GigaChat-3-Ultra: F1=0.67

Кэш DeepSeek через session_id: 89% экономии

Подробности — в старых коммитах d86ee70, 07d0062, 28e412e. В этой сессии P4 не пересматривался.

🔑 HH OAUTH — КРАТКАЯ СПРАВКА
Полная процедура восстановления токена — в docs/HH-OAUTH-RECOVERY.md (169 строк).

Суть: access_token живёт 14 дней, refresh_token ~60 дней. hh.ru закрыл applicant-API для новых приложений, поэтому используем firehh (публичный Android-client_id).

Refresh-логика в коде: только endpoint исправлен (api.hh.ru → hh.ru). Полный refresh flow не wired: client_id/client_secret не передаются, новая пара не сохраняется в .env, нет проактивного триггера. Каждые 14 дней — ручной firehh auth login (по инструкции).

Backlog по refresh: см. раздел «Что НЕ закрыто».

📋 ПРИОРИТЕТЫ НА 22 ОКТЯБРЯ
Фаза «Переоценка 247 rejected_by_llm»
✅ Сброс 247 → discovered, чистка seen_vacancies

🟡 evaluate_discovered.py работает в фоне (PID 76166, ~162 из 169 к 06:00 22.10)

⏸ 45 discovered без description — не оценены, ждут решения

Фаза «D2 — оценка через DeepSeek»
✅ Код (коммит 8c4618a)

✅ Smoke на 1 вакансии

✅ Запущен прогон 169 discovered

⏸ Валидация на калибровке (19 эталонных) — не сделана

Фаза «Closed-vacancy handling»
✅ VACANCY_HIDDEN + VACANCY_REMOVED → EXPIRED (без circuit breaker)

⏸ Тест на живой закрытой вакансии — не сделан

Фаза «OAuth refresh»
✅ Endpoint fix (8c4618a)

⏸ Полный refresh flow (client_id/secret + сохранение в .env) — не wired

📝 Процедура описана в docs/HH-OAUTH-RECOVERY.md

Backlog (не срочно)
Circuit breaker не сбрасывает paused=True — агент зависает на 6 часов

Комментарий к «Пропустить» в TG + логирование причин фильтра

Проверка rejected_by_filter (junior-маркеры, «интерн» в «интернет»)

Кэш DeepSeek — не сохраняем cached_tokens/cost_rub

apply_failed (12) — растёт, причина не выяснена

A2 (search по 4 резюме)

Расширение калибровки с 19 до 30

🎬 СЛЕДУЮЩИЙ ШАГ
Утро 22.10:

Проверить evaluate_discovered.py — завершился ли (pgrep -af evaluate_discovered.py)

Посмотреть === DONE === в /tmp/eval-169-*.log

Запустить main.py → bootstrap отправит новые pending в TG

Разгрести карточки в TG (старые + новые)

Затем:

Валидация D2 на калибровке (19 эталонных, F1=1.00 — ожидание)

Тест closed-vacancy на живой закрытой вакансии

45 без desc: либо read_vacancy через CloakBrowser, либо выкинуть

Circuit breaker fix (сброс paused после успешного цикла)

Refresh flow (client_id/secret + сохранение .env)

📁 КЛЮЧЕВЫЕ ФАЙЛЫ
Файл	Что там
profile.yaml (в корне!)	AI/LLM-якоря, технологии, стоп-факторы. В .gitignore
config/resumes.yaml	4 ID резюме (RP active, RSP/RPO/ROP ждут)
.env	LLM_PROVIDER, LLM_MODEL, ASSESS_MODEL=deepseek-v3.2, COVER_LETTER_MODEL=qwen3.7-flash, AITUNNEL_*, ENABLE_COVER_LETTER_GENERATION=true, MAX_PAGES_PER_QUERY=0, MAX_PENDING_RECOMMENDATIONS=60, TG_REPORT_PER_PAGE=true
ai_analyzer.py	Промпт (правила 1–24 + 18a), _request (с model_override, system_override, session_id), assess(), generate_cover_letter()
llm/managed.py	_generate — retry, dirtyjson, blacklist, prompt_version
llm/types.py	LLMRequest (vacancy_id, session_id)
llm/providers/openai_compatible.py	Адаптер AITUNNEL (session_id в payload)
database.py	Миграции, applied_jobs, seen_vacancies, evaluations, ALLOWED_TRANSITIONS
main.py	process_vacancy, run_search_cycle, _load_context_digest, _context_analyzer, record_evaluation, check_llm, run()
hh_client.py	_is_junior_title, search_by_resume (пагинация), _get_resume_id, submit_application, read_vacancy, classify_page (VACANCY_HIDDEN)
tg_bot.py	Bootstrap, notify_cycle_done, show_pending (карточки), reply, 📄 Стр. N/M
cover_letter.py	format_portfolio_footer, cover_letter_validation_error (лимит 1500)
scripts/backup_db.sh	Бэкап agent.db с ротацией (10 последних)
scripts/evaluate_models.py	Прогон моделей (--provider gigachat|aitunnel, --max-tokens, --system-from-file, --session-id)
scripts/compare_all.py	Матрица N×M, consensus, pairwise, CSV
scripts/compare_vs_manual.py	Precision/recall/F1 против ручной разметки
scripts/generate_cover_letters.py	Генерация сопроводов через AITUNNEL с дайджестом
scripts/evaluate_discovered.py	Оценка discovered без CloakBrowser (D2 pipeline)
scripts/test_cover_letter_models.py	Сравнение моделей на сопроводах
docs/HH-OAUTH-RECOVERY.md	Процедура восстановления OAuth-токена
docs/datasets/ds_context_digest.md	Дайджест для оценки (~34K символов)
docs/datasets/ds_cover_letter_digest.md	Дайджест для сопроводов (~32K символов) v3.3.1
docs/datasets/calibration_set.json	20 калибровочных (в git)
docs/datasets/manual_labels_20261017.txt	Ручная разметка (в git)
🛠️ КОМАНДЫ
Запуск агента:

bash
cd ~/hh-ai-agent && source venv/bin/activate
unset HH_ACCESS_TOKEN HH_REFRESH_TOKEN HH_TOKEN_EXPIRES_AT
export DISPLAY=:99
python3 main.py
Бэкап БД:

bash
cd ~/hh-ai-agent && ./scripts/backup_db.sh <метка>
Healthcheck LLM:

bash
cd ~/hh-ai-agent && source venv/bin/activate
timeout 60 python3 main.py --check-llm
Оценка discovered без CloakBrowser:

bash
cd ~/hh-ai-agent && source venv/bin/activate
python3 scripts/evaluate_discovered.py --dry-run
nohup python3 scripts/evaluate_discovered.py > /tmp/eval-$(date +%H%M).log 2>&1 &
Прогон моделей для калибровки:

bash
cd ~/hh-ai-agent
set -a; source .env; set +a
python3 scripts/evaluate_models.py --calibration --provider aitunnel \
  --models deepseek-v3.2,qwen3.7-flash --max-tokens 16000 --delay 3
Сравнение с ручной разметкой:

bash
cd ~/hh-ai-agent && python3 scripts/compare_vs_manual.py
Генерация сопроводов (скрипт):

bash
cd ~/hh-ai-agent
set -a; source .env; set +a
python3 scripts/generate_cover_letters.py --calibration --show
Push в форк: cd ~/hh-ai-agent && git push
Pull из upstream (cherry-pick): git fetch upstream && git cherry-pick <commit>

Состояние БД:

bash
sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;"
sqlite3 -header -column agent.db "SELECT model, COUNT(*) FROM evaluations GROUP BY model;"
sqlite3 agent.db "SELECT COUNT(*) FROM seen_vacancies;"
Тесты (по одному, не все сразу):

bash
python3 -m pytest tests/test_config.py -q --no-header
python3 -m pytest tests/test_hh_client.py -q --no-header
python3 -m pytest tests/test_approval.py -q --no-header
python3 -m pytest tests/ -q --no-header   # полный, baseline 27 fail
⚠️ ВАЖНЫЕ НЮАНСЫ
os.environ перезаписывает .env в config.py:values.update(os.environ). Если делал source .env — в этом shell main.py получит старые значения. Правило: агент запускать в чистом shell, или unset HH_* перед запуском.

CloakBrowser free v146, 1 concurrent session. Не жать две кнопки одновременно. Если запущен main.py, read_vacancy/submit_application не работают из другого процесса.

AUTO_APPLY_ENABLED=false — только ручное approval через TG.

ENABLE_COVER_LETTER_GENERATION=true — сопроводы через AITUNNEL qwen.

MAX_APPLICATIONS_PER_DAY=50, но на первый отклик — по одной карточке.

AITUNNEL баланс: ~1027 ₽ (22.10.2026). DeepSeek v3.2 ~0.6-0.7₽/вызов, qwen ~0.3-0.4₽/вызов с кэшем.

--max-tokens 16000 для DeepSeek/Qwen (reasoning-модели).

session_id должен быть одинаковым для всей серии вызовов (иначе кэш не активируется).

Кэш TTL ~10 минут. Если пауза >10 мин — первый вызов холодный.

circuit breaker зависает: после paused=True не сбрасывается. Агент висит на 6+ часов. Патч: сбрасывать control.paused = False после успешного search_cycle_finished со state=completed.

VACANCY_HIDDEN — «Вам недоступна эта вакансия» (скрыл работодатель). VACANCY_REMOVED — «Вакансия в архиве». Оба → EXPIRED, не PAGE_STRUCTURE_CHANGED, не circuit breaker.

evaluate_discovered.py — не использует main.py:process_vacancy, только assess() + request_approval. Не шлёт карточки в TG — они появятся после следующего запуска main.py (bootstrap).

Правило «Песец» — mascots/tracker.py, шлёт картинки песца в TG. К LLM отношения не имеет.

.gitignore: .env, config/, agent.db, *.bak-*, docs/datasets/*.txt, docs/datasets/compare.py, docs/datasets/export_raw.py, docs/datasets/calibration_*.csv

Xvfb 1280x800x24 (фактическое).

При записи больших файлов — маленькими блоками cat >> по 60-80 строк.

В названиях задач — писать полностью.

🔗 ССЫЛКИ
Upstream: https://github.com/fikstt2/hh-ai-agent

Форк: https://github.com/asverinov/hh-ai-agent

Ветка: https://github.com/asverinov/hh-ai-agent/tree/my-local-work

hh.ru резюме РП: https://hh.ru/resume/bc7c9e1aff0fe6b4ae0039ed1f614361526447

Telegram: @asverinov_bot

AITUNNEL: https://aitunnel.ru

firehh (npm): @bagasek/firehh

📄 РЕЗЮМЕ (для P4 / multi-resume)
4 резюме:

РП (Руководитель проектов) — основное, поиск идёт по нему

РСП (Сервис-менеджер)

РПО (Руководитель проектного офиса)

РОП (Руководитель отдела поддержки)

SuitabilityResult.best_resume уже выбирает одно из 4 (RP/RSP/RPO/ROP).
Задел: сравнение вакансий со всеми резюме + search по 4 (A2).

📌 БЫСТРЫЙ СТАРТ В НОВОЙ СЕССИИ
Скажи «дайджест» — получишь cat docs/AI5-digest.md.

Проверь, что агент остановлен: pgrep -af "python3 main.py".

Проверь сервисы: ss -tlnp | grep -E ":(8090|5900)".

Проверь БД: sqlite3 -header -column agent.db "SELECT status, COUNT(*) FROM vacancies GROUP BY status;".

Проверь AITUNNEL баланс: curl через API.

Следующая задача: дождаться evaluate_discovered.py, запустить main.py, разгрести TG. Затем — closed-vacancy тест, circuit breaker fix, refresh flow.

Состояние на 22.10.2026, 01:00:

discovered: 162 (169 оценятся к 06:00) + 45 без desc

pending_approval: 20

rejected_by_llm: 21

applied: 25, skipped: 31, expired: 3, rejected_by_filter: 30

analysis_failed: 5, apply_failed: 12

seen_vacancies: 1517

AITUNNEL баланс: ~1027 ₽

DeepSeek вызовов: 96, ~61 ₽

HEAD: 8c4618a

Создан в AI6, коммит 8c4618a. При обновлении — старую версию сохраняем как docs/AI5-digest.md.bak-<timestamp>.

