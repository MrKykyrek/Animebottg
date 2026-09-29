"""
Telegram-бот расписания СибУПК.
Парсит http://old.sibupk.su/services/shedule_new/index.php?mode=1

Установка:
    pip install aiogram beautifulsoup4 aiohttp apscheduler python-dotenv

Запуск:
    python main.py           # обычный запуск
    python main.py --test    # самопроверка парсера без Telegram
"""

import asyncio
import logging
import sys
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import os

# ==================== КОНФИГ ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

ID_FORMA = os.getenv("ID_FORMA", "1")
ID_FAK = os.getenv("ID_FAK", "1")
KURS = os.getenv("KURS", "1")
GROUP_NAME = os.getenv("GROUP_NAME", "ПК-61 (Поварское и кондитерское дело)")

URL = "http://old.sibupk.su/services/shedule_new/index.php?mode=1"

PARSE_HOUR = int(os.getenv("PARSE_HOUR", "7"))
PARSE_MINUTE = int(os.getenv("PARSE_MINUTE", "30"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Novosibirsk")
# ================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# ==================== ХЕЛПЕРЫ ====================
async def fetch_ranges(session: aiohttp.ClientSession) -> list[str]:
    """Получает список доступных интервалов с сайта."""
    data = {
        "id_Forma": ID_FORMA,
        "id_Fak": ID_FAK,
        "Kurs": KURS,
        "NamePodGrup": GROUP_NAME,
        "RangeNedel": "",
    }
    async with session.post(URL, data=data, timeout=20) as resp:
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


# ==================== ПАРСЕР ====================
def parse_schedule_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table")
    if not table:
        return "⚠️ Таблица расписания не найдена."

    out_lines: list[str] = []
    for row in table.find_all("tr"):
        th = row.find("th", colspan="5")
        if th:
            text = th.get_text(" ", strip=True)
            if "НЕДЕЛЯ" in text.upper() and "№ Пары" not in text:
                out_lines.append(f"\n━━━ {text} ━━━")
            elif "(" in text and ")" in text and "№" not in text:
                out_lines.append(f"\n📅 {text}")
            continue

        tds = row.find_all("td")
        if len(tds) < 5:
            continue

        num_time = tds[0].get_text(" ", strip=True)
        subject = tds[1].get_text(" ", strip=True)
        room = tds[3].get_text(" ", strip=True)
        teacher = tds[4].get_text(" ", strip=True)

        out_lines.append(
            f"  {num_time}\n"
            f"  📚 {subject}\n"
            f"  🚪 {room}   👤 {teacher}\n"
        )

    if not out_lines:
        return "⚠️ Не удалось разобрать расписание."

    return "\n".join(out_lines).strip()


# ==================== СБОРКА СООБЩЕНИЯ ====================
async def build_schedule_message() -> str:
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
    body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return header + body


# ==================== ОТПРАВКА ====================
async def send_schedule(chat_id: int | None = None) -> None:
    target = chat_id or CHAT_ID
    if not target:
        logging.warning("CHAT_ID не задан — пропускаю автоотправку")
        return
    try:
        text = await build_schedule_message()
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
        "/test — проверить, что парсер работает\n"
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


@dp.message(Command("test"))
async def cmd_test(message: Message) -> None:
    """Проверка всех этапов: интервалы → скачивание → парсинг."""
    await message.answer("🔍 Проверяю парсер...")

    report = ["<b>Отчёт о проверке</b>\n"]

    # 1. Интервалы
    try:
        async with aiohttp.ClientSession() as session:
            ranges = await fetch_ranges(session)
        if ranges:
            report.append(f"✅ Интервалы получены: {len(ranges)} шт.")
            report.append(f"   Первый: <code>{ranges[0]}</code>")
        else:
            report.append("❌ Интервалы не найдены (пустой список).")
    except Exception as e:
        report.append(f"❌ Ошибка получения интервалов: <code>{e}</code>")
        ranges = []

    if not ranges:
        await message.answer("\n".join(report), parse_mode="HTML")
        return

    # 2. Текущий интервал
    current = pick_current_range(ranges)
    report.append(f"✅ Выбран интервал: <code>{current}</code>")

    # 3. Скачивание HTML
    try:
        html = await fetch_schedule_html(current)
        report.append(f"✅ HTML получен: {len(html)} символов")
    except Exception as e:
        report.append(f"❌ Ошибка скачивания: <code>{e}</code>")
        await message.answer("\n".join(report), parse_mode="HTML")
        return

    # 4. Парсинг
    try:
        parsed = parse_schedule_html(html)
        if parsed.startswith("⚠️"):
            report.append(f"❌ {parsed}")
        else:
            report.append(f"✅ Расписание разобрано: {len(parsed)} символов")
            preview = parsed[:500].replace("<", "&lt;").replace(">", "&gt;")
            report.append(f"\n<b>Превью:</b>\n<pre>{preview}...</pre>")
    except Exception as e:
        report.append(f"❌ Ошибка парсинга: <code>{e}</code>")

    await message.answer("\n".join(report), parse_mode="HTML")


# ==================== САМОПРОВЕРКА БЕЗ TELEGRAM ====================
async def run_self_test() -> None:
    """Запускается с аргументом --test. Проверяет парсер без Telegram."""
    print("=" * 50)
    print("САМОПРОВЕРКА БОТА (без Telegram)")
    print("=" * 50)

    # 1. Проверка конфига
    print("\n[1] Конфиг:")
    print(f"    BOT_TOKEN: {'✅ задан' if BOT_TOKEN else '❌ пусто'}")
    print(f"    CHAT_ID:   {CHAT_ID}")
    print(f"    GROUP:     {GROUP_NAME}")

    # 2. Интервалы
    print("\n[2] Получение интервалов...")
    try:
        async with aiohttp.ClientSession() as session:
            ranges = await fetch_ranges(session)
        print(f"    ✅ Получено {len(ranges)} интервалов")
        for r in ranges[:3]:
            print(f"       • {r}")
        if len(ranges) > 3:
            print(f"       ... и ещё {len(ranges) - 3}")
    except Exception as e:
        print(f"    ❌ Ошибка: {e}")
        return

    if not ranges:
        print("    ❌ Интервалы пустые")
        return

    # 3. Текущий интервал
    current = pick_current_range(ranges)
    print(f"\n[3] Текущий интервал: {current}")

    # 4. Скачивание
    print("\n[4] Скачивание HTML...")
    try:
        html = await fetch_schedule_html(current)
        print(f"    ✅ Получено {len(html)} символов")
    except Exception as e:
        print(f"    ❌ Ошибка: {e}")
        return

    # 5. Парсинг
    print("\n[5] Парсинг расписания...")
    parsed = parse_schedule_html(html)
    if parsed.startswith("⚠️"):
        print(f"    ❌ {parsed}")
        return
    print(f"    ✅ Разобрано {len(parsed)} символов")
    print("\n" + "=" * 50)
    print("ПРЕВЬЮ (первые 1500 символов):")
    print("=" * 50)
    print(parsed[:1500])
    if len(parsed) > 1500:
        print(f"\n... и ещё {len(parsed) - 1500} символов")
    print("\n" + "=" * 50)
    print("✅ САМОПРОВЕРКА ПРОЙДЕНА")
    print("=" * 50)


# ==================== ЗАПУСК ====================
async def main() -> None:
    scheduler.add_job(
        send_schedule,
        "cron",
        hour=PARSE_HOUR,
        minute=PARSE_MINUTE,
        day_of_week="mon-fri",
    )
    scheduler.start()

    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(run_self_test())
    else:
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            logging.info("Бот остановлен")