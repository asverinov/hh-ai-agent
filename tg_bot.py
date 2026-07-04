import asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from config import TG_BOT_TOKEN, TG_USER_ID

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()

async def send_notification(text: str):
    """Отправляет уведомление пользователю."""
    if TG_BOT_TOKEN == "your_bot_token_here" or TG_USER_ID == "your_telegram_id_here":
        print("ОШИБКА: Не настроен Telegram. Уведомление:")
        print(text)
        return
        
    try:
        await bot.send_message(chat_id=TG_USER_ID, text=text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке сообщения в TG: {e}")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if str(message.from_user.id) == TG_USER_ID:
        await message.answer("Привет! Я ваш ИИ-агент для поиска работы на HH.ru. Я буду присылать сюда уведомления.")
    else:
        await message.answer(f"Извините, у вас нет доступа к этому боту.\nВаш ID: <code>{message.from_user.id}</code>\nСкопируйте его и пропишите в файл .env как TG_USER_ID, после чего перезапустите скрипт.")

captcha_event = asyncio.Event()
captcha_solution = ""

async def send_captcha_request(filepath: str, text: str):
    """Отправляет фото капчи пользователю."""
    if TG_BOT_TOKEN == "your_bot_token_here" or TG_USER_ID == "your_telegram_id_here":
        print("ОШИБКА: Не настроен Telegram. Капча сохранена в", filepath)
        return
        
    try:
        from aiogram.types import FSInputFile
        photo = FSInputFile(filepath)
        captcha_event.clear() # Блокируем процесс
        await bot.send_photo(chat_id=TG_USER_ID, photo=photo, caption=text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке капчи в TG: {e}")

@dp.message()
async def handle_text(message: Message):
    """Принимает текст капчи от пользователя."""
    if str(message.from_user.id) != TG_USER_ID:
        return
        
    global captcha_solution
    if not captcha_event.is_set():
        captcha_solution = message.text.strip()
        captcha_event.set()
        await message.answer("✅ Код принят, пробую ввести...")

async def start_bot():
    """Запускает бота (long-polling)"""
    print("Запуск Telegram-бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(start_bot())
