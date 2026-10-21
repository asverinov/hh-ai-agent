HH OAuth — восстановление токена
Когда читать: в логе hh_token_refresh_failed status=405 или 403 token-expired, поиск возвращает found=0.

Что понадобится: SSH к виртуалке, TightVNC Viewer, 10–15 минут.

TL;DR
Создать обёртку /tmp/chromium-firehh-wrapper.sh (добавляет --no-sandbox к snap-Chromium).

Запустить firehh auth login из /tmp в чистом окружении.

Через VNC войти на hh.ru, нажать синюю «Продолжить».

Забрать токен из ~/.config/firehh/token.json → обновить .env.

Очистить HH_* env, запустить агента.

Контекст
access_token живёт 14 дней, refresh_token — ~60 дней.

С декабря 2025 hh.ru закрыл публичный applicant-API. Своё приложение на dev.hh.ru соискатель зарегистрировать не может.

firehh использует публичный Android-client_id из мобильного приложения hh.ru — официальный OAuth-flow, работает.

Ротация: каждый успешный refresh выдаёт НОВУЮ пару, старый refresh_token инвалидируется. Если новую пару не сохранить — потеряли.

hh.ru блокирует refresh, пока access_token жив: invalid_grant: token not expired. Проактивный refresh за N дней — невозможен.

Состояние refresh в коде
Сделано (коммит 8c4618a):

Endpoint исправлен: api.hh.ru/oauth/token → hh.ru/oauth/token.

Не сделано (backlog):

client_id/client_secret не передаются в refresh-запросе.

Новая пара после refresh не сохраняется в .env — при рестарте агент читает старые, ротация убивает текущий refresh.

Нет проактивного триггера.

Вывод: пока — ручной firehh auth login каждые 14 дней.

Процедура
1. Проверить VNC/Xvfb
bash
ss -tlnp | grep 5900 || {
  export DISPLAY=:99
  pgrep -x Xvfb >/dev/null || (Xvfb :99 -screen 0 1920x1080x24 & sleep 2)
  pgrep -x fluxbox >/dev/null || (fluxbox & sleep 1)
  pgrep -x x11vnc >/dev/null || (x11vnc -display :99 -forever -shared -rfbport 5900 -listen 0.0.0.0 -bg -nopw)
}
2. Проверить firehh
bash
which firehh
# → /home/a/.bun/bin/firehh
firehh --version
Если нет — bun install -g @bagasek/firehh.

3. Создать обёртку для snap-Chromium
Snap-Chromium на Ubuntu 24.04 падает с No usable sandbox без --no-sandbox. firehh не передаёт этот флаг.

bash
cat > /tmp/chromium-firehh-wrapper.sh <<'WRAPPER'
#!/bin/bash
exec /usr/bin/chromium --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"
WRAPPER
chmod +x /tmp/chromium-firehh-wrapper.sh
4. Запустить firehh auth login
Важно: из /tmp, чтобы firehh не подхватил наш .env со старыми токенами. И с unset, потому что в SSH-сессии могут висеть старые HH_*.

bash
cd /tmp
export DISPLAY=:99
pkill -f "firehh auth" 2>/dev/null; sleep 1
nohup env -u HH_ACCESS_TOKEN -u HH_REFRESH_TOKEN -u HH_TOKEN \
    firehh auth login --browser /tmp/chromium-firehh-wrapper.sh --timeout 1800 \
    > /tmp/firehh-auth.log 2>&1 &
5. VNC + кнопка «Продолжить»
TightVNC Viewer → 192.168.100.10:5900 (пароль пустой).

В открывшемся Chromium должна быть вкладка «Вход в приложение».

Убедиться, что аккаунт правильный (Сверинов Александр Григорьевич).

Нажать синюю кнопку «Продолжить» (не «Использовать другой профиль»).

После нажатия hh.ru попытается редиректнуть на hhandroid://oauthresponse?code=.... Chromium покажет пустую страницу или ошибку — это нормально, firehh ловит redirect через DevTools.

6. Проверить token.json
bash
cat /tmp/firehh-auth.log
# → должно быть: {"ok":true,"data":{...,"expires_at":"..."}}

python3 -c "
import json
d = json.load(open('/home/a/.config/firehh/token.json'))
print('access:', d['access_token'][:20], '...')
print('expires_at:', d['expires_at'])
"
7. Обновить .env
bash
cd ~/hh-ai-agent; source venv/bin/activate
python3 <<'PY'
import json
from pathlib import Path
from datetime import datetime

d = json.load(open('/home/a/.config/firehh/token.json'))
access = d['access_token']; refresh = d['refresh_token']
raw = d['expires_at']
if isinstance(raw, int):
    expires_ms = raw
elif isinstance(raw, str):
    dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    expires_ms = int(dt.timestamp() * 1000)

env = Path('.env')
lines = env.read_text().splitlines()
out = []
for line in lines:
    if line.startswith('HH_ACCESS_TOKEN='):   out.append(f'HH_ACCESS_TOKEN={access}')
    elif line.startswith('HH_REFRESH_TOKEN='): out.append(f'HH_REFRESH_TOKEN={refresh}')
    elif line.startswith('HH_TOKEN_EXPIRES_AT='): out.append(f'HH_TOKEN_EXPIRES_AT={expires_ms}')
    else: out.append(line)
env.write_text('\n'.join(out) + '\n')
print(f'OK: .env обновлён, expires_ms={expires_ms}')
PY
8. Проверить токен
bash
python3 -c "
from config import load_settings
import urllib.request, json
s = load_settings()
req = urllib.request.Request('https://api.hh.ru/me',
    headers={'Authorization': f'Bearer {s.hh_access_token}', 'User-Agent': 'hh-ai-agent'})
with urllib.request.urlopen(req) as r:
    d = json.loads(r.read())
    print(f'OK: me.id={d[\"id\"]}')
"
9. Запуск агента
Обязательно unset — env перезаписывает .env в config.py:values.update(os.environ).

bash
cd ~/hh-ai-agent
source venv/bin/activate
unset HH_ACCESS_TOKEN HH_REFRESH_TOKEN HH_TOKEN_EXPIRES_AT
export DISPLAY=:99
python3 main.py
Если что-то пошло не так
Симптом	Причина	Решение
firehh висит, окна нет	snap-Chromium упал без --no-sandbox	Проверить обёртку (шаг 3)
invalid_grant: token not expired	access_token ещё жив	Не refresh'им, работаем на текущем
405 method_not_allowed	Endpoint api.hh.ru/oauth/token	Проверить, что коммит 8c4618a применён
invalid_grant: token has already been refreshed	Ротация: refresh использован	firehh auth login заново
Окно есть, кнопки «Продолжить» нет	Не та вкладка / прокрутка	Открыть именно OAuth-вкладку
load_settings читает старые токены	env перезаписал .env	unset HH_* (шаг 9)
Профилактика
Раз в неделю: grep "hh_token" agent.log | tail -5

Если expires_at < 5 дней — firehh auth login заранее, не ждать протухания

При любом старте агента — unset HH_ACCESS_TOKEN HH_REFRESH_TOKEN HH_TOKEN_EXPIRES_AT

Backlog (что нужно доделать в refresh)
□ Добавить client_id/client_secret в refresh-запрос (hh_client.py:_refresh_token_if_needed)
□ После успешного refresh — записывать новую пару в .env (иначе ротация убивает)
□ Обработка invalid_grant: token not expired как «не ошибка, работаем на текущем»
□ При старте: если expires_at - now < 5 дней → уведомление в TG «пора firehh auth login»

