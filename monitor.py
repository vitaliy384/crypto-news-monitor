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
 
# Сколько последних записей из каждой ленты обрабатывать за один прогон.
# При часовом расписании (schedule раз в час) 10 записей на ленту дают
# хороший запас, чтобы не терять новости между прогонами.
ENTRIES_PER_FEED = 10
 
IMPORTANT_KEYWORDS = [
    # Регуляторы и центробанки (актуальные фигуры на август 2026)
    "federal reserve", "fed", "fomc", "warsh", "powell",  # Kevin Warsh — текущий глава ФРС с мая 2026
    "sec", "atkins", "gensler",  # Paul Atkins — текущий глава SEC с апреля 2025
    "cftc", "treasury", "bessent", "yellen",
    "rate cut", "rate hike", "interest rate",
 
    # Макростатистика США
    "cpi", "nonfarm", "nfp", "unemployment", "gdp", "inflation", "jobs report",
 
    # Крипто
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blackrock", "fidelity", "microstrategy",
    "etf", "spot etf", "binance", "coinbase", "tether", "usdt",
    "halving", "mining", "bankruptcy", "hack", "exploit", "fdic",
    "stablecoin", "cbdc", "tokenization", "clarity act", "crypto regulation",
 
    # Геополитика и сырьё — то, что может двигать рынок в целом,
    # даже если новость формально не про крипту
    "sanction", "war", "tariff", "trade war",
    "oil", "opec", "hormuz", "strait", "brent", "wti",
    "gold", "silver", "metals", "bullion",
    "iran", "china", "trump",
 
    # Долг/ликвидность
    "debt", "treasury buyback", "bond", "dollar", "liquidity", "volatility",
    "futures", "options"
]
 
# Технический шум по ЛЮБОМУ активу (не только крипте) — не несёт
# фундаментальной информации, независимо от того, нефть это, золото
# или альткоин.
BLACKLIST_KEYWORDS = [
    "technical analysis", "chart", "moving average",
    "bollinger", "fibonacci", "trend line", "rsi", "macd",
    "resistance", "support level", "live levels", "hourly levels",
    "overbought", "oversold", "breakout", "bear flag", "bull flag",
    "argentina", "south africa", "syria", "yemen"
]
 
RSS_FEEDS = [
    "https://www.investing.com/rss/news_14.rss",   # Economy News — быстрая реакция на макро/заявления
    "https://www.investing.com/rss/news_11.rss",   # Commodities & Futures — нефть, золото, тарифы
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.theblock.co/rss.xml",             # институциональные/регуляторные крипто-новости
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
            "Ты — опытный крипто-трейдер и макро-аналитик. Твоя задача — оценивать новости "
            "по шкале 1-10 с точки зрения их влияния на биткоин и крипторынок в целом.\n\n"
 
            "ВАЖНЫЙ ПРИНЦИП: рынок реагирует не только на уже свершившиеся факты, но и на "
            "СИГНАЛЫ — заявления первых лиц на пресс-конференциях, неожиданные макроданные, "
            "геополитическую эскалацию или деэскалацию. Новость не обязана содержать слово "
            "'крипто' или 'биткоин', чтобы быть важной: обвал/рост доллара, шок нефтяных или "
            "золотых цен, начало/завершение военного конфликта, крупные санкции или тарифная "
            "война — всё это может двигать крипторынок так же сильно, как и новости напрямую "
            "о нём. Пример: заявление министра финансов США может обрушить или разогнать "
            "биткоин на 15-20% за день, даже если формально речь шла об облигациях, а не о крипте.\n\n"
 
            "Критерии оценки:\n"
            "9-10: Решения ФРС/SEC (изменение ставки, одобрение/отказ ETF), взломы крупных бирж, "
            "новые масштабные санкции, начало военных действий, резкий обвал/скачок доллара или "
            "гособлигаций США.\n"
            "7-8: Публичные заявления и комментарии на пресс-конференциях главы ФРС (Кевин Уорш), "
            "главы SEC (Пол Аткинс), министра финансов (Скотт Бессент), Трампа — по ставкам, "
            "тарифам, санкциям, крипторегулированию; макростатистика США (CPI, NFP, GDP) с "
            "отклонением от прогноза >0.5%; резкие движения нефти/золота (>3-5% за короткий "
            "срок) на фоне геополитики (Иран, Ормузский пролив, войны, ОПЕК); конкретные даты "
            "или ультиматумы в переговорах с Ираном.\n"
            "5-6: Обычные крипто-новости (партнёрства, запуски, обновления сетей), мнения "
            "аналитиков, обсуждение законопроектов без голосования, плановый рост госдолга без "
            "новых решений, умеренные движения нефти/металлов без явного триггера.\n"
            "1-4: Технический анализ и графики (уровни, RSI, Fibonacci — неважно, по крипте, "
            "нефти или золоту), новости узко об одной конкретной монете без макро-контекста "
            "(например, обновление кода одного альткоина), новости о странах вне G7/крупных "
            "экономик, не связанные с криптой или макро.\n\n"
 
            "ВАЖНО: если новость — это неподтверждённый слух или основана на анонимных "
            "источниках ('по данным источников', 'предположительно'), не завышай оценку до "
            "уровня 9-10 — такое максимум 6-7, если только рынок уже не отреагировал.\n\n"
 
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
    def __init__(self, rss_feeds, important_keywords, blacklist_keywords, db_handler, telegram, analyzer,
                 entries_per_feed=10, delay=15):
        self.rss_feeds = rss_feeds
        self.important_keywords = important_keywords
        self.blacklist_keywords = blacklist_keywords
        self.db = db_handler
        self.telegram = telegram
        self.analyzer = analyzer
        self.entries_per_feed = entries_per_feed
        self.delay = delay
        self.processed_urls = self.db.load()
 
    def _is_potentially_important(self, title: str, summary: str) -> bool:
        text = f"{title} {summary}".lower()
        if any(bw in text for bw in self.blacklist_keywords):
            return False
        return any(kw in text for kw in self.important_keywords)
 
    def _process_entry(self, entry) -> bool:
        """Обрабатывает одну запись. Возвращает True, если был реальный
        вызов DeepSeek API (и, значит, нужна пауза перед следующим запросом)."""
        link = entry.get("link", "")
        if not link:
            return False
        if link in self.processed_urls:
            return False
        self.processed_urls.add(link)
        title = entry.title
        summary = entry.get("summary", "")
        if not self._is_potentially_important(title, summary):
            logger.info(f"Пропуск (шум): {title[:50]}...")
            return False
        logger.info(f"Анализируем: {title[:60]}...")
        data = self.analyzer.analyze(title, summary)
        if not data:
            return True  # вызов API был (пусть и неудачный) — пауза всё равно нужна
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
        return True
 
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
                for entry in feed.entries[:self.entries_per_feed]:
                    api_was_called = self._process_entry(entry)
                    new_urls_found = True
                    if api_was_called:
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
    monitor = NewsMonitor(
        RSS_FEEDS, IMPORTANT_KEYWORDS, BLACKLIST_KEYWORDS, db, telegram, analyzer,
        entries_per_feed=ENTRIES_PER_FEED
    )
    logger.info("--- ЗАПУСК МОНИТОРИНГА (крипто-фильтр, порог 6) ---")
    monitor.run_once()
    logger.info("--- ЗАВЕРШЕНИЕ ---")
 
if __name__ == "__main__":
    main()
 
 
