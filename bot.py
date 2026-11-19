import asyncio, csv, io, json, logging, os, re
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from urllib.parse import urlsplit

import pytz
from playwright.async_api import async_playwright
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY_URL = os.environ.get("PROXY_URL")
MSK = pytz.timezone("Europe/Moscow")
URL = "https://fon.bet/results/table-tennis"
BTN = "📊 Результаты за сегодня"
STATE_FILE = Path("fon_state.json")   # cookies после капчи
META_FILE = Path("bot_meta.json")     # chat_id + дата последнего ручного запроса
NIGHT_HOUR = int(os.environ.get("NIGHT_HOUR_MSK", "1"))
CAPTCHA_IDLE_MIN = 15                 # ожидание ответа на капчу днём
NIGHT_IDLE_HOURS = 8                  # ...и ночью (ответишь утром)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tt-bot")
lock = asyncio.Lock()
S = {"pw": None, "browser": None, "page": None, "waiting": False,
     "chat": None, "target": None, "night": False, "ts": None}
day_store = {}

def now(): return datetime.now(MSK)
def load_meta():
    try: return json.loads(META_FILE.read_text())
    except Exception: return {}
META = load_meta()
def save_meta():
    try: META_FILE.write_text(json.dumps(META))
    except Exception: pass

def proxy_cfg():
    if not PROXY_URL: return None
    u = urlsplit(PROXY_URL)
    cfg = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username: cfg["username"], cfg["password"] = u.username, u.password or ""
    return cfg

def url_for(iso): return URL + (f"?date={iso}" if iso else "")
def label(iso): return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y") if iso else now().strftime("%d.%m.%Y")

async def close_session(save=False):
    try:
        if save and S["page"]: await S["page"].context.storage_state(path=str(STATE_FILE))
    except Exception: pass
    try:
        if S["browser"]: await S["browser"].close()
        if S["pw"]: await S["pw"].stop()
    except Exception: pass
    S.update(pw=None, browser=None, page=None, waiting=False, chat=None, target=None, night=False, ts=None)

def idle_expired():
    if not S["waiting"] or not S["ts"]: return False
    lim = timedelta(hours=NIGHT_IDLE_HOURS) if S["night"] else timedelta(minutes=CAPTCHA_IDLE_MIN)
    return now() - S["ts"] > lim

async def ensure_browser():
    if S["page"]: return
    S["pw"] = await async_playwright().start()
    S["browser"] = await S["pw"].chromium.launch(headless=True, proxy=proxy_cfg(),
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
    ctx = await S["browser"].new_context(locale="ru-RU", timezone_id="Europe/Moscow",
        viewport={"width": 1440, "height": 900},
        storage_state=str(STATE_FILE) if STATE_FILE.exists() else None,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    S["page"] = await ctx.new_page()

async def captcha_field(page):
    loc = page.locator('input[placeholder*="цифры"]')
    if await loc.count() and await loc.first.is_visible(): return loc.first
    return None

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

async def harvest(page):
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

def build_rows(raw, date_str, drop_yesterday):
    out = []
    for r in raw:
        t = r["time"]
        if drop_yesterday and "Вчера" in t: continue
        m = re.search(r"\d{1,2}:\d{2}", t)
        tm = m.group(0) if m else "?"
        sets, s1, s2, p1, p2 = [], 0, 0, 0, 0
        for s in r["sets"]:                      # только числовые сеты, без '---'
            mm = re.match(r"^(\d+)\s*-\s*(\d+)$", s.strip())
            if not mm: continue
            a, b = int(mm.group(1)), int(mm.group(2))
            sets.append(f"{a}-{b}"); p1 += a; p2 += b; s1 += a > b; s2 += b > a
        total = (r["total"] or "").strip()
        if not sets or total in ("", "0:0"): continue   # мусор/не сыграно — пропускаем
        if ":" in total:
            try: s1, s2 = map(int, total.split(":"))
            except Exception: pass
        else: total = f"{s1}:{s2}"
        out.append([date_str, tm, r["tournament"], r["p1"], r["p2"], total, " ".join(sets),
                    s1, s2, p1, p2, p1 + p2, p1 - p2, len(sets)])
    return out

def make_csv(raw, date_str, drop_yesterday):
    store = day_store.setdefault(date_str, {})
    for r in build_rows(raw, date_str, drop_yesterday):
        store[(r[1], r[3], r[4])] = r
    today_lbl = now().strftime("%d.%m.%Y")
    for d in list(day_store):
        if d not in (date_str, today_lbl): del day_store[d]
    data = sorted(store.values(), key=lambda r: (r[1], r[2], r[3]))
    buf = io.StringIO(); buf.write("\ufeff")
    w = csv.writer(buf, delimiter="\t")
    w.writerow(["Дата","Время","Турнир","Игрок 1","Игрок 2","Итог","Сеты","Сеты_1","Сеты_2",
                "Очки_1","Очки_2","Тотал_очков","Разница_очков","Кол_во_сетов"])
    w.writerows(data)
    return len(data), buf.getvalue().encode("utf-8")

async def run_collect(chat_id, iso=None, night=False):
    """iso=None → сегодня; iso='ГГГГ-ММ-ДД' → конкретные сутки."""
    async with lock:
        if idle_expired(): await close_session(False)
        if S["waiting"]: return None, None, "⏳ Уже жду ответ на капчу."
        await ensure_browser()
        page = S["page"]
        await page.goto(url_for(iso), wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)
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
            await page.wait_for_selector('div[class*="results-event--"]', timeout=25000)
        except Exception:
            shot = await page.screenshot()
            S.update(waiting=True, chat=chat_id, target=iso, night=night, ts=now())
            return None, shot, "🔐 Фонбет просит капчу. Пришли ответ текстом — я продолжу."
        raw = await harvest(page)
        if not raw:
            shot = await page.screenshot()
            await close_session(False)
            return None, shot, "Страница открылась, но матчей нет."
        n, payload = make_csv(raw, label(iso), iso is None)
        await close_session(True)
        fname = f"Результаты_ТТ_{label(iso).replace('.', '-')}_{now().strftime('%H%M')}.csv"
        cap = f"{'🌙 Автовыгрузка' if night else '✅'} за {label(iso)}: {n} матчей"
        return (payload, fname, cap), None, ""

async def send_res(bot, chat_id, res, shot, note):
    if res:
        payload, fname, cap = res
        await bot.send_document(chat_id, document=io.BytesIO(payload), filename=fname, caption=cap)
    elif shot:
        await bot.send_photo(chat_id, shot, caption=note)
    else:
        await bot.send_message(chat_id, note)

# ---------- команды ----------
KB = ReplyKeyboardMarkup([[BTN]], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    META["chat_id"] = update.effective_chat.id; save_meta()
    await update.message.reply_text(
        "🏓 Результаты настольного тенниса (fon.bet)\n\n"
        "📊 Кнопка / /results — сегодня\n"
        "/prev — предыдущие сутки вручную\n"
        f"🌙 В {NIGHT_HOUR:02d}:00 МСК: если за вчера не было запросов — сам пришлю вчерашний файл\n"
        "🔐 Капча — картинка в чат, ответь текстом\n"
        "/reset — сброс сессии", reply_markup=KB)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        await close_session(False)
        try: STATE_FILE.unlink()
        except Exception: pass
    await update.message.reply_text("🔄 Сессия сброшена.")

async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    META["chat_id"] = chat; save_meta()
    if S["waiting"]:
        await update.message.reply_text("🔐 Жду ответ на капчу выше."); return
    msg = await update.message.reply_text("⏳ Собираю сегодня (~1–2 мин)…")
    try:
        res, shot, note = await run_collect(chat)
        if res: META["last_manual"] = now().strftime("%d.%m.%Y"); save_meta()
        await send_res(context.bot, chat, res, shot, note)
        await msg.delete()
    except Exception as e:
        log.exception("results")
        await msg.edit_text(f"❌ {str(e)[:250]}")
        await close_session(False)

async def prev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    META["chat_id"] = chat; save_meta()
    iso = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    msg = await update.message.reply_text(f"⏳ Собираю {label(iso)}…")
    try:
        res, shot, note = await run_collect(chat, iso)
        if res: META["last_manual"] = label(iso); save_meta()
        await send_res(context.bot, chat, res, shot, note)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:250]}")
        await close_session(False)

async def captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not S["waiting"] or S["chat"] != chat or not text: return
    async with lock:
        if idle_expired():
            await close_session(False)
            await update.message.reply_text("⌛ Капча устарела. Жми кнопку заново."); return
        msg = await update.message.reply_text("⏳ Ввожу ответ…")
        try:
            page = S["page"]
            f = await captcha_field(page)
            if f:
                await f.fill(text)
                await page.get_by_text("ПРОВЕРИТЬ", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(4000)
            if await captcha_field(page):
                try:
                    await page.get_by_text("ОБНОВИТЬ", exact=True).first.click(timeout=2000)
                    await page.wait_for_timeout(1500)
                except Exception: pass
                S["ts"] = now()
                await update.message.reply_photo(await page.screenshot(), caption="❌ Не приняло. Новая капча:")
                await msg.delete(); return
            if not await page.locator('div[class*="results-event--"]').count():
                await page.goto(url_for(S["target"]), wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector('div[class*="results-event--"]', timeout=25000)
            raw = await harvest(page)
            n, payload = make_csv(raw, label(S["target"]), S["target"] is None)
            night = S["night"]
            await close_session(True)
            fname = f"Результаты_ТТ_{label(S['target'] or now().strftime('%Y-%m-%d')).replace('.', '-')}_{now().strftime('%H%M')}.csv"
            await context.bot.send_document(chat, document=io.BytesIO(payload), filename=fname,
                caption=f"{'🌙 Автовыгрузка' if night else '✅'} за {label(S['target'])}: {n} матчей")
            await msg.delete()
        except Exception as e:
            log.exception("captcha")
            await update.message.reply_text(f"❌ {str(e)[:250]}")
            await close_session(False)

async def night_job(context: ContextTypes.DEFAULT_TYPE):
    chat = META.get("chat_id")
    if not chat:
        log.warning("night: нет chat_id (нажми /start)"); return
    prev_iso = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if META.get("last_manual") == label(prev_iso):
        log.info("night: пропуск — за %s уже были запросы", label(prev_iso)); return
    try:
        res, shot, note = await run_collect(chat, prev_iso, night=True)
        if res:
            res = (res[0], res[1], res[2] + " (за вчера не было запросов)")
        await send_res(context.bot, chat, res, shot, "🌙 Ночной сбор: " + (note or ""))
    except Exception:
        log.exception("night job")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler(["results", "tt"], results))
    app.add_handler(CommandHandler("prev", prev_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN]), results))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_answer))
    app.job_queue.run_daily(night_job, time=dtime(hour=NIGHT_HOUR, minute=0, tzinfo=MSK))
    log.info("Бот запущен. Ночная выгрузка в %02d:00 МСК при отсутствии запросов", NIGHT_HOUR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
