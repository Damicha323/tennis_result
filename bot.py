import os
import asyncio
import csv
import io
from datetime import datetime
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СВОЙ_ТОКЕН")

# ---------- КЛАССИФИКАЦИЯ ВИДА СПОРТА ----------
def classify_sport(tournament, total_str):
    t = (tournament or "").upper()
    try:
        total = float(total_str)
    except:
        total = None

    # Приоритет 1: ключевые слова
    if any(k in t for k in ["ATP", "WTA", "WTT", "TT CUP", "SETKA", "UTR", "WORLD TENNIS", "ЛИГА ПРО"]):
        return "Теннис"
    if any(k in t for k in ["НХЛ", "КХЛ", "NHL", "AHL", "SHL", "LIIGA", "DEL", "ICE HOCKEY", "ФОНБЕТ КХЛ"]):
        return "Хоккей"
    if any(k in t for k in ["НБА", "NBA", "ЕВРОЛИГА", "ЛИГА ВТБ", "BASKET"]):
        return "Баскетбол"
    if any(k in t for k in ["UFC", "KSW", "MMA", "OKTAGON", "LFA", "БОЙ", "МУАЙ", "КИКБОКСИНГ"]):
        return "MMA/Единоборства"
    if any(k in t for k in ["COUNTER", "DOTA", "VALORANT", "OVERWATCH", "CS2", "CS:GO"]):
        return "Киберспорт"
    if any(k in t for k in ["NFL", "NCAA.*ФУТБОЛ"]):
        return "Американский футбол"
    if any(k in t for k in ["PDC", "ДАРТС"]):
        return "Дартс"
    if any(k in t for k in ["СНУКЕР", "SNOOKER", "ПУЛ 9"]):
        return "Снукер/Бильярд"
    if any(k in t for k in ["ШАХМАТ", "CHESS"]):
        return "Шахматы"
    if any(k in t for k in ["СУМО", "КУСЮ"]):
        return "Сумо"
    if any(k in t for k in ["РЕГБИ", "RUGBY"]):
        return "Регби"
    if any(k in t for k in ["КРИКЕТ", "T20", "ODI"]):
        return "Крикет"
    if any(k in t for k in ["БЕЙСБОЛ", "LVBP", "LIDOM", "MLB"]):
        return "Бейсбол"
    if any(k in t for k in ["ВОЛЕЙБОЛ", "CEV", "ТАУРОН"]):
        return "Волейбол"
    if any(k in t for k in ["ГАНДБОЛ", "ASOBAL"]):
        return "Гандбол"
    if any(k in t for k in ["ВОДНОЕ ПОЛО"]):
        return "Водное поло"
    if any(k in t for k in ["ФЛОРБОЛ"]):
        return "Флорбол"
    if any(k in t for k in ["БЕНДИ", "BANDY"]):
        return "Хоккей с мячом"
    if "ФУТБОЛ" in t:
        return "Футбол"

    # Приоритет 2: по тоталу
    if total is not None:
        if total >= 200: return "Баскетбол"
        if 170 <= total < 200: return "Волейбол"
        if 130 <= total < 170: return "Гандбол"
        if 50 <= total < 70: return "Хоккей"
        if 40 <= total < 50: return "Футбол"
        if 10 <= total < 20: return "Теннис"
        if 70 <= total < 130: return "Водное поло/Флорбол"
        if 4 <= total < 10: return "Футбол/Футзал"
    return "Прочее"

# ---------- ПАРСИНГ ----------
async def scrape_line():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx = await browser.new_context(
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"
        )
        page = await ctx.new_page()
        await page.goto("https://fon.bet/sports?mode=1&dateInterval=4", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        # Закрыть попапы
        for txt in ["Принять", "Согласен", "OK", "Понятно"]:
            try:
                b = page.get_by_text(txt, exact=True)
                if await b.count(): await b.first.click(timeout=500)
            except: pass

        # Прокрутка + разворачивание
        for _ in range(10):
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(800)
            # Кликнуть «Показать ещё»
            for t in ["Показать ещё", "Показать еще", "Развернуть"]:
                try:
                    loc = page.locator(f"text={t}")
                    for i in range(await loc.count()):
                        await loc.nth(i).click(timeout=500)
                except: pass

        # Собираем данные
        rows = []
        current_sport = "Прочее"
        current_tournament = "Неизвестный турнир"
        current_sub = "Основное"

        elements = await page.query_selector_all('div[class*="table-component-text--"], div[class*="sport-base-event--"], h2, h3')
        for el in elements:
            cls = await el.get_attribute("class") or ""
            txt = (await el.inner_text()).strip()

            # Заголовок вида спорта
            if cls == "" or "table-component-text--" not in cls:
                if txt in ["Футбол", "Хоккей", "Теннис", "Баскетбол", "Киберспорт", "Волейбол", "Гандбол", "MMA", "Бокс", "Дартс", "Снукер", "Шахматы", "Регби", "Крикет", "Бейсбол", "Флорбол", "Кёрлинг", "Сумо", "Водное поло", "Футзал", "Американский футбол"]:
                    current_sport = txt

            # Турнир
            if "table-component-text--" in cls and "sport-base-event" not in cls:
                if 5 < len(txt) < 80 and not any(w in txt for w in ["ФОРА", "ТОТАЛ", "ИСХОД"]):
                    current_tournament = txt
                    if current_sport == "Прочее":
                        current_sport = classify_sport(current_tournament, "")

            # Подсобытие
            if "event-block-subtitle--" in cls or any(w in txt for w in ["1-й период", "2-й период", "3-й период", "1-й тайм", "2-й тайм", "1-й сет", "2-й сет"]):
                current_sub = txt

            # Матч
            if "sport-base-event--" in cls:
                name_el = await el.query_selector('a[class*="sport-event__name--"]')
                match_name = (await name_el.inner_text()).strip() if name_el else "?"
                time_el = await el.query_selector('span[class*="event-block-planned-time__time--"]')
                time_text = (await time_el.inner_text()).strip() if time_el else "?"

                factors = await el.query_selector_all('div[class*="factor-value--"]')
                singles, complexes = [], []
                t_param = ""
                for f in factors:
                    fcls = await f.get_attribute("class") or ""
                    ftxt = (await f.inner_text()).strip()
                    if "single--" in fcls:
                        singles.append(ftxt)
                    elif "complex--" in fcls:
                        lines = [l.strip() for l in ftxt.split("\n") if l.strip()]
                        if len(lines) >= 2: complexes.append(lines)
                    elif "param--" in fcls:
                        t_param = ftxt

                p1, pX, p2 = "", "-", ""
                f1_p, f1_v, f2_p, f2_v = "", "", "", ""
                t_over, t_under = "", ""

                if len(singles) >= 3:
                    p1, pX, p2 = singles[0], singles[1], singles[2]
                elif len(singles) == 2:
                    p1, p2 = singles[0], singles[1]
                if len(complexes) >= 2:
                    f1_p, f1_v = complexes[0][0], complexes[0][1]
                    f2_p, f2_v = complexes[1][0], complexes[1][1]
                if len(singles) >= 5:
                    t_over, t_under = singles[3], singles[4]
                elif len(singles) >= 4:
                    t_over, t_under = singles[2], singles[3]

                if match_name != "?" and p1 and p1 != "-":
                    sport = classify_sport(current_tournament, t_param)
                    rows.append([sport, current_tournament, time_text, match_name, current_sub,
                                 p1, pX, p2, f1_p, f1_v, f2_p, f2_v, t_param, t_over, t_under])
                    current_sub = "Основное"

        await browser.close()
        return rows

# ---------- ФОРМИРОВАНИЕ CSV ----------
def make_csv(rows):
    buf = io.StringIO()
    buf.write("\uFEFF")  # BOM для Excel
    w = csv.writer(buf, delimiter="\t")
    w.writerow(["Вид_спорта", "Турнир", "Время", "Матч", "Тип", "П1", "X", "П2",
                "Фора 1", "Кэф Ф1", "Фора 2", "Кэф Ф2", "Тотал", "Больше", "Меньше"])
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Доступные команды:\n"
        "/line — спарсить линию по всем видам спорта\n"
        "/tennis — только теннис\n"
        "/help — справка"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def line_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, sport_filter=None):
    msg = await update.message.reply_text("⏳ Запускаю парсинг fon.bet... Это займёт 2-4 минуты.")
    try:
        rows = await scrape_line()
        if sport_filter:
            rows = [r for r in rows if r[0] == sport_filter]
        if not rows:
            await msg.edit_text("❌ Ничего не нашлось.")
            return
        csv_bytes = make_csv(rows)
        date = datetime.now().strftime("%Y-%m-%d_%H%M")
        filename = f"line_{sport_filter or 'all'}_{date}.csv"
        await msg.edit_text(f"✅ Готово! Собрано {len(rows)} матчей. Отправляю файл...")
        await update.message.reply_document(
            document=io.BytesIO(csv_bytes),
            filename=filename,
            caption=f"📊 {len(rows)} матчей | {datetime.now().strftime('%d.%m %H:%M')}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")

async def tennis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await line_cmd(update, context, sport_filter="Теннис")

# ---------- ЗАПУСК ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("line", line_cmd))
    app.add_handler(CommandHandler("tennis", tennis_cmd))
    print("🤖 Бот запущен")
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()