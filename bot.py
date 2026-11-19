import asyncio, csv, io, logging, os, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY_URL = os.environ.get("PROXY_URL")          # опционально
MSK = timezone(timedelta(hours=3))
URL = "https://fon.bet/results/table-tennis"
BTN = "📊 Результаты за сегодня"
STATE_FILE = Path("fon_state.json")              # cookies после капчи
CAPTCHA_IDLE_MIN = 15                            # через сколько мин забыть незавершённую капчу

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tt-bot")

S = {"pw": None, "browser": None, "page": None, "waiting": False, "chat": None, "ts": None}
lock = asyncio.Lock()
day_store = {}                                   # накопление матчей за сутки
KB = ReplyKeyboardMarkup([[BTN]], resize_keyboard=True)

def proxy_cfg():
    if not PROXY_URL: return None
    u = urlsplit(PROXY_URL)
    cfg = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username: cfg["username"], cfg["password"] = u.username, u.password or ""
    return cfg

async def close_session(save=False):
    try:
        if save and S["page"]:
            await S["page"].context.storage_state(path=str(STATE_FILE))
    except Exception: pass
    try:
        if S["browser"]: await S["browser"].close()
        if S["pw"]: await S["pw"].stop()
    except Exception: pass
    S.update(pw=None, browser=None, page=None, waiting=False, chat=None, ts=None)

async def ensure_page():
    if S["page"]: return
    S["pw"] = await async_playwright().start()
    S["browser"] = await S["pw"].chromium.launch(headless=True, proxy=proxy_cfg(),
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
    ctx = await S["browser"].new_context(locale="ru-RU", timezone_id="Europe/Moscow",
        viewport={"width": 1440, "height": 900},
        storage_state=str(STATE_FILE) if STATE_FILE.exists() else None,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    S["page"] = await ctx.new_page()
    await S["page"].goto(URL, wait_until="domcontentloaded", timeout=60000)
    await S["page"].wait_for_timeout(4000)
    for txt in ("Принять", "Согласен", "Понятно", "OK"):
        try:
            b = S["page"].get_by_text(txt, exact=True)
            if await b.count(): await b.first.click(timeout=700)
        except Exception: pass

async def captcha_field(page):
    loc = page.locator('input[placeholder*="цифры"]')
    if await loc.count() and await loc.first.is_visible(): return loc.first
    return None

async def send_captcha(bot, chat_id, note=""):
    shot = await S["page"].screenshot()
    S.update(waiting=True, chat=chat_id, ts=datetime.now(MSK))
    await bot.send_photo(chat_id, shot, caption=(note + "🔐 Фонбет просит капчу.\nПришли ответ ОТДЕЛЬНЫМ сообщением (цифры/буквы как на картинке).\n/reset — начать заново."))

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

async def do_scrape(page):
    try:
        tab = page.get_by_text("Завершенные", exact=True)
        if await tab.count(): await tab.first.click(timeout=1500)
    except Exception: pass
    await page.wait_for_selector('div[class*="results-event--"]', timeout=25000)
    await page.wait_for_timeout(1200)
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
    return list(found.values())

def merge_and_csv(raw):
    date_str = datetime.now(MSK).strftime("%d.%m.%Y")
    store = day_store.setdefault(date_str, {})
    for d in list(day_store):
        if d != date_str: del day_store[d]
    for r in raw:
        if "Вчера" in r["time"]: continue
        m = re.search(r"\d{1,2}:\d{2}", r["time"])
        tm = m.group(0) if m else "?"
        s1 = s2 = pts1 = pts2 = 0
        for s in r["sets"]:
            mm = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
            if not mm: continue
            a, b = int(mm.group(1)), int(mm.group(2))
            pts1 += a; pts2 += b; s1 += a > b; s2 += b > a
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

async def finish_scrape(bot, chat_id):
    """Капчи нет (или прошла) — собираем и отправляем CSV."""
    raw = await do_scrape(S["page"])
    date_str, n, payload = merge_and_csv(raw)
    now = datetime.now(MSK)
    fname = f"Результаты_ТТ_{date_str.replace('.', '-')}_{now.strftime('%H%M')}.csv"
    await bot.send_document(chat_id, document=io.BytesIO(payload), filename=fname,
        caption=f"✅ Настольный теннис за {date_str}: {n} матчей (на {now.strftime('%H:%M')} МСК)")
    await close_session(save=True)               # cookies сохраняем, браузер закрываем (экономим память)

def idle_expired():
    return S["waiting"] and S["ts"] and datetime.now(MSK) - S["ts"] > timedelta(minutes=CAPTCHA_IDLE_MIN)

# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏓 Результаты настольного тенниса (fon.bet), только по кнопке.\n\n"
        "📊 Кнопка внизу или /results — собрать за сегодня\n"
        "🔐 Если сайт попросит капчу — пришлю картинку, ответь текстом\n"
        "/reset — сбросить сессию и cookies", reply_markup=KB)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        await close_session(save=False)
        try: STATE_FILE.unlink()
        except Exception: pass
    await update.message.reply_text("🔄 Сессия сброшена. Жми кнопку — начнём заново.")

async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with lock:
        if idle_expired(): await close_session(save=False)
        if S["waiting"]:
            if S["chat"] == chat_id:
                await update.message.reply_text("🔐 Жду ответ на капчу выше. Пришли цифры/буквы текстом.")
            else:
                await update.message.reply_text("⏳ Сейчас другой чат проходит капчу, подожди.")
            return
        msg = await update.message.reply_text("⏳ Открываю fon.bet…")
        try:
            await ensure_page()
            if await captcha_field(S["page"]):
                await send_captcha(context.bot, chat_id)
                await msg.delete(); return
            await finish_scrape(context.bot, chat_id)
            await msg.delete()
        except Exception as e:
            log.exception("results failed")
            try: await update.message.reply_photo(await S["page"].screenshot(), caption=f"❌ {str(e)[:250]}")
            except Exception: await update.message.reply_text(f"❌ {str(e)[:250]}")
            await close_session(save=False)

async def captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not S["waiting"] or S["chat"] != chat_id or not text: return
    async with lock:
        if idle_expired():
            await close_session(save=False)
            await update.message.reply_text("⌛ Капча устарела. Жми кнопку заново."); return
        msg = await update.message.reply_text("⏳ Ввожу ответ…")
        try:
            field = await captcha_field(S["page"])
            if field:
                await field.fill(text)
                await S["page"].get_by_text("ПРОВЕРИТЬ", exact=True).first.click(timeout=3000)
                await S["page"].wait_for_timeout(4000)
            if await captcha_field(S["page"]):   # не прошла — обновляем картинку
                try:
                    await S["page"].get_by_text("ОБНОВИТЬ", exact=True).first.click(timeout=2000)
                    await S["page"].wait_for_timeout(1500)
                except Exception: pass
                await send_captcha(context.bot, chat_id, "❌ Не приняло. Новая капча:\n")
                await msg.delete(); return
            await finish_scrape(context.bot, chat_id)
            await msg.delete()
        except Exception as e:
            log.exception("captcha failed")
            await update.message.reply_text(f"❌ {str(e)[:250]}")
            await close_session(save=False)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler(["results", "tt"], results))
    app.add_handler(MessageHandler(filters.Text([BTN]), results))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_answer))
    log.info("Бот запущен (без расписания, капча через Telegram)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
