import sys
import os
import json
import time
import logging
from typing import Set, Optional, Dict, Any
from pathlib import Path

try:
    import requests
    import feedparser
    from openai import OpenAI
except ImportError as e:
    print(f"❌ Ошибка: отсутствует библиотека {e.name}. Установите: pip install {e.name}")
    sys.exit(1)

# ========== КЛЮЧИ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not DEEPSEEK_API_KEY or not TG_BOT_TOKEN or not TG_CHAT_ID:
    logging.error("Отсутствуют переменные окружения")
    sys.exit(1)

# ========== НАСТРОЙКИ ==========
DB_FILE = "processed_news.json"

IMPORTANT_KEYWORDS = [
    "federal reserve", "fed", "powell", "fomc", "rate cut", "rate hike", "interest rate",
    "sec", "gary gensler", "cftc", "treasury", "yellen", "trump",
    "cpi", "nonfarm", "nfp", "unemployment", "gdp", "inflation",
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blackrock", "fidelity", "microstrategy",
    "etf", "spot etf", "binance", "coinbase", "tether", "usdt",
    "halving", "mining", "bankruptcy", "hack", "exploit", "fdic",
    "sanction", "war", "oil", "opec", "hormuz", "strait",
    "debt", "treasury", "bessent", "musalem", "crypto regulation", "clarity act",
    "stablecoin", "cbdc", "tokenization", "futures", "options", "liquidity", "volatility"
]

BLACKLIST_KEYWORDS = [
    "technical analysis", "chart", "moving average",
    "bollinger", "fibonacci", "trend line", "rsi", "macd",
    "argentina", "south africa", "syria", "yemen"
]

RSS_FEEDS = [
    "https://www.investing.com/rss/news_287.rss",
    "https://www.investing.com/rss/news_14.rss",
    "https://www.investing.com/rss/news_11.rss",
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
]

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ========== КЛАССЫ ==========
class DatabaseHandler:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)

    def load(self) -> Set[str]:
        if self.db_path.exists():
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data) if isinstance(data, list) else set()
            except Exception as e:
                logger.error(f"Ошибка чтения базы: {e}")
        return set()

    def save(self, urls: Set[str], limit: int = 500) -> None:
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(list(urls)[-limit:], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения базы: {e}")

class TelegramSender:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
            response = requests.post(self.api_url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False

class DeepSeekAnalyzer:
    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        self.model = model

    def analyze(self, title: str, summary: str) -> Optional[Dict[str, Any]]:
        system_prompt = (
            "Ты — крипто-трейдер, оцениваешь новости по шкале 1-10, где 10 — максимальное влияние на биткоин и рынок.\n"
            "Важны не только уже принятые решения, но и сигналы: заявления первых лиц, неожиданные макроданные, геополитические риски.\n\n"
            "Критерии:\n"
            "9-10: Решения ФРС/SEC (изменение ставки, одобрение/отказ ETF), взломы бирж, новые санкции, начало военных действий.\n"
            "7-8: Заявления Пауэлла, Генслера, Трампа, макростатистика США (CPI, NFP, GDP) с отклонением от прогноза >0.5%, переговоры с Ираном с упоминанием конкретных дат или ультиматумов.\n"
            "5-6: Обычные крипто-новости (партнёрства, запуски, обновления сетей), мнения аналитиков, обсуждение законопроектов без голосования, общий рост госдолга без новых решений.\n"
            "1-4: Технический анализ, графики, новости о странах вне G7/крупных экономик, не связанные с криптой или макро.\n\n"
            "Формат ответа (только JSON, без пояснений):\n"
            '{"score": 7, "sentiment": "Bullish", "summary": "Краткая выжимка на русском (2 предложения), суть и эффект на крипту."}\n'
            "sentiment может быть только Bullish, Bearish или Neutral."
        )
        user_prompt = f"Заголовок: {title}\nТекст: {summary}"
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
            )
            content = response.choices[0].message.content
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                return json.loads(content[start:end+1])
            else:
                logger.error(f"Не найден JSON: {content[:200]}")
                return None
        except Exception as e:
            logger.error(f"Ошибка DeepSeek: {e}")
            return None

class NewsMonitor:
    def __init__(self, rss_feeds, important_keywords, blacklist_keywords, db_handler, telegram, analyzer, delay=15):
        self.rss_feeds = rss_feeds
        self.important_keywords = important_keywords
        self.blacklist_keywords = blacklist_keywords
        self.db = db_handler
        self.telegram = telegram
        self.analyzer = analyzer
        self.delay = delay
        self.processed_urls = self.db.load()

    def _is_potentially_important(self, title: str, summary: str) -> bool:
        text = f"{title} {summary}".lower()
        if any(bw in text for bw in self.blacklist_keywords):
            return False
        return any(kw in text for kw in self.important_keywords)

    def _process_entry(self, entry) -> None:
        link = entry.get("link", "")
        if not link:
            return
        if link in self.processed_urls:
            return
        self.processed_urls.add(link)
        title = entry.title
        summary = entry.get("summary", "")
        if not self._is_potentially_important(title, summary):
            logger.info(f"Пропуск (шум): {title[:50]}...")
            return
        logger.info(f"Анализируем: {title[:60]}...")
        data = self.analyzer.analyze(title, summary)
        if not data:
            return
        score = data.get('score', 0)
        sentiment = data.get('sentiment', 'Neutral')
        logger.info(f"Оценка: {score}/10 | Тренд: {sentiment}")
        if score >= 6:
            sentiment_emoji = "🟢" if sentiment == "Bullish" else "🔴" if sentiment == "Bearish" else "⚪"
            msg = (
                f"⚡ <b>ВАЖНО [{score}/10]</b>\n"
                f"<b>Тренд:</b> {sentiment_emoji} {sentiment}\n\n"
                f"{data.get('summary')}\n\n"
                f"🔗 <a href='{link}'>Читать источник</a>"
            )
            if self.telegram.send(msg):
                logger.info("Сообщение доставлено в Telegram")
        else:
            logger.info(f"Оценка {score} < 6 – пропущено")

    def run_once(self) -> None:
        new_urls_found = False
        for feed_url in self.rss_feeds:
            if not feed_url.strip():
                continue
            logger.info(f"Читаем ленту: {feed_url}")
            try:
                feed = feedparser.parse(feed_url)
                if not feed.entries:
                    continue
                for entry in feed.entries[:3]:
                    self._process_entry(entry)
                    new_urls_found = True
                    time.sleep(self.delay)
            except Exception as e:
                logger.error(f"Ошибка ленты {feed_url}: {e}")
        if new_urls_found:
            self.db.save(self.processed_urls)
            logger.info("База обновлена")

def main():
    db = DatabaseHandler(DB_FILE)
    telegram = TelegramSender(TG_BOT_TOKEN, TG_CHAT_ID)
    analyzer = DeepSeekAnalyzer(DEEPSEEK_API_KEY)
    monitor = NewsMonitor(RSS_FEEDS, IMPORTANT_KEYWORDS, BLACKLIST_KEYWORDS, db, telegram, analyzer)
    logger.info("--- ЗАПУСК МОНИТОРИНГА (крипто-фильтр, порог 6) ---")
    monitor.run_once()
    logger.info("--- ЗАВЕРШЕНИЕ ---")

if __name__ == "__main__":
    main()
