import os, re, json, html, time, calendar
from datetime import datetime, timezone, timedelta

import feedparser
import requests

# Обычные крипто-ленты
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cointelegraph.com/rss/tag/solana",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
    "https://news.google.com/rss/search?q=solana+crypto&hl=en-US&gl=US&ceid=US:en",
]

# Государственные источники: берём только новости с упоминанием крипты
GOV_FEEDS = [
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.justice.gov/news/rss?m=1",
    "https://news.google.com/rss/search?q=government+cryptocurrency+regulation&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=central+bank+cryptocurrency&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=SEC+OR+CFTC+OR+Treasury+crypto&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=правительство+криптовалюты+закон&hl=ru&gl=RU&ceid=RU:ru",
]

WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "4"))
MIN_IMPORTANCE = int(os.getenv("MIN_IMPORTANCE", "3"))
MAX_ITEMS = 50
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
    "gemini-3.5-flash-lite",
]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

KEYWORDS = ["sec ", "etf", "hack", "exploit", "listing", "ban", "lawsuit",
            "bitcoin", "ethereum", "btc", "eth", "stablecoin", "binance",
            "solana"]

CRYPTO_RE = re.compile(
    r"crypto|bitcoin|blockchain|digital asset|virtual asset|stablecoin|"
    r"\btoken|cbdc|ethereum|solana|"
    r"крипто|биткоин|блокчейн|цифров\w+ валют|цифров\w+ актив", re.I)
GOV_RE = re.compile(
    r"government|ministry|minister|central bank|regulator|treasury|congress|"
    r"senate|parliament|\bsec\b|cftc|\bdoj\b|white house|legislat|\bbill\b|"
    r"\blaw\b|sanction|federal reserve|european commission|\bimf\b|"
    r"правительств|министерств|центробанк|центральн\w+ банк|закон|регулятор|"
    r"парламент|госдум|налог", re.I)


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def text_of(it):
    return it["title"] + " " + it["summary"]


def is_sol(it):
    t = text_of(it).lower()
    return "solana" in t or re.search(r"\bsol\b", t) is not None


def is_gov(it):
    if it.get("gov_feed") and CRYPTO_RE.search(text_of(it)):
        return True
    t = text_of(it)
    return bool(CRYPTO_RE.search(t) and GOV_RE.search(t))


def read_feed(url, cutoff, gov_feed):
    items = []
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print("feed error", url, e)
        return items
    feed_title = feed.feed.get("title", url)
    for e in feed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if not ts:
            continue
        published = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc)
        if published < cutoff:
            continue
        src = e.get("source")
        source = (src.get("title") if src else None) or feed_title
        items.append({
            "title": clean(e.get("title")),
            "summary": clean(e.get("summary"))[:400],
            "link": e.get("link"),
            "source": source,
            "gov_feed": gov_feed,
        })
    return items


def fetch_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items = []
    for url in FEEDS:
        items += read_feed(url, cutoff, False)
    for url in GOV_FEEDS:
        items += read_feed(url, cutoff, True)
    # из гос-лент оставляем только то, где есть крипта
    items = [it for it in items
             if not it["gov_feed"] or CRYPTO_RE.search(text_of(it))]
    seen, unique = set(), []
    for it in items:
        key = it["title"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(it)
    # сначала Solana и госновости
    unique.sort(key=lambda it: not (is_sol(it) or is_gov(it)))
    return unique[:MAX_ITEMS]


def call_gemini(model, prompt):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent")
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=90,
    )
    if not r.ok:
        print("Gemini", model, r.status_code, r.text[:300])
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def analyze_with_gemini(items):
    payload_items = [
        {"id": i, "title": it["title"], "text": it["summary"]}
        for i, it in enumerate(items)
    ]
    prompt = (
        "Ты аналитик криптоновостей. Для каждой новости верни JSON-массив "
        "объектов: {\"id\": int, \"coins\": [тикеры], "
        "\"sentiment\": \"positive|negative|neutral\", "
        "\"importance\": 1-5, \"summary_ru\": \"1-2 предложения по-русски\"}. "
        "Важность 5 = влияет на рынок (взломы, решения регуляторов, ETF, "
        "крупные листинги). Не давай торговых советов. Только JSON.\n\n"
        + json.dumps(payload_items, ensure_ascii=False)
    )
    for model in GEMINI_MODELS:
        try:
            data = call_gemini(model, prompt)
            print("Gemini OK:", model)
            return {a["id"]: a for a in data}
        except Exception as e:
            print("Gemini error", model, e)
    return None


def analyze_with_keywords(items):
    result = {}
    for i, it in enumerate(items):
        t = text_of(it).lower()
        hits = sum(k in t for k in KEYWORDS)
        result[i] = {"coins": [], "sentiment": "neutral",
                     "importance": min(5, hits + 1), "summary_ru": it["title"]}
    return result


EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}


def build_digest(items, analysis):
    rows = []
    for i, it in enumerate(items):
        a = analysis.get(i)
        if not a or a.get("importance", 0) < MIN_IMPORTANCE:
            continue
        rows.append((a["importance"], i, it, a))
    rows.sort(key=lambda x: -x[0])
    if not rows:
        return None
    lines = ["<b>📰 Крипто-дайджест</b>\n"]
    for imp, _, it, a in rows:
        coins = ", ".join(a.get("coins") or [])
        coins = " [" + html.escape(coins) + "]" if coins else ""
        mark = ("◎ " if a.get("sol") else "") + ("🏛 " if a.get("gov") else "")
        lines.append(
            EMOJI.get(a.get("sentiment"), "⚪") + " " + mark
            + "<b>" + str(imp) + "/5</b>" + coins + "\n"
            + html.escape(a["summary_ru"]) + "\n"
            + '<a href="' + html.escape(it["link"] or "") + '">'
            + html.escape(it["source"]) + "</a>\n"
        )
    lines.append("<i>Не является инвестиционной рекомендацией.</i>")
    return "\n".join(lines)


def send_telegram(text):
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    chunks.append(cur)
    for c in chunks:
        r = requests.post(
            "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
            json={"chat_id": TG_CHAT, "text": c, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        print("Telegram:", r.status_code, r.text)
        r.raise_for_status()
        time.sleep(1)


def main():
    items = fetch_news()
    print("Новостей:", len(items))
    if not items:
        return
    analysis = None
    if GEMINI_KEY:
        analysis = analyze_with_gemini(items)
    if analysis is None:
        print("Используем фильтр по ключевым словам")
        analysis = analyze_with_keywords(items)
    # Solana и госновости показываем всегда
    for i, it in enumerate(items):
        sol, gov = is_sol(it), is_gov(it)
        if sol or gov:
            a = analysis.setdefault(i, {
                "coins": [], "sentiment": "neutral",
                "importance": MIN_IMPORTANCE, "summary_ru": it["title"],
            })
            a["importance"] = max(a.get("importance", 0), MIN_IMPORTANCE)
            a["sol"] = sol
            a["gov"] = gov
            if not a.get("summary_ru"):
                a["summary_ru"] = it["title"]
    digest = build_digest(items, analysis)
    if digest:
        send_telegram(digest)
    else:
        print("Нет важных новостей")


if __name__ == "__main__":
    main()
