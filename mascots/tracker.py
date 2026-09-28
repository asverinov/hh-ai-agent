import random
import shutil
import sys
from pathlib import Path
try:
    from PIL import Image, ImageEnhance
except ImportError:
    Image = None
    ImageEnhance = None

_PESEC_ENABLED = False
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            if sys.stdout and hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            print(text)
        except Exception:
            clean = text.encode("ascii", errors="replace").decode("ascii")
            print(clean)


def enable_pesec_mode() -> None:
    global _PESEC_ENABLED
    _PESEC_ENABLED = True
    if sys.platform == "win32":
        try:
            if sys.stdout and hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def is_pesec_enabled() -> bool:
    return _PESEC_ENABLED


# --- 1. Главный арт при старте ---

def show_pesec_startup() -> None:
    """Выводит главное изображение песцов при старте агента."""
    if not _PESEC_ENABLED or Image is None:
        return

    target_file = None
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        candidate = ASSETS_DIR / f"pesec{ext}"
        if candidate.exists():
            target_file = candidate
            break

    if target_file:
        try:
            img = Image.open(target_file).convert("RGB")
            term_cols = shutil.get_terminal_size(fallback=(100, 24)).columns
            width = max(50, min(term_cols - 6, 120))

            img = ImageEnhance.Sharpness(img).enhance(1.6)
            img = ImageEnhance.Contrast(img).enhance(1.15)

            height = int(width * (img.height / img.width) * 0.58)
            if height % 2 != 0:
                height += 1

            img = img.resize((width, height), Image.Resampling.LANCZOS)
            indent = " " * max(0, (term_cols - width) // 2)

            for y in range(0, height, 2):
                row = [indent]
                for x in range(width):
                    r_top, g_top, b_top = img.getpixel((x, y))
                    r_bot, g_bot, b_bot = (
                        img.getpixel((x, y + 1)) if (y + 1 < height) else (0, 0, 0)
                    )
                    row.append(
                        f"\033[38;2;{r_top};{g_top};{b_top}m\033[48;2;{r_bot};{g_bot};{b_bot}m▀"
                    )
                row.append("\033[0m")
                _safe_print("".join(row))
        except Exception:
            pass

    _safe_print(
        "\n\033[1;36m  🐾 [PESEC AGENT MODE ACTIVATED] Песцы вышли на охоту за офферами! 🐾\033[0m\n"
    )


# --- 2. Получение картинок для Telegram ---

def get_pesec_image(reaction: str | None = None) -> Path | None:
    """Ищет картинку песца в папке assets под тип реакции или берёт случайную."""
    if not _PESEC_ENABLED or not ASSETS_DIR.exists():
        return None

    exts = (".png", ".jpg", ".jpeg", ".webp")

    # Поиск точечной картинки (например: pesec_approve.jpg, pesec_applied.jpg)
    if reaction:
        for ext in exts:
            candidate = ASSETS_DIR / f"pesec_{reaction}{ext}"
            if candidate.exists():
                return candidate

    # Фолбэк на любые изображения в assets
    candidates: list[Path] = []
    for ext in exts:
        candidates.extend(ASSETS_DIR.glob(f"*pesec*{ext}"))
        candidates.extend(ASSETS_DIR.glob(f"*песец*{ext}"))

    valid = [p for p in candidates if p.is_file()]
    return random.choice(valid) if valid else None


# --- 3. Реакции на события в консоли (бэкенд) ---

def pesec_on_analyzing(title: str) -> None:
    """Когда агент начинает оценивать вакансию."""
    if not _PESEC_ENABLED:
        return
    _safe_print(
        f"\033[38;2;130;190;240m  (\\__/)\n  ( •.•) 🔍 Ириска вчитывается в требования: \033[1m{title}\033[0m"
    )


def pesec_on_approve(title: str, score: int | float | None = None) -> None:
    """Когда вакансия подошла и одобрена."""
    if not _PESEC_ENABLED:
        return
    score_str = f" [Score: {score}]" if score is not None else ""
    _safe_print(
        f"\033[38;2;120;220;120m  (\\__/)\n"
        f"  ( ^.^ )/ ✨ ГОДНОТА! Ириска одобряет{score_str}: \033[1m{title}\033[0m"
    )


def pesec_on_reject(title: str, reason: str = "") -> None:
    """Когда вакансия отклонена фильтром или LLM."""
    if not _PESEC_ENABLED:
        return
    reason_str = f" ({reason})" if reason else ""
    _safe_print(
        f"\033[38;2;230;110;110m  (\\__/)\n"
        f"  ( -.-) 💤 песцы зевнули и скипнули: \033[1m{title}\033[0m\033[38;2;200;100;100m{reason_str}\033[0m"
    )


def pesec_on_applied(title: str) -> None:
    """Когда отклик успешно отправлен."""
    if not _PESEC_ENABLED:
        return
    _safe_print(
        f"\033[38;2;255;215;0m  (\\__/)\n"
        f"  ( >.<) 🚀 ОТКЛИК УЛЕТЕЛ! Резюме доставлено на: \033[1m{title}\033[0m"
    )