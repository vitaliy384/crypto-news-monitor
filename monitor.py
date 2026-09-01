import sys
import os
import json
import time
import re
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
 
    # Геополитика, война, санкции
    "sanction", "war", "tariff", "trade war", "trade deal", "export ban", "import ban",
    "invasion", "ceasefire", "airstrike", "air strike", "missile strike", "drone attack",
    "military strike", "strikes on", "coup", "assassination", "nuclear",
    "oil", "opec", "hormuz", "strait", "brent", "wti",
    "gold", "silver", "metals", "bullion",
    "iran", "china", "russia", "ukraine", "trump",
 
    # Стихийные бедствия и сбои производства/логистики
    "earthquake", "typhoon", "hurricane", "wildfire", "flood", "explosion",
    "factory fire", "chip factory", "semiconductor plant", "port closure", "supply chain",
    "power outage", "blackout", "pipeline",
 
    # Долг/ликвидность
    "debt", "treasury buyback", "bond", "dollar", "liquidity", "volatility",
    "futures", "options"
]
 
# Технический шум и "пустые" рыночные сводки — не несут фактической
# информации, независимо от актива или темы.
BLACKLIST_KEYWORDS = [
    "technical analysis", "chart", "moving average",
    "bollinger", "fibonacci", "trend line", "rsi", "macd",
    "resistance", "support level", "live levels", "hourly levels",
    "overbought", "oversold", "breakout", "bear flag", "bull flag",
    "in focus", "what's moving", "morning bid", "week ahead", "day ahead",
    "top stories", "market wrap", "stocks to watch",
    "argentina", "south africa", "syria", "yemen"
]
 
RSS_FEEDS = [
    "https://www.investing.com/rss/news_14.rss",   # Economy News — быстрая реакция на макро/заявления
    "https://www.investing.com/rss/news_11.rss",   # Commodities & Futures — нефть, золото, тарифы
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",             # институциональные/регуляторные крипто-новости
    "https://www.aljazeera.com/xml/rss/all.xml",   # мировые события: войны, санкции, катастрофы
    "https://feeds.bbci.co.uk/news/world/rss.xml", # мировые события, официальный источник
 
    # Официальные источники — дополняют быстрые ленты для плановых
    # событий (решения по ставке, официальные ETF-решения), где
    # официальный пресс-релиз публикуется одновременно с событием.
    "https://www.federalreserve.gov/feeds/press_all.xml",             # все пресс-релизы ФРС
    "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml", # речи и показания перед Конгрессом
    "https://www.sec.gov/news/pressreleases.rss",                      # официальные пресс-релизы SEC
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
 
    def load(self) -> Dict[str, Set[str]]:
        """Возвращает {"urls": set(...), "titles": set(...)}.
        Поддерживает старый формат файла (просто список URL) для обратной совместимости."""
        if self.db_path.exists():
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        # старый формат — только URL, заголовков ещё не было
                        return {"urls": set(data), "titles": set()}
                    if isinstance(data, dict):
                        return {
                            "urls": set(data.get("urls", [])),
                            "titles": set(data.get("titles", [])),
                        }
            except Exception as e:
                logger.error(f"Ошибка чтения базы: {e}")
        return {"urls": set(), "titles": set()}
 
    def save(self, urls: Set[str], titles: Set[str], limit: int = 500) -> None:
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "urls": list(urls)[-limit:],
                        "titles": list(titles)[-limit:],
                    },
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            logger.error(f"Ошибка сохранения базы: {e}")
 
def normalize_title(title: str) -> str:
    """Приводит заголовок к виду для сравнения: нижний регистр, без пунктуации
    и лишних пробелов — чтобы ловить republish той же новости с чуть другой
    версткой заголовка."""
    text = title.lower()
    text = re.sub(r"[^\w\s]", "", text)  # убрать пунктуацию
    text = re.sub(r"\s+", " ", text).strip()
    return text
 
 
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
            "Ты — редактор новостной ленты. Твоя задача — отбирать только новости с "
            "ПРЯМЫМ, КОНКРЕТНЫМ и УЖЕ СВЕРШИВШИМСЯ фактическим влиянием на мировую "
            "экономику, торговлю, крипторынок и повседневную жизнь — а не рассуждения "
            "о том, как на что-то «отреагировал рынок» или что «может произойти».\n\n"
 
            "СТРОГО ИСКЛЮЧАЙ (оценка 1-2), даже если тема выглядит важной:\n"
            "- Любые формулировки о «настроении рынка», «реакции инвесторов», "
            "«рынок увидел позитив/негатив в словах X» — это интерпретация, а не факт\n"
            "- Обзорные статьи-дайджесты («что движет рынками сегодня», «утренняя сводка», "
            "«неделя впереди», «что в фокусе»)\n"
            "- Мнения, прогнозы и рассуждения аналитиков без нового факта\n"
            "- Технический анализ графиков (уровни, RSI, Fibonacci)\n\n"
 
            "Критерии оценки — только конкретные свершившиеся факты:\n"
            "9-10: военный удар, начало боевых действий, вторжение; официальное решение "
            "по процентной ставке (ФРС, ЕЦБ и т.п.); крупный взлом биржи или утечка "
            "данных; новые масштабные санкции; авария или катастрофа, остановившая "
            "производство критичной продукции (завод микрочипов, крупный порт, "
            "нефтепровод, электростанция).\n"
            "7-8: официально ВВЕДЁННЫЕ (не обсуждаемые, а принятые) новые тарифы или "
            "пошлины между крупными экономиками; прямое конкретное заявление главы "
            "государства или центробанка о принятом решении (не намёк и не "
            "предположение); крупное стихийное бедствие в промышленно значимом регионе "
            "(землетрясение, тайфун, наводнение, масштабные лесные пожары); КОНКРЕТНЫЙ "
            "ультиматум с названным условием и названным действием — например «нанесём "
            "удары, если к завтрашнему дню Иран не откроет пролив» — это ещё не "
            "свершившийся факт, но само обязательство конкретно и значимо.\n"
            "5-6: официально анонсированные переговоры по значимым темам с конкретной "
            "датой; макростатистика США (CPI, NFP, GDP) с отклонением от прогноза "
            "более 0.5%.\n"
            "1-4: всё остальное — включая партнёрства и обновления отдельных "
            "крипто-проектов без макро-значения, обсуждение ещё не принятых решений, "
            "новости о странах вне G7/крупных экономик без прямого влияния на мировой "
            "рынок; РАЗМЫТЫЕ формулировки без конкретики — «оставляем за собой право», "
            "«могут рассмотреть», «не исключают», «предупредили о возможных мерах» — "
            "это дипломатическая уклончивость, а не обязательство, даже если исходит "
            "от первого лица.\n\n"
 
            "Если сомневаешься, является ли новость проверяемым фактом или просто "
            "интерпретацией/мнением о реакции рынка — ставь низкую оценку.\n\n"
 
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
        db_data = self.db.load()
        self.processed_urls = db_data["urls"]
        self.processed_titles = db_data["titles"]
 
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
 
        title = entry.title
        norm_title = normalize_title(title)
        if norm_title and norm_title in self.processed_titles:
            # та же новость уже встречалась под другим URL (republish
            # в другой ленте того же или другого сайта)
            self.processed_urls.add(link)
            logger.info(f"Пропуск (дубликат заголовка): {title[:50]}...")
            return False
 
        self.processed_urls.add(link)
        if norm_title:
            self.processed_titles.add(norm_title)
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
            self.db.save(self.processed_urls, self.processed_titles)
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
 
