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
LINE_URL = os.environ.get("LINE_URL", "https://fon.bet/sports/table-tennis")
MSK = pytz.timezone("Europe/Moscow")
URL_RES = "https://fon.bet/results/table-tennis"
BTN_RES = "📊 Результаты за сегодня"
BTN_LINE = "📋 Линия ТТ"
STATE_FILE = Path("fon_state.json")
META_FILE = Path("bot_meta.json")
NIGHT_HOUR = int(os.environ.get("NIGHT_HOUR_MSK", "1"))
CAPTCHA_IDLE_MIN = 15
NIGHT_IDLE_HOURS = 8
SEL_RES = 'div[class*="results-event--"]'
SEL_LINE = 'div[class*="sport-base-event--"]'

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tt-bot")
lock = asyncio.Lock()
S = {"pw": None, "browser": None, "page": None, "waiting": False,
     "chat": None, "ts": None, "task": None, "night": False}
day_store = {}
KB = ReplyKeyboardMarkup([[BTN_RES], [BTN_LINE]], resize_keyboard=True)

def now(): return datetime.now(MSK)
def label(iso): return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y") if iso else now().strftime("%d.%m.%Y")
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

async def close_session(save=False):
    try:
        if save and S["page"]: await S["page"].context.storage_state(path=str(STATE_FILE))
    except Exception: pass
    try:
        if S["browser"]: await S["browser"].close()
        if S["pw"]: await S["pw"].stop()
    except Exception: pass
    S.update(pw=None, browser=None, page=None, waiting=False, chat=None, ts=None, task=None, night=False)

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

async def dismiss_banners(page):
    for txt in ("Принять", "Согласен", "Понятно", "OK"):
        try:
            b = page.get_by_text(txt, exact=True)
            if await b.count(): await b.first.click(timeout=700)
        except Exception: pass

COLLECT_RES = """() => {
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

COLLECT_LINE = """() => {
  const out = [];
  let cur = "Неизвестный турнир";
  document.querySelectorAll('div[class*="table-component-text--"], div[class*="sport-base-event--"]').forEach(el => {
    const cls = el.className;
    if (cls.includes('table-component-text--') && !cls.includes('sport-base-event')) {
      const txt = el.innerText.trim();
      if (txt.length > 5 && (txt.includes('.') || txt.includes('Лига') || txt.includes('Cup') || txt.includes('Москва') || txt.includes('Минск'))) {
        if (!txt.includes('ФОРА') && !txt.includes('ТОТАЛ') && !txt.includes('ИСХОД')) cur = txt.split('\\n')[0].trim();
      }
    }
    if (cls.includes('sport-base-event--')) {
      const nameEl = el.querySelector('a[class*="sport-event__name--"]');
      const match = nameEl ? nameEl.innerText.trim() : '';
      const timeEl = el.querySelector('span[class*="event-block-planned-time__time--"]');
      const time = timeEl ? timeEl.innerText.trim() : '?';
      let p1='',p2='',f1='',k1='',f2='',k2='',total='',over='',under='';
      const singles=[], complexes=[];
      el.querySelectorAll('div[class*="factor-value--"]').forEach(f => {
        const c = f.className;
        if (c.includes('table-component-factor-value_single--')) singles.push(f.innerText.trim());
        else if (c.includes('table-component-factor-value_complex--')) {
          const lines = f.innerText.split('\\n').map(l => l.trim()).filter(l => l);
          if (lines.length >= 2) complexes.push(lines);
        } else if (c.includes('table-component-factor-value_param--')) total = f.innerText.trim();
      });
      if (singles.length >= 2) { p1 = singles[0]; p2 = singles[1]; }
      if (complexes.length >= 2) { f1 = complexes[0][0]; k1 = complexes[0][1]; f2 = complexes[1][0]; k2 = complexes[1][1]; }
      if (singles.length >= 4) { over = singles[2]; under = singles[3]; }
      else if (singles.length === 3) over = singles[2];
      if (match) out.push({tournament: cur, time, match, p1, p2, f1, k1, f2, k2, total, over, under});
    }
  });
  return out;
}"""

async def scroll_collect(page, js):
    await page.evaluate("""() => {
        let best=document.documentElement,h=0;
        document.querySelectorAll('*').forEach(el=>{if(el.scrollHeight>el.clientHeight&&el.scrollHeight>h){h=el.scrollHeight;best=el;}});
        window.__sc=best; if(best===document.documentElement)window.scrollTo(0,0);else best.scrollTop=0;
    }""")
    found, last, stuck, steps = {}, -1, 0, 0
    while stuck < 5 and steps < 150:
        for r in await page.evaluate(js):
            key = (r.get("tournament", ""), r["time"], r.get("match") or (r.get("p1", "") + "|" + r.get("p2", "")))
            found[key] = r
        await page.evaluate("() => {const c=window.__sc; if(c===document.documentElement)window.scrollBy(0,700);else c.scrollTop+=700;}")
        await page.wait_for_timeout(700)
        cur = await page.evaluate("() => {const c=window.__sc; return c===document.documentElement?window.scrollY:c.scrollTop;}")
        stuck = stuck + 1 if cur == last else 0
        last = cur; steps += 1
    return list(found.values())

def make_results_csv(raw, date_str, drop_yesterday):
    store = day_store.setdefault(date_str, {})
    for r in raw:
        if drop_yesterday and "Вчера" in r["time"]: continue
        m = re.search(r"\d{1,2}:\d{2}", r["time"])
        tm = m.group(0) if m else "?"
        sets, s1, s2, p1, p2 = [], 0, 0, 0, 0
        for s in r["sets"]:
            mm = re.match(r"^(\d+)\s*-\s*(\d+)$", s.strip())
            if not mm: continue
            a, b = int(mm.group(1)), int(mm.group(2))
            sets.append(f"{a}-{b}"); p1 += a; p2 += b; s1 += a > b; s2 += b > a
        total = (r["total"] or "").strip()
        if not sets or total in ("", "0:0"): continue
        if ":" in total:
            try: s1, s2 = map(int, total.split(":"))
            except Exception: pass
        else: total = f"{s1}:{s2}"
        store[(tm, r["p1"], r["p2"])] = [date_str, tm, r["tournament"], r["p1"], r["p2"],
            total, " ".join(sets), s1, s2, p1, p2, p1 + p2, p1 - p2, len(sets)]
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

def make_line_csv(raw):
    seen = {}
    for r in raw:
        key = (r["tournament"], r["time"], r["match"])
        if key not in seen:
            seen[key] = [r["tournament"], r["time"], r["match"], r["p1"], r["p2"],
                         r["f1"], r["k1"], r["f2"], r["k2"], r["total"], r["over"], r["under"]]
    data = list(seen.values())
    buf = io.StringIO(); buf.write("\ufeff")
    w = csv.writer(buf, delimiter="\t")
    w.writerow(["Турнир","Время","Матч","П1","П2","Фора 1","Кэф Ф1","Фора 2","Кэф Ф2","Тотал","Больше","Меньше"])
    w.writerows(data)
    return len(data), buf.getvalue().encode("utf-8")

def task_urls(task):
    if task[0] == "results":
        return [URL_RES + (f"?date={task[1]}" if task[1] else "")]
    urls = [LINE_URL]
    for u in ("https://fon.bet/sports/table-tennis", "https://fon.bet/line/table-tennis"):
        if u not in urls: urls.append(u)
    return urls

async def open_page(task):
    page = S["page"]
    sel = SEL_LINE if task[0] == "line" else SEL_RES
    for u in task_urls(task):
        try:
            await page.goto(u, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            await dismiss_banners(page)
            if await captcha_field(page): return "captcha"
            await page.wait_for_selector(sel, timeout=15000)
            return "ok"
        except Exception:
            continue
    if task[0] == "line":
        try:
            await page.goto("https://fon.bet/sports", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            await dismiss_banners(page)
            if await captcha_field(page): return "captcha"
            await page.get_by_text("Настольный теннис").first.click(timeout=5000)
            await page.wait_for_selector(SEL_LINE, timeout=20000)
            return "ok"
        except Exception:
            return "fail"
    return "fail"

async def complete_task(bot, chat_id):
    task, night, page = S["task"], S["night"], S["page"]
    if task is None: task = ("results", None)
    sel = SEL_LINE if task[0] == "line" else SEL_RES
    if not await page.locator(sel).count():
        st = await open_page(task)
        if st == "captcha":
            S["ts"] = now()
            await bot.send_photo(chat_id, await page.screenshot(), caption="🔐 Снова капча. Пришли ответ текстом:")
            return
        if st == "fail":
            await bot.send_photo(chat_id, await page.screenshot(), caption="❌ Страница не прогрузилась.")
            await close_session(False); return
    if task[0] == "line":
        raw = await scroll_collect(page, COLLECT_LINE)
        if not raw:
            await bot.send_message(chat_id, "❌ Линия пуста (строк нет).")
            await close_session(False); return
        n, payload = make_line_csv(raw)
        fname = f"Линия_ТТ_{now().strftime('%Y-%m-%d_%H%M')}.csv"
        cap = f"📋 Линия настольный теннис: {n} матчей (на {now().strftime('%H:%M')} МСК)"
    else:
        raw = await scroll_collect(page, COLLECT_RES)
        iso = task[1]; date_str = label(iso)
        n, payload = make_results_csv(raw, date_str, iso is None)
        if not n:
            await bot.send_message(chat_id, f"❌ За {date_str} завершённых матчей нет.")
            await close_session(False); return
        fname = f"Результаты_ТТ_{date_str.replace('.', '-')}_{now().strftime('%H%M')}.csv"
        cap = f"{'🌙 Автовыгрузка' if night else '✅'} за {date_str}: {n} матчей"
        if night: cap += " (за вчера не было запросов)"
        else:
            META["last_manual"] = date_str; save_meta()
    await close_session(True)
    await bot.send_document(chat_id, document=io.BytesIO(payload), filename=fname, caption=cap)

async def run_task(bot, chat_id, task, night=False):
    async with lock:
        if idle_expired(): await close_session(False)
        if S["waiting"]:
            await bot.send_message(chat_id, "🔐 Жду ответ на капчу выше."); return
        await ensure_browser()
        st = await open_page(task)
        if st == "captcha":
            S.update(waiting=True, chat=chat_id, ts=now(), task=task, night=night)
            await bot.send_photo(chat_id, await S["page"].screenshot(),
                caption="🔐 Фонбет просит капчу. Пришли ответ текстом — я продолжу.")
            return
        if st == "fail":
            shot = await S["page"].screenshot()
            await close_session(False)
            await bot.send_photo(chat_id, shot, caption="❌ Страница не прогрузилась (блок IP / капча / другой URL линии).")
            return
        S.update(task=task, night=night, chat=chat_id)   # запомнили задачу ДО сбора (фикс бага)
        await complete_task(bot, chat_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    META["chat_id"] = update.effective_chat.id; save_meta()
    await update.message.reply_text(
        "🏓 Настольный теннис (fon.bet)\n\n"
        f"{BTN_RES} — результаты за сегодня (/results)\n"
        f"{BTN_LINE} — линия: кэфы, форы, тоталы (/line)\n"
        "/prev — результаты за вчера\n"
        f"🌙 В {NIGHT_HOUR:02d}:00 МСК: если за вчера не запрашивал результаты — сам пришлю вчерашний файл\n"
        "🔐 Капча — картинка в чат, ответь текстом\n"
        "/reset — сброс сессии и cookies", reply_markup=KB)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        await close_session(False)
        try: STATE_FILE.unlink()
        except Exception: pass
    await update.message.reply_text("🔄 Сессия сброшена.")

async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    META["chat_id"] = chat; save_meta()
    msg = await update.message.reply_text("⏳ Собираю результаты за сегодня (~1–2 мин)…")
    try:
        await run_task(context.bot, chat, ("results", None))
        await msg.delete()
    except Exception as e:
        log.exception("results")
        await msg.edit_text(f"❌ {str(e)[:250]}")
        await close_session(False)

async def prev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    META["chat_id"] = chat; save_meta()
    iso = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    msg = await update.message.reply_text(f"⏳ Собираю результаты за {label(iso)}…")
    try:
        await run_task(context.bot, chat, ("results", iso))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:250]}")
        await close_session(False)

async def line_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    META["chat_id"] = chat; save_meta()
    msg = await update.message.reply_text("⏳ Собираю линию настольного тенниса (~1–2 мин)…")
    try:
        await run_task(context.bot, chat, ("line", None))
        await msg.delete()
    except Exception as e:
        log.exception("line")
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
            await complete_task(context.bot, chat)
            await msg.delete()
        except Exception as e:
            log.exception("captcha")
            await update.message.reply_text(f"❌ {str(e)[:250]}")
            await close_session(False)

async def night_job(context: ContextTypes.DEFAULT_TYPE):
    chat = META.get("chat_id")
    if not chat:
        log.warning("night: нет chat_id (нажми /start)"); return
    prev = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if META.get("last_manual") == label(prev):
        log.info("night: пропуск — за %s уже были запросы", label(prev)); return
    try:
        await run_task(context.bot, chat, ("results", prev), night=True)
    except Exception:
        log.exception("night job")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler(["results", "tt"], results))
    app.add_handler(CommandHandler("prev", prev_cmd))
    app.add_handler(CommandHandler(["line", "line_tt"], line_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_RES]), results))
    app.add_handler(MessageHandler(filters.Text([BTN_LINE]), line_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_answer))
    app.job_queue.run_daily(night_job, time=dtime(hour=NIGHT_HOUR, minute=0, tzinfo=MSK))
    log.info("Бот запущен: результаты + линия, ночь в %02d:00 МСК", NIGHT_HOUR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
