import os, re, json, html, time, calendar
from datetime import datetime, timezone, timedelta

import feedparser
import requests

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "4"))
MIN_IMPORTANCE = int(os.getenv("MIN_IMPORTANCE", "3"))
MAX_ITEMS = 30
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

KEYWORDS = ["sec ", "etf", "hack", "exploit", "listing", "ban", "lawsuit",
            "bitcoin", "ethereum", "btc", "eth", "stablecoin", "binance"]


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("feed error", url, e)
            continue
        source = feed.feed.get("title", url)
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts:
                continue
            published = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc)
            if published < cutoff:
                continue
            items.append({
                "title": clean(e.get("title")),
                "summary": clean(e.get("summary"))[:400],
                "link": e.get("link"),
                "source": source,
            })
    seen, unique = set(), []
    for it in items:
        key = it["title"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique[:MAX_ITEMS]


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
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + GEMINI_MODEL + ":generateContent")
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=90,
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return {a["id"]: a for a in json.loads(text)}


def analyze_with_keywords(items):
    result = {}
    for i, it in enumerate(items):
        t = (it["title"] + " " + it["summary"]).lower()
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
        lines.append(
            EMOJI.get(a.get("sentiment"), "⚪") + " <b>" + str(imp) + "/5</b>" + coins + "\n"
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
        r.raise_for_status()
        time.sleep(1)


def main():
    items = fetch_news()
    print("Новостей:", len(items))
    if not items:
        return
    analysis = None
    if GEMINI_KEY:
        try:
            analysis = analyze_with_gemini(items)
        except Exception as e:
            print("Gemini error, fallback:", e)
    if analysis is None:
        analysis = analyze_with_keywords(items)
    digest = build_digest(items, analysis)
    if digest:
        send_telegram(digest)
    else:
        print("Нет важных новостей")


if __name__ == "__main__":
    main()
