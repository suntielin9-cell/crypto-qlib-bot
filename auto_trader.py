# -*- coding: utf-8 -*-
"""Auto futures trader - EMA trend strategy on BTC/ETH"""
import os, json, time, sys
from datetime import datetime, timedelta
import traceback

_BASE = os.path.dirname(os.path.abspath(__file__))
_PID_FILE = os.path.join(_BASE, 'trader.pid')

# ── PID file lock: prevent multiple instances ──
def _check_pid_lock():
    """Exit if another instance is already running."""
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if that PID is still alive
            if sys.platform == 'win32':
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x400, False, old_pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    print('Another trader instance (PID %d) is running. Exiting.' % old_pid)
                    sys.exit(0)
            else:
                # Unix
                if os.path.exists('/proc/%d' % old_pid):
                    print('Another trader instance (PID %d) is running. Exiting.' % old_pid)
                    sys.exit(0)
        except (ValueError, OSError, AttributeError):
            # Stale PID file or no access — proceed
            pass
    with open(_PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

_check_pid_lock()

_CRASH_LOG = os.path.join(_BASE, 'trader_crash.log')
try:
    with open(_CRASH_LOG, 'a', encoding='utf-8') as f:
        f.write('\n=== STARTUP %s ===\n' % datetime.now().isoformat())
except:
    pass

_LOG_FILE = os.path.join(_BASE, 'trader.log')
_log_fh = open(_LOG_FILE, 'w', encoding='utf-8')

def wlog(msg):
    line = '%s %s' % (datetime.now().strftime('%m/%d %H:%M:%S'), msg)
    _log_fh.write(line + '\n')
    _log_fh.flush()

wlog('PID: %d starting' % os.getpid())
wlog('Python: %s' % sys.version)

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from binance.client import Client
from binance.exceptions import BinanceAPIException
import trading_config as cfg

# ── Qlib integration ──
qlib_validator = None
if cfg.QLIB_ENABLED:
    try:
        from qlib_integration.validator import QlibValidator
        qlib_validator = QlibValidator()
        wlog('[qlib] QlibValidator initialized')
    except Exception as e:
        wlog('[qlib] init failed: %s' % str(e)[:200])

wlog('imports done')

# Emoji constants (not in f-string expr to support Python 3.11)
EMOJI_GREEN = '\U0001f7e2'
EMOJI_RED = '\U0001f534'

def api_call(func, *args, max_retries=4, **kwargs):
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ProxyError,
                requests.exceptions.SSLError,
                BinanceAPIException) as e:
            last_exc = e
            if attempt < max_retries:
                wait = min(2 ** attempt * 5, 90)
                wlog('[retry %d/%d] %s' % (attempt, max_retries, type(e).__name__))
                time.sleep(wait)
            else:
                wlog('[fail %dx] %s: %s' % (max_retries, type(e).__name__, str(e)[:200]))
    raise last_exc


state = {
    'position': None,
    'daily_start_balance': 0.0,
    'daily_pnl': 0.0,
    'trades_today': 0,
    'cooldown_until': None,
    'paused': False,
    'last_notified_pnl': 0,
    'proxy_down_count': 0,
    'cycle': 0,
    # Qlib stats
    'qlib_blocked': 0,
    'qlib_confirmed': 0,
    'qlib_cycle': 0,
    'latest_qlib': None,
}

_STATS_FILE = os.path.join(_BASE, 'qlib_stats.json')


def write_qlib_stats(balance=0):
    """Write latest bot + Qlib stats to JSON for the monitoring dashboard."""
    pos = state.get('position')
    data = {
        'timestamp': datetime.now().isoformat(),
        'bot': {
            'pid': os.getpid(),
            'balance': round(balance, 2),
            'paused': state.get('paused', False),
            'daily_pnl': round(state.get('daily_pnl', 0), 2),
            'trades_today': state.get('trades_today', 0),
            'cycle': state.get('cycle', 0),
        },
        'position': {
            'symbol': pos['symbol'],
            'direction': pos['direction'],
            'qty': pos['qty'],
            'entry_price': pos['entry_price'],
            'stop_loss': pos['stop_loss'],
        } if pos else None,
        'qlib': {
            'total_validations': state.get('qlib_cycle', 0),
            'confirmed': state.get('qlib_confirmed', 0),
            'blocked': state.get('qlib_blocked', 0),
            'latest': state.get('latest_qlib'),
        },
        'validator': qlib_validator.get_stats() if qlib_validator else None,
    }
    try:
        with open(_STATS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def wait_for_proxy(client, max_wait=300):
    waited = 0
    while waited < max_wait:
        try:
            api_call(client.futures_account, max_retries=1)
            wlog('[proxy] connected (waited %ds)' % waited)
            return True
        except Exception as e:
            wlog('[proxy] %s (%ds)' % (type(e).__name__, waited))
            time.sleep(15)
            waited += 15
    return False


def get_balance(client):
    info = api_call(client.futures_account)
    for a in info['assets']:
        if a['asset'] == 'USDT':
            return float(a['walletBalance'])
    return 0.0


def recover_existing_position(client):
    """Detect open positions on Binance and load into state with stop loss."""
    try:
        positions = api_call(client.futures_position_information)
        for p in positions:
            amt = float(p['positionAmt'])
            if amt != 0:
                symbol = p['symbol']
                side = 'buy' if amt > 0 else 'sell'
                entry_price = float(p['entryPrice'])
                qty = abs(amt)
                stop_price = round(entry_price * (1 - cfg.STOP_LOSS_PCT) if side == 'buy'
                                   else entry_price * (1 + cfg.STOP_LOSS_PCT), 2)

                # Cancel ALL existing stops first (clean up duplicates from crashed instances)
                canceled = cancel_all_stop_orders(client, symbol)

                stop_side = 'SELL' if side == 'buy' else 'BUY'
                api_call(client.futures_create_order,
                         symbol=symbol, side=stop_side, type='STOP_MARKET',
                         quantity=qty, stopPrice=stop_price, reduceOnly=True)
                wlog('[recover] STOP_MARKET placed for %s at $%.2f' % (symbol, stop_price))

                state['position'] = {
                    'symbol': symbol, 'direction': side, 'qty': qty,
                    'entry_price': entry_price, 'stop_loss': stop_price,
                    'time': datetime.now().isoformat(),
                    'trail_stage': 0,
                    'highest_price': entry_price,
                }
                wlog('[recover] loaded %s %s %.4f @ $%.2f stop $%.2f'
                     % (symbol, side.upper(), qty, entry_price, stop_price))
                cfg.notify('%s Recovered existing position\n%s %s @ $%.2f\nStop: $%.2f'
                           % (EMOJI_GREEN, symbol, side.upper(), entry_price, stop_price))
                return True
    except Exception as e:
        wlog('[recover error] %s' % str(e)[:200])
    return False


def get_emas(client, symbol):
    klines = api_call(client.futures_klines, symbol=symbol,
                      interval=Client.KLINE_INTERVAL_4HOUR, limit=200)
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    def ema(data, period):
        m = 2 / (period + 1)
        r = [data[0]]
        for i in range(1, len(data)):
            r.append((data[i] - r[-1]) * m + r[-1])
        return r

    return {
        'close': closes[-1],
        'ema20': ema(closes, 20)[-1],
        'ema50': ema(closes, 50)[-1],
        'ema200': ema(closes, 200)[-1],
        'volume': volumes[-1],
        'avg_vol': sum(volumes[-20:]) / 20,
        'rsi': calc_rsi(closes, 14),
        'klines': klines,
    }


def calc_rsi(data, period=14):
    if len(data) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(len(data) - period, len(data)):
        diff = data[i] - data[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


TRADING_PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT']


def get_macro_trend(client, symbol):
    """Check daily chart for macro bias. Returns 'bullish', 'bearish', or 'neutral'."""
    try:
        klines = api_call(client.futures_klines, symbol=symbol,
                          interval=Client.KLINE_INTERVAL_1D, limit=55)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 20:
            return 'neutral'
        d_ema20 = ema(closes, 20)[-1]
        d_ema50 = ema(closes, 50)[-1] if len(closes) >= 50 else d_ema20
        price = closes[-1]

        if d_ema50 > d_ema20 and price > d_ema20:
            return 'bullish'
        if d_ema50 < d_ema20 and price < d_ema20:
            return 'bearish'
        return 'neutral'
    except Exception as e:
        wlog('[macro err %s] %s' % (symbol, str(e)[:100]))
        return 'neutral'


def check_signal(d, macro='neutral'):
    uptrend = d['ema50'] > d['ema200']
    downtrend = d['ema50'] < d['ema200']

    above_ema20 = d['close'] > d['ema20']
    below_ema20 = d['close'] < d['ema20']

    vol_ok = d['volume'] > d['avg_vol']
    dev = abs(d['close'] / d['ema20'] - 1)
    rsi = d.get('rsi', 50)

    # RSI filter: avoid buying overbought, avoid selling oversold
    rsi_high = rsi > 70
    rsi_low = rsi < 30

    # Macro determines direction: bearish=only short, bullish=only long
    if macro == 'bearish':
        if downtrend and below_ema20 and dev >= 0.015 and not rsi_low:
            return 'sell'
    elif macro == 'bullish':
        if uptrend and above_ema20 and dev >= 0.015 and not rsi_high:
            return 'buy'
    else:  # neutral: both directions with volume gate
        if downtrend and below_ema20 and dev >= 0.015 and not rsi_low and vol_ok:
            return 'sell'
        if uptrend and above_ema20 and dev >= 0.015 and not rsi_high and vol_ok:
            return 'buy'

    return None


# Symbols where bounce-short strategy applies (BTC bearish bias)
BOUNCE_SHORT_SYMBOLS = ['BTCUSDT']


def check_bounce_short(client, symbol):
    """BTC-specific: if in downtrend + strong bounce from low, open short."""
    if symbol not in BOUNCE_SHORT_SYMBOLS:
        return None
    try:
        klines = api_call(client.futures_klines, symbol=symbol,
                          interval=Client.KLINE_INTERVAL_4HOUR, limit=50)
        closes = [float(k[4]) for k in klines]
        lows = [float(k[3]) for k in klines]

        def ema(data, period):
            m = 2 / (period + 1)
            r = [data[0]]
            for i in range(1, len(data)):
                r.append((data[i] - r[-1]) * m + r[-1])
            return r

        e20 = ema(closes, 20)[-1]
        e50 = ema(closes, 50)[-1]
        e200 = ema(closes, 200)[-1] if len(closes) >= 200 else e50
        close = closes[-1]

        # Must be in downtrend
        if e50 >= e200:
            return None

        # Recent low and bounce
        recent_low = min(lows[-4:-1])
        bounce_pct = (close - recent_low) / recent_low
        below_ema20 = close < e20

        # Strong bounce from low but still below EMA20 = short opportunity
        if bounce_pct >= 0.015 and below_ema20:
            return 'sell'

        # Two consecutive up candles while below EMA20 = bounce short
        if len(closes) >= 3 and below_ema20:
            if closes[-1] > closes[-2] > closes[-3]:
                return 'sell'
    except Exception as e:
        wlog('[bounce_short err %s] %s' % (symbol, str(e)[:100]))
    return None


def signal_analysis(symbol, d, direction, macro='neutral'):
    """Generate detailed trading analysis text."""
    ema20, ema50, ema200 = d['ema20'], d['ema50'], d['ema200']
    close = d['close']
    vol_ratio = d['volume'] / d['avg_vol'] if d['avg_vol'] > 0 else 1.0

    trend = 'UP' if ema50 > ema200 else 'DOWN'
    price_vs_ema20 = 'ABOVE' if close > ema20 else 'BELOW'

    lines = []
    lines.append('=' * 34)
    lines.append('  %s SIGNAL ANALYSIS' % symbol)
    lines.append('=' * 34)
    lines.append('4H Trend  : %s (EMA50 %.2f %s EMA200 %.2f)'
                 % (trend, ema50, '>' if ema50 > ema200 else '<', ema200))
    lines.append('Macro     : %s' % macro.upper())
    lines.append('Price     : $%.2f' % close)
    lines.append('  vs EMA20: %s (%.2f) %s' % (price_vs_ema20, ema20,
                  'bullish momentum' if close > ema20 else 'bearish momentum'))
    lines.append('  vs EMA50: %.2f  vs EMA200: %.2f' % (ema50, ema200))
    lines.append('Volume    : %.1f%% of avg (%.2f / %.2f)'
                 % (vol_ratio * 100, d['volume'], d['avg_vol']))
    lines.append('RSI(14)   : %.1f' % d.get('rsi', 50))
    if direction == 'buy':
        lines.append('Rationale : Uptrend + price above EMA20 + RSI not overbought')
    else:
        lines.append('Rationale : Downtrend + price below EMA20 + RSI not oversold')
    lines.append('Stop Loss : %.1f%% fixed (adjustable via trailing)' % (cfg.STOP_LOSS_PCT * 100))
    lines.append('Deviation : %.2f%% from EMA20' % ((close / ema20 - 1) * 100))
    lines.append('=' * 34)
    return '\n'.join(lines)


def cancel_all_stop_orders(client, symbol):
    """Cancel ALL STOP_MARKET (conditional) orders for a symbol using Algo API."""
    count = 0
    try:
        # Query existing conditional orders
        algos = api_call(client.futures_get_open_algo_orders)
        existing = [a for a in algos if a.get('symbol') == symbol and a.get('algoStatus') == 'NEW']
        count = len(existing)
        if count > 0:
            api_call(client.futures_cancel_all_algo_open_orders, symbol=symbol)
            if count > 1:
                wlog('[cancel] removed %d duplicate conditional stops for %s' % (count, symbol))
        return count
    except Exception as e:
        wlog('[cancel_stop %s] %s' % (symbol, str(e)[:100]))
    return count


def set_leverage(client, symbol):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=cfg.LEVERAGE)
    except:
        pass
    try:
        client.futures_change_margin_type(symbol=symbol, marginType='ISOLATED')
    except:
        pass


def get_precision(client, symbol):
    info = api_call(client.futures_exchange_info)
    for s in info['symbols']:
        if s['symbol'] == symbol:
            return int(s['quantityPrecision'])
    return 3


def open_trade(client, symbol, direction, balance, analysis=None):
    ticker = api_call(client.futures_symbol_ticker, symbol=symbol)
    price = float(ticker['price'])

    pos_usdt = balance * cfg.POSITION_SIZE_PCT
    precision = get_precision(client, symbol)
    set_leverage(client, symbol)

    raw_qty = (pos_usdt * cfg.LEVERAGE) / price
    qty = round(raw_qty - (raw_qty % (10 ** -precision)), precision)
    if qty <= 0:
        cfg.notify('[SKIP] %s qty too small: %s' % (symbol, qty))
        return None

    side = 'BUY' if direction == 'buy' else 'SELL'
    stop_side = 'SELL' if direction == 'buy' else 'BUY'

    try:
        order = api_call(client.futures_create_order, symbol=symbol,
                         side=side, type='MARKET', quantity=qty,
                         newOrderRespType='RESULT')
        fill_price = float(order.get('avgPrice') or order.get('price') or price)

        stop_price = round(fill_price * (1 - cfg.STOP_LOSS_PCT) if direction == 'buy'
                           else fill_price * (1 + cfg.STOP_LOSS_PCT), 2)
        api_call(client.futures_create_order,
                 symbol=symbol, side=stop_side, type='STOP_MARKET',
                 quantity=qty, stopPrice=stop_price, reduceOnly=True)

        entry = {
            'symbol': symbol, 'direction': direction, 'qty': qty,
            'entry_price': fill_price, 'stop_loss': stop_price,
            'time': datetime.now().isoformat(),
            'trail_stage': 0,
            'highest_price': fill_price,
        }
        state['position'] = entry
        state['trades_today'] += 1

        msg_lines = ['%s OPEN %s %s' % (EMOJI_GREEN, direction.upper(), symbol),
                     'Entry: $%.2f  Qty: %s' % (fill_price, qty),
                     'Stop: $%.2f  Size: %.1f USDT x %dx' % (stop_price, pos_usdt, cfg.LEVERAGE)]
        if analysis:
            msg_lines.insert(0, analysis)
        msg = '\n'.join(msg_lines)
        cfg.notify(msg)
        wlog('\n' + msg)
        return entry

    except BinanceAPIException as e:
        cfg.notify('[OPEN FAIL] %s: %s' % (symbol, str(e)[:200]))
        wlog('[OPEN FAIL] %s' % str(e)[:200])
        # If MARKET order filled but STOP_MARKET failed, close the position
        try:
            positions = api_call(client.futures_position_information, symbol=symbol)
            for p in positions:
                if p['symbol'] == symbol and float(p['positionAmt']) != 0:
                    close_side = 'BUY' if float(p['positionAmt']) < 0 else 'SELL'
                    api_call(client.futures_create_order, symbol=symbol,
                             side=close_side, type='MARKET',
                             quantity=abs(float(p['positionAmt'])),
                             reduceOnly=True)
                    wlog('[cleanup] closed residual %s position' % symbol)
                    break
        except:
            pass
        return None


def close_position(client, reason='manual'):
    pos = state['position']
    if not pos:
        return

    side = 'SELL' if pos['direction'] == 'buy' else 'BUY'
    try:
        api_call(client.futures_create_order,
                 symbol=pos['symbol'], side=side, type='MARKET',
                 quantity=pos['qty'], reduceOnly=True)
        ticker = api_call(client.futures_symbol_ticker, symbol=pos['symbol'])
        exit_price = float(ticker['price'])

        pnl = (exit_price - pos['entry_price']) / pos['entry_price']
        if pos['direction'] == 'sell':
            pnl = -pnl
        pnl_pct = pnl * 100
        pnl_usdt = pnl_pct / 100 * (pos['qty'] * pos['entry_price']) / cfg.LEVERAGE

        state['daily_pnl'] += pnl_usdt
        state['position'] = None
        state['cooldown_until'] = datetime.now() + timedelta(minutes=cfg.COOLDOWN_MINUTES)

        icon = EMOJI_RED if pnl_usdt < 0 else EMOJI_GREEN
        msg = ('%s CLOSE %s\n%s %s\nEntry: $%.2f -> Exit: $%.2f\nPNL: %+.2f USDT (%+.2f%%)\nDaily: %+.2f USDT'
               % (icon, reason,
                  pos['symbol'], pos['direction'].upper(),
                  pos['entry_price'], exit_price,
                  pnl_usdt, pnl_pct, state['daily_pnl']))
        cfg.notify(msg)
        wlog('\n' + msg)

        if state['daily_pnl'] <= -cfg.DAILY_LOSS_LIMIT_PCT * state['daily_start_balance']:
            state['paused'] = True
            cfg.notify('CIRCUIT BREAKER! Daily loss limit %.0f%% reached, trading paused'
                       % (cfg.DAILY_LOSS_LIMIT_PCT * 100))

        return pnl_usdt

    except BinanceAPIException as e:
        wlog('[CLOSE FAIL] %s' % str(e)[:200])
        return None


TRAIL_BE_THRESHOLD = 1.0    # % profit to move stop to break-even
TRAIL_ACTIVATE = 3.0        # % profit to activate trailing
TRAIL_DISTANCE = 0.01       # 1% trail distance


def monitor_position(client):
    if not state['position']:
        return

    pos = state['position']
    try:
        positions = api_call(client.futures_position_information, symbol=pos['symbol'])
        for p in positions:
            if p['symbol'] == pos['symbol'] and float(p['positionAmt']) == 0:
                wlog('[mon] %s closed (stop hit)' % pos['symbol'])
                state['position'] = None
                state['cooldown_until'] = datetime.now() + timedelta(minutes=cfg.COOLDOWN_MINUTES)
                # Estimate PnL from current mark price
                try:
                    tick = api_call(client.futures_symbol_ticker, symbol=pos['symbol'])
                    exit_pr = float(tick['price'])
                    raw = (exit_pr - pos['entry_price']) / pos['entry_price']
                    if pos['direction'] == 'sell':
                        raw = -raw
                    pnl_usdt = raw * (pos['qty'] * pos['entry_price']) / cfg.LEVERAGE
                    state['daily_pnl'] += pnl_usdt
                    icon = EMOJI_RED if pnl_usdt < 0 else EMOJI_GREEN
                    cfg.notify('%s %s closed by stop\nPNL: %+.2f USDT (%+.1f%%)'
                               % (icon, pos['symbol'], pnl_usdt, raw * 100))
                except:
                    cfg.notify('%s Stop loss triggered, auto closed' % pos['symbol'])
                return

        ticker = api_call(client.futures_symbol_ticker, symbol=pos['symbol'])
        cur = float(ticker['price'])
        pnl = (cur - pos['entry_price']) / pos['entry_price']
        if pos['direction'] == 'sell':
            pnl = -pnl
        pnl_pct = round(pnl * 100)
        direction = pos['direction']
        symbol = pos['symbol']

        if abs(pnl_pct - state['last_notified_pnl']) >= 1:
            state['last_notified_pnl'] = pnl_pct
            wlog('  %s %s: %+d%%' % (symbol, direction.upper(), pnl_pct))

        # ── Trailing stop logic ──
        trail_stage = pos.get('trail_stage', 0)

        # Stage 1: Move stop to break-even at 1% profit
        if trail_stage == 0 and pnl_pct >= TRAIL_BE_THRESHOLD:
            stop_side = 'SELL' if direction == 'buy' else 'BUY'
            if cancel_all_stop_orders(client, symbol):
                api_call(client.futures_create_order,
                         symbol=symbol, side=stop_side, type='STOP_MARKET',
                         quantity=pos['qty'], stopPrice=pos['entry_price'],
                         reduceOnly=True)
                pos['trail_stage'] = 1
                pos['stop_loss'] = pos['entry_price']
                msg = ('%s %s BREAK-EVEN activated (+%d%%)\n'
                       'Stop moved to entry $%.2f' % (EMOJI_GREEN, symbol, pnl_pct, pos['entry_price']))
                cfg.notify(msg)
                wlog('[trail] %s' % msg)
                return

        # Stage 2: Activate trailing at 3% profit
        if trail_stage == 1 and pnl_pct >= TRAIL_ACTIVATE:
            stop_side = 'SELL' if direction == 'buy' else 'BUY'
            trail_dist = TRAIL_DISTANCE
            new_stop = round(cur * (1 - trail_dist), 2) if direction == 'buy' else round(cur * (1 + trail_dist), 2)
            if cancel_all_stop_orders(client, symbol):
                api_call(client.futures_create_order,
                         symbol=symbol, side=stop_side, type='STOP_MARKET',
                         quantity=pos['qty'], stopPrice=new_stop, reduceOnly=True)
                pos['trail_stage'] = 2
                pos['stop_loss'] = new_stop
                pos['trail_peak'] = cur
                locked_pnl = (new_stop / pos['entry_price'] - 1) * 100 if direction == 'buy' \
                             else (1 - new_stop / pos['entry_price']) * 100
                msg = ('%s %s TRAILING activated (+%d%%)\n'
                       'Stop: $%.2f (locked ~%.1f%%)' % (EMOJI_GREEN, symbol, pnl_pct, new_stop, locked_pnl))
                cfg.notify(msg)
                wlog('[trail] %s' % msg)
                return

        # Stage 2+: Update trailing stop as price moves favorably
        if trail_stage >= 2:
            trail_peak = pos.get('trail_peak', pos['entry_price'])
            is_new_peak = (direction == 'buy' and cur > trail_peak) or \
                          (direction == 'sell' and cur < trail_peak)
            if is_new_peak:
                pos['trail_peak'] = cur
                trail_dist = TRAIL_DISTANCE
                new_stop = round(cur * (1 - trail_dist), 2) if direction == 'buy' \
                           else round(cur * (1 + trail_dist), 2)

                stop_needs_update = (direction == 'buy' and new_stop > pos['stop_loss']) or \
                                    (direction == 'sell' and new_stop < pos['stop_loss'])
                if stop_needs_update:
                    stop_side = 'SELL' if direction == 'buy' else 'BUY'
                    if cancel_all_stop_orders(client, symbol):
                        api_call(client.futures_create_order,
                                 symbol=symbol, side=stop_side, type='STOP_MARKET',
                                 quantity=pos['qty'], stopPrice=new_stop, reduceOnly=True)
                        pos['stop_loss'] = new_stop
                        wlog('[trail] %s stop updated to $%.2f (peak $%.2f, +%d%%)'
                             % (symbol, new_stop, cur, pnl_pct))

    except Exception as e:
        wlog('[mon error] %s' % str(e)[:200])


def main():
    wlog('main() started')

    # Try proxy first, fallback to direct (VPS outside China)
    proxy = 'http://127.0.0.1:7897'
    try:
        os.environ['HTTP_PROXY'] = proxy
        os.environ['HTTPS_PROXY'] = proxy
        client = Client(cfg.BINANCE_API_KEY, cfg.BINANCE_SECRET_KEY,
                        requests_params={'timeout': 60})
        client.session.trust_env = True
        client.session.proxies.update({'http': proxy, 'https': proxy})
        # Test connection
        api_call(client.futures_account, max_retries=1)
        wlog('[proxy] connected via proxy')
    except Exception:
        wlog('[proxy] proxy not available, trying direct connection...')
        os.environ.pop('HTTP_PROXY', None)
        os.environ.pop('HTTPS_PROXY', None)
        client = Client(cfg.BINANCE_API_KEY, cfg.BINANCE_SECRET_KEY,
                        requests_params={'timeout': 60})
        client.session.trust_env = False
        client.session.proxies = {}
    wlog('client created')

    # Init all trading pairs
    for sym in TRADING_PAIRS:
        set_leverage(client, sym)
    wlog('pairs initialized: %s' % ', '.join(TRADING_PAIRS))

    wlog('request retry wrapper disabled (api_call handles retries)')

    # Wait for proxy
    wlog('[proxy] checking connection...')
    if not wait_for_proxy(client):
        wlog('[proxy] failed to connect, exiting')
        try:
            cfg.notify('Trader startup failed: proxy unreachable')
        except:
            pass
        return

    try:
        state['daily_start_balance'] = get_balance(client)
        wlog('balance: %.8f' % state['daily_start_balance'])
    except Exception as e:
        wlog('get_balance failed: %s' % str(e)[:200])
        try:
            cfg.notify('Trader startup failed: cannot get balance')
        except:
            pass
        return

    wlog('=' * 50)
    wlog('Auto trader starting... balance: %.2f' % state['daily_start_balance'])
    wlog('=' * 50)

    try:
        cfg.notify('Auto trader started!\nBalance: %.2f USDT\nLeverage: %dx'
                   % (state['daily_start_balance'], cfg.LEVERAGE))
    except Exception as e:
        wlog('notify err: %s' % str(e)[:200])

    # Recover any existing open positions from previous session
    recover_existing_position(client)

    wlog('entering main loop')

    while True:
        try:
            now = datetime.now()

            try:
                balance = get_balance(client)
                state['proxy_down_count'] = 0
            except Exception as e:
                state['proxy_down_count'] += 1
                wait = min(state['proxy_down_count'] * 30, 300)
                wlog('[proxy down %dx] %s' % (state['proxy_down_count'], type(e).__name__))
                time.sleep(wait)
                continue

            if state['daily_pnl'] <= -cfg.DAILY_LOSS_LIMIT_PCT * state['daily_start_balance']:
                state['paused'] = True

            if state['paused']:
                wlog('[paused] daily loss limit. balance: %.2f' % balance)
                time.sleep(300)
                continue

            if state['position']:
                try:
                    monitor_position(client)
                except Exception as e:
                    wlog('[monitor error] %s' % str(e)[:200])
            else:
                if state['cooldown_until'] and now < state['cooldown_until']:
                    remain = int((state['cooldown_until'] - now).total_seconds() / 60)
                    if state['cycle'] % 4 == 0:
                        wlog('[cooldown] %d min remaining' % remain)
                    time.sleep(60)
                    state['cycle'] += 1
                    continue

                wlog('[%s] scanning...' % now.strftime('%H:%M'))
                for symbol in TRADING_PAIRS:
                    if state['trades_today'] >= cfg.MAX_TRADES_PER_DAY:
                        wlog('daily limit %d trades reached' % cfg.MAX_TRADES_PER_DAY)
                        break
                    try:
                        d = get_emas(client, symbol)
                        macro = get_macro_trend(client, symbol)
                        signal = check_signal(d, macro)
                        if not signal:
                            signal = check_bounce_short(client, symbol)
                        if signal:
                            # ── Qlib factor validation ──
                            if qlib_validator and cfg.QLIB_ENABLED:
                                try:
                                    qr = qlib_validator.validate(
                                        client, symbol, signal, d.get('klines', []), d)
                                    state['qlib_cycle'] += 1
                                    state['latest_qlib'] = qr
                                    if qr['blocked']:
                                        state['qlib_blocked'] += 1
                                        wlog('[QLIB-BLOCK] %s %s conf=%.1f reason=%s'
                                             % (symbol, signal.upper(), qr['confidence'],
                                                qr['reason'] or '?'))
                                        continue
                                    else:
                                        state['qlib_confirmed'] += 1
                                        wlog('[QLIB-CONFIRM] %s %s conf=%.1f'
                                             % (symbol, signal.upper(), qr['confidence']))
                                except Exception as qe:
                                    wlog('[qlib err %s] %s' % (symbol, str(qe)[:150]))
                            elif state['qlib_cycle'] % 30 == 0:
                                wlog('[qlib] validator not available')

                            analysis = signal_analysis(symbol, d, signal, macro)
                            if symbol in BOUNCE_SHORT_SYMBOLS:
                                analysis = ('BTC bearish bias, bounce-short triggered.\n'
                                            'Downtrend + strong bounce from low.\n' + analysis)
                            wlog('\n' + analysis)
                            open_trade(client, symbol, signal, balance, analysis)
                            time.sleep(3)
                    except Exception as e:
                        wlog('[scan %s fail] %s' % (symbol, str(e)[:200]))

            state['cycle'] += 1
            write_qlib_stats(balance)
            time.sleep(60)

        except KeyboardInterrupt:
            wlog('\nstopping')
            if state['position']:
                close_position(client, 'manual')
            try:
                cfg.notify('Trader stopped')
            except:
                pass
            break
        except Exception as e:
            wlog('[error] %s: %s' % (type(e).__name__, str(e)[:200]))
            time.sleep(30)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        try:
            with open(_CRASH_LOG, 'a', encoding='utf-8') as f:
                f.write('[%s] UNCAUGHT:\n' % datetime.now().isoformat())
                f.write(traceback.format_exc() + '\n')
        except:
            pass
        raise
