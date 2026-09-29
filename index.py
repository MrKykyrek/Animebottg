"""
Telegram-бот расписания СибУПК.
Парсит http://old.sibupk.su/services/shedule_new/index.php?mode=1

Установка:
    pip install aiogram beautifulsoup4 aiohttp apscheduler

Запуск:
    python bot.py
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup

# ==================== КОНФИГ ====================
BOT_TOKEN = "8387547147:AAGki73PH8mEYt0c_KQoo3BmKidID1b1emg"
CHAT_ID = 0                    # ID чата/канала, куда слать авто-расписание

# Параметры группы — как на сайте
ID_FORMA = "1"                 # 1 = очная
ID_FAK = "1"                   # 1 = Торгово-технологический факультет
KURS = "1"                     # 1 курс
GROUP_NAME = "ПК-61 (Поварское и кондитерское дело)"

URL = "http://old.sibupk.su/services/shedule_new/index.php?mode=1"

PARSE_HOUR = 7                 # во сколько отправлять авто-расписание
PARSE_MINUTE = 30
TIMEZONE = "Asia/Novosibirsk"  # СибУПК в Новосибирске
# ================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# ==================== ХЕЛПЕРЫ ====================
def get_current_range() -> str:
    """Возвращает интервал 2 недель, в который попадает сегодня."""
    today = datetime.now().date()
    # Ориентируемся на ближайший понедельник и берём текущую пару недель
    # На сайте интервалы заданы вручную, но мы можем запросить ближайший
    # Для простоты — берём список из формы и находим подходящий
    # Здесь упрощённо: возвращаем текущий интервал, который найдём парсингом формы
    return ""  # будет подставлено динамически


async def fetch_ranges(session: aiohttp.ClientSession) -> list[str]:
    """Получает список доступных интервалов с сайта."""
    data = {
        "id_Forma": ID_FORMA,
        "id_Fak": ID_FAK,
        "Kurs": KURS,
        "NamePodGrup": GROUP_NAME,
        "RangeNedel": "",
    }
    async with session.post(URL, data=data) as resp:
        html = await resp.text()
    soup = BeautifulSoup(html, "html.parser")
    ranges = []
    for opt in soup.find_all("option"):
        val = opt.get("value", "").strip()
        if val and " - " in val:
            ranges.append(val)
    return ranges


def pick_current_range(ranges: list[str]) -> str:
    """Выбирает интервал, в который попадает сегодняшняя дата."""
    today = datetime.now().date()
    for r in ranges:
        try:
            start_s, end_s = r.split(" - ")
            start = datetime.strptime(start_s.strip(), "%d.%m.%Y").date()
            end = datetime.strptime(end_s.strip(), "%d.%m.%Y").date()
            if start <= today <= end:
                return r
        except ValueError:
            continue
    # если сегодня вне интервалов — берём ближайший будущий
    future = []
    for r in ranges:
        try:
            start_s = r.split(" - ")[0].strip()
            start = datetime.strptime(start_s, "%d.%m.%Y").date()
            if start >= today:
                future.append((start, r))
        except ValueError:
            continue
    if future:
        future.sort()
        return future[0][1]
    return ranges[0] if ranges else ""


async def fetch_schedule_html(range_nedel: str) -> str:
    """Скачивает HTML с расписанием для указанного интервала."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": URL,
    }
    data = {
        "id_Forma": ID_FORMA,
        "id_Fak": ID_FAK,
        "Kurs": KURS,
        "NamePodGrup": GROUP_NAME,
        "RangeNedel": range_nedel,
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(URL, data=data, timeout=20) as resp:
            resp.raise_for_status()
            return await resp.text()


# ==================== ПАРСЕР РАСПИСАНИЯ ====================
def parse_schedule_html(html: str) -> str:
    """Превращает HTML с расписанием в красивый текст для Telegram."""
    soup = BeautifulSoup(html, "html.parser")

    # Находим таблицу расписания — она с class="table"
    table = soup.find("table", class_="table")
    if not table:
        return "⚠️ Таблица расписания не найдена."

    out_lines: list[str] = []
    current_week = None
    current_day = None

    for row in table.find_all("tr"):
        # Заголовок недели: <th colspan="5">НЕЧЕТНАЯ НЕДЕЛЯ</th>
        th = row.find("th", colspan="5")
        if th:
            text = th.get_text(" ", strip=True)
            if "НЕДЕЛЯ" in text.upper() and "№ Пары" not in text:
                current_week = text
                out_lines.append(f"\n━━━ {current_week} ━━━")
            elif "(" in text and ")" in text and "№" not in text:
                current_day = text
                out_lines.append(f"\n📅 {current_day}")
            continue

        # Обычная строка с парой
        tds = row.find_all("td")
        if len(tds) < 5:
            continue

        num_time = tds[0].get_text(" ", strip=True)
        subject = tds[1].get_text(" ", strip=True)
        _stream = tds[2].get_text(" ", strip=True)
        room = tds[3].get_text(" ", strip=True)
        teacher = tds[4].get_text(" ", strip=True)

        # Сокращаем "(лек)", "(лаб)", "(с)" и т.п. — оставляем
        out_lines.append(
            f"  {num_time}\n"
            f"  📚 {subject}\n"
            f"  🚪 {room}   👤 {teacher}\n"
        )

    if not out_lines:
        return "⚠️ Не удалось разобрать расписание."

    return "\n".join(out_lines).strip()


# ==================== ОТПРАВКА ====================
async def build_schedule_message() -> str:
    """Собирает полный текст расписания для текущей недели."""
    async with aiohttp.ClientSession() as session:
        ranges = await fetch_ranges(session)

    if not ranges:
        return "⚠️ Не удалось получить список интервалов."

    current = pick_current_range(ranges)
    html = await fetch_schedule_html(current)

    header = (
        f"📖 <b>Расписание</b>\n"
        f"Группа: <b>{GROUP_NAME}</b>\n"
        f"Интервал: <b>{current}</b>\n"
    )
    body = parse_schedule_html(html)

    # Telegram HTML: экранируем спецсимволы в body
    body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return header + body


async def send_schedule(chat_id: int | None = None) -> None:
    target = chat_id or CHAT_ID
    if not target:
        logging.warning("CHAT_ID не задан — пропускаю автоотправку")
        return
    try:
        text = await build_schedule_message()
        # Лимит 4096 символов
        for i in range(0, len(text), 4000):
            await bot.send_message(target, text[i : i + 4000], parse_mode="HTML")
    except Exception as e:
        logging.exception("Ошибка при парсинге/отправке")
        try:
            await bot.send_message(target, f"❌ Ошибка: {e}")
        except Exception:
            pass


# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот расписания СибУПК.\n\n"
        f"Группа: <b>{GROUP_NAME}</b>\n\n"
        "Команды:\n"
        "/schedule — расписание на текущую неделю\n"
        "/chatid — ID этого чата",
        parse_mode="HTML",
    )


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message) -> None:
    await message.answer("⏳ Загружаю расписание...")
    await send_schedule(message.chat.id)


@dp.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    await message.answer(f"ID чата: <code>{message.chat.id}</code>", parse_mode="HTML")


# ==================== ЗАПУСК ====================
async def main() -> None:
    scheduler.add_job(
        send_schedule,
        "cron",
        hour=PARSE_HOUR,
        minute=PARSE_MINUTE,
        day_of_week="mon-fri",  # по будням
    )
    scheduler.start()

    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")