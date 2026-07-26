import asyncio
from database import init_db
from tg_bot import start_bot, send_notification
from hh_client import HHClient

async def agent_loop():
    client = HHClient()
    await client.start()
    
    # Первая авторизация. hh.ru регулярно отдает таймауты и антибот-заглушки,
    # поэтому одна неудачная попытка не должна убивать весь запуск.
    logged_in = False
    for attempt in range(1, 4):
        try:
            logged_in = await client.login_if_needed()
            if logged_in:
                break
            print(f"Авторизация не подтверждена (попытка {attempt} из 3).")
        except Exception as e:
            print(f"Ошибка при проверке авторизации (попытка {attempt} из 3): {e}")
        if attempt < 3:
            print("Повтор через 2 минуты...")
            await asyncio.sleep(120)

    if not logged_in:
        print("Не удалось авторизоваться после 3 попыток. Завершение работы.")
        await client.stop()
        return

    await send_notification("🤖 ИИ-агент успешно запущен и начал работу!")

    try:
        while True:
            try:
                # Ищем вакансии и откликаемся
                await client.search_and_apply(send_notification)
                
                # Проверяем чаты
                await client.check_chats(send_notification)
            except Exception as e:
                print(f"Ошибка в основном цикле агента: {e}")
            
            # Ждем 30 минут перед следующим запуском
            print("Ожидание 30 минут...")
            await asyncio.sleep(1800)
    finally:
        await client.stop()

async def main():
    # Инициализация БД
    init_db()
    print("Инициализация завершена.")
    
    # Запускаем бота и логику агента параллельно
    bot_task = asyncio.create_task(start_bot())
    agent_task = asyncio.create_task(agent_loop())
    
    await asyncio.gather(bot_task, agent_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановка работы.")
