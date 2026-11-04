#!/usr/bin/env python3
"""refresh_hh_token.py — обновляет HH_ACCESS_TOKEN/REFRESH/EXPIRES в .env через hh.ru/oauth/token."""
import asyncio, os, sys, time
from pathlib import Path

ENV = Path(".env")

def read_env() -> dict:
    d = {}
    if not ENV.exists():
        return d
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def write_env(key: str, value: str):
    lines = ENV.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

async def main():
    import aiohttp
    env = read_env()
    refresh = env.get("HH_REFRESH_TOKEN", "").strip()
    if not refresh:
        print("FAIL: HH_REFRESH_TOKEN пуст"); return 1
    print(f"Refresh-токен: len={len(refresh)}, prefix={refresh[:8]}...")
    print("POST https://hh.ru/oauth/token (grant_type=refresh_token)")
    async with aiohttp.ClientSession() as s:
        async with s.post("https://hh.ru/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }) as r:
            payload = await r.json()
            if r.status != 200:
                print(f"FAIL: HTTP {r.status}")
                print(f"  body: {payload}")
                return 1
    new_access = str(payload.get("access_token", "")).strip()
    new_refresh = str(payload.get("refresh_token", refresh)).strip()
    expires_in = int(payload.get("expires_in", 0) or 0)
    if not new_access:
        print("FAIL: access_token пуст в ответе"); return 1
    expires_at = int((time.time() + expires_in) * 1000)
    # Бэкап .env
    import shutil
    if ENV.exists():
        shutil.copy(ENV, str(ENV) + f".bak-{time.strftime('%Y%m%d_%H%M%S')}")
    write_env("HH_ACCESS_TOKEN", new_access)
    write_env("HH_REFRESH_TOKEN", new_refresh)
    write_env("HH_TOKEN_EXPIRES_AT", str(expires_at))
    print(f"OK: обновлён .env")
    print(f"  access: len={len(new_access)}, prefix={new_access[:8]}...")
    print(f"  refresh: len={len(new_refresh)}, prefix={new_refresh[:8]}...")
    print(f"  expires_in: {expires_in} сек, expires_at_ms: {expires_at}")
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
