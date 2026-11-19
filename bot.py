import asyncio, csv, io, json, logging, os, re
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import urlsplit

import pytz
from playwright.async_api import async_playwright
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY_URL = os.environ.get("PROXY_URL")          # опционально: http://user:pass@host:port
MSK = pytz.timezone("Europe/Moscow")
RUN_HOURS = [int(h) for h in os.environ.get("RUN_HOURS_MSK", "7,15,23").split(",")]
URL = "https://fon.bet/results/table-tennis"
BTN = "📊 Результаты за сегодня"
CHATS_FILE = Path("chats.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tt-bot")
scrape_lock = asyncio.Lock()
day_store = {}   # дата -> {(время, игрок1, игрок2): строка}  (накопление за сутки)

def load_chats():
    try: return set(json.loads(CHATS_FILE.read_text()))
    except Exception: return set()
def save_chats(s):
    try: CHATS_FILE.write_text(json.dumps(sorted(s)))
    except Exception: pass
chats = load_chats()
if os.environ.get("CHAT_ID"): chats.add(int(os.environ["CHAT_ID"]))

def proxy_cfg():
    if not PROXY_URL: return None
    u = urlsplit(PROXY_URL)
    cfg = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username: cfg["username"], cfg["password"] = u.username, u.password or ""
    return cfg

# Сбор строк со страницы — селекторы из твоего браузерного скрипта
COLLECT_JS = """() => {
  const out = [];
  document.querySelectorAll('div[class*="results-event--"]').forEach(row => {
    let tournament = "Неизвестный турнир";
    let parent = row.parentElement;
    while (parent && parent !== document.body) {
      const t = parent.querySelector('[class*="tournament"], [class*="header"], [class*="competition"]');
      if (t && t.innerText.trim()) { tournament = t.innerText.split('\\n')[0].trim(); break; }
      parent = parent.parentElement;
    }
    const timeEl = row.querySelector('span[class*="results-event-time__description--"]');
    const time = timeEl ? timeEl.innerText.trim() : '';
    const pl = row.querySelectorAll('div[class*="results-event-team__caption--"]');
    const p1 = pl[0] ? pl[0].innerText.trim() : '';
    const p2 = pl[1] ? pl[1].innerText.trim() : '';
    let scores = Array.from(row.querySelectorAll('div[class*="results-scoreBlock__score--"]')).map(e => e.innerText.trim());
    const sum = row.querySelectorAll('div[class*="_summary--"]');
    let total = '';
    if (sum.length === 2) { total = sum[0].innerText.trim() + ':' + sum[1].innerText.trim(); scores = scores.slice(0, -2); }
    const sets = [];
    for (let j = 0; j + 1 < scores.length; j += 2) sets.push(scores[j] + '-' + scores[j+1]);
    if (p1 && p2) out.push({time, tournament, p1, p2, sets, total});
  });
  return out;
}"""

async def scrape_results():
    """Возвращает (raw_rows, screenshot|None, note)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy_cfg(),
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        page = await ctx.new_page()
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            for txt in ("Принять", "Согласен", "Понятно", "OK"):
                try:
                    b = page.get_by_text(txt, exact=True)
                    if await b.count(): await b.first.click(timeout=700)
                except Exception: pass
            try:
                tab = page.get_by_text("Завершенные", exact=True)
                if await tab.count(): await tab.first.click(timeout=1500)
            except Exception: pass
            try:
                await page.wait_for_selector('div[class*="results-event--"]', timeout=30000)
            except Exception:
                return [], await page.screenshot(), "Матчи не появились: страница пустая/капча/блок IP."
            await page.wait_for_timeout(1500)
            await page.evaluate("""() => {
                let best=document.documentElement,h=0;
                document.querySelectorAll('*').forEach(el=>{if(el.scrollHeight>el.clientHeight&&el.scrollHeight>h){h=el.scrollHeight;best=el;}});
                window.__sc=best; if(best===document.documentElement)window.scrollTo(0,0);else best.scrollTop=0;
            }""")
            found, last, stuck, steps = {}, -1, 0, 0
            while stuck < 5 and steps < 150:
                for r in await page.evaluate(COLLECT_JS):
                    found[(r["time"], r["p1"], r["p2"])] = r
                await page.evaluate("() => {const c=window.__sc; if(c===document.documentElement)window.scrollBy(0,700);else c.scrollTop+=700;}")
                await page.wait_for_timeout(700)
                cur = await page.evaluate("() => {const c=window.__sc; return c===document.documentElement?window.scrollY:c.scrollTop;}")
                stuck = stuck + 1 if cur == last else 0
                last = cur; steps += 1
            if not found:
                return [], await page.screenshot(), "Страница открылась, но строк матчей нет."
            return list(found.values()), None, ""
        finally:
            await browser.close()

def merge_and_csv(raw):
    date_str = datetime.now(MSK).strftime("%d.%m.%Y")
    store = day_store.setdefault(date_str, {})
    for d in list(day_store):
        if d != date_str: del day_store[d]
    for r in raw:
        if "Вчера" in r["time"]: continue          # только текущие сутки
        m = re.search(r"\d{1,2}:\d{2}", r["time"])
        tm = m.group(0) if m else "?"
        s1 = s2 = pts1 = pts2 = 0
        for s in r["sets"]:
            mm = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
            if not mm: continue
            a, b = int(mm.group(1)), int(mm.group(2))
            pts1 += a; pts2 += b
            s1 += a > b; s2 += b > a
        total = r["total"] or f"{s1}:{s2}"
        if ":" in total:
            try: s1, s2 = map(int, total.split(":"))
            except Exception: pass
        store[(tm, r["p1"], r["p2"])] = [date_str, tm, r["tournament"], r["p1"], r["p2"],
            total, " ".join(r["sets"]), s1, s2, pts1, pts2, pts1 + pts2, pts1 - pts2, len(r["sets"])]
    data = sorted(store.values(), key=lambda r: (r[1], r[2], r[3]))
    buf = io.StringIO(); buf.write("\ufeff")
    w = csv.writer(buf, delimiter="\t")
    w.writerow(["Дата","Время","Турнир","Игрок 1","Игрок 2","Итог","Сеты","Сеты_1","Сеты_2",
                "Очки_1","Очки_2","Тотал_очков","Разница_очков","Кол_во_сетов"])
    w.writerows(data)
    return date_str, len(data), buf.getvalue().encode("utf-8")

async def collect():
    async with scrape_lock:
        raw, shot, note = await scrape_results()
    if not raw: return None, shot, note
    date_str, n, payload = merge_and_csv(raw)
    now = datetime.now(MSK)
    fname = f"Результаты_ТТ_{date_str.replace('.', '-')}_{now.strftime('%H%M')}.csv"
    cap = f"✅ Настольный теннис за {date_str}: {n} матчей (на {now.strftime('%H:%M')} МСК)"
    return (payload, fname, cap), None, ""

async def send_to(bot, chat_id, result, shot, note):
    if result:
        payload, fname, cap = result
        await bot.send_document(chat_id, document=io.BytesIO(payload), filename=fname, caption=cap)
    else:
        await bot.send_photo(chat_id, photo=shot,
            caption=f"⚠️ {note}\nЕсли на скрине капча/пусто — фонбет режет IP сервера. Добавь PROXY_URL в Variables.")

# ---------- команды ----------
KB = ReplyKeyboardMarkup([[BTN]], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats.add(update.effective_chat.id); save_chats(chats)
    await update.message.reply_text(
        "🏓 Результаты настольного тенниса (fon.bet)\n\n"
        f"⏰ Автовыгрузка за сутки: {', '.join(f'{h:02d}:00' for h in RUN_HOURS)} МСК\n"
        "📊 Кнопка внизу или /results — собрать сейчас\n"
        "/diag — скриншот страницы (диагностика)\n"
        "/stop — отключить авторассылку", reply_markup=KB)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats.discard(update.effective_chat.id); save_chats(chats)
    await update.message.reply_text("Авторассылка отключена. /start — включить снова.")

async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats.add(update.effective_chat.id); save_chats(chats)
    if scrape_lock.locked():
        await update.message.reply_text("⏳ Уже собираю, подожди 1–2 минуты."); return
    msg = await update.message.reply_text("⏳ Собираю результаты за сегодня (~1–2 мин)…")
    try:
        result, shot, note = await collect()
        await send_to(context.bot, update.effective_chat.id, result, shot, note)
        await msg.delete()
    except Exception as e:
        log.exception("scrape failed")
        await msg.edit_text(f"❌ Ошибка: {str(e)[:300]}")

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 Проверяю доступ к fon.bet…")
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, proxy=proxy_cfg(),
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            pg = await (await b.new_context(locale="ru-RU", timezone_id="Europe/Moscow")).new_page()
            await pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(8000)
            cnt = await pg.evaluate('document.querySelectorAll("div[class*=results-event--]").length')
            shot = await pg.screenshot()
            await b.close()
        await update.message.reply_photo(shot, caption=f"Строк матчей в DOM: {cnt}")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:300]}")

async def scheduled(context: ContextTypes.DEFAULT_TYPE):
    if not chats: log.warning("Нет подписчиков рассылки"); return
    try:
        result, shot, note = await collect()
        for cid in list(chats):
            try: await send_to(context.bot, cid, result, shot, note)
            except Exception: log.exception("send to %s failed", cid)
    except Exception: log.exception("scheduled scrape failed")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler(["results", "tt", "today"], results))
    app.add_handler(CommandHandler("diag", diag))
    app.add_handler(MessageHandler(filters.Text([BTN]), results))
    for h in RUN_HOURS:
        app.job_queue.run_daily(scheduled, time=dtime(hour=h, minute=0, tzinfo=MSK))
    log.info("Бот запущен. Расписание: %s МСК", RUN_HOURS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
