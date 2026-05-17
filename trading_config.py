"""Trading bot configuration - API keys and risk parameters"""
import os, json

# ── Load .env file at module level ──
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path, encoding='utf-8') as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Binance API credentials (loaded from .env) ──
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY')

# ── Risk management ──
TOTAL_CAPITAL_USDT = 88.23       # 实际资金
LEVERAGE = 3                      # 低杠杆
PER_TRADE_RISK_PCT = 0.02        # 单笔风险 2%
DAILY_LOSS_LIMIT_PCT = 0.08      # 日亏损 8% 熔断
STOP_LOSS_PCT = 0.015            # 止损 1.5%
MAX_TRADES_PER_DAY = 3           # 每日最多 3 次
MIN_VOLUME_USDT = 5000000        # Min 5M USDT volume
MAX_POSITIONS = 1                 # 同时只开 1 单
COOLDOWN_MINUTES = 15            # 平仓后冷却 15 分钟
POSITION_SIZE_PCT = 0.10         # 每笔用 10% 资金（小仓位）

# ── Market scanning ──
SCAN_INTERVAL_SECONDS = 60       # Scan every 60 seconds
TOP_GAINERS_COUNT = 10           # Watch top 10 gainers
STRONG_TREND_THRESHOLD = 0.03    # 3%+ move = strong trend signal

# ── Telegram ──
TELEGRAM_BOT_TOKEN = None
TELEGRAM_PROXY = None

def _load_token():
    global TELEGRAM_BOT_TOKEN, TELEGRAM_PROXY
    if TELEGRAM_BOT_TOKEN is None:
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        if os.path.exists(env_path):
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if '=' in line:
                            k, _, v = line.partition('=')
                            v = v.strip()
                            if k.strip() == 'TELEGRAM_BOT_TOKEN':
                                TELEGRAM_BOT_TOKEN = v
                            elif k.strip() == 'PROXY_URL':
                                TELEGRAM_PROXY = v

# ── Qlib Integration Settings ──
QLIB_ENABLED = True                     # Master switch: Qlib validation on/off
QLIB_MIN_CONFIDENCE = 40.0             # Below this: block trade (0-100)
QLIB_MIN_CANDLES = 60                   # Minimum klines for valid factor computation
QLIB_STATS_LOG_INTERVAL = 60            # Log validator stats every N main cycles


def notify(text: str):
    """Send a notification message via Telegram."""
    _load_token()
    chat_id_path = os.path.join(os.path.dirname(__file__), '.trade_chat_id')
    if not os.path.exists(chat_id_path) or not TELEGRAM_BOT_TOKEN:
        return
    with open(chat_id_path) as f:
        cid = f.read().strip()
    if not cid:
        return

    try:
        import requests
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        proxies = {'http': TELEGRAM_PROXY, 'https': TELEGRAM_PROXY} if TELEGRAM_PROXY else None
        requests.post(url, json={
            'chat_id': int(cid),
            'text': text,
            'parse_mode': 'HTML'
        }, proxies=proxies, timeout=10)
    except Exception:
        pass
