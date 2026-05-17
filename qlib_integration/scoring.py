"""Confidence scoring engine — converts Alpha158 factors into a 0-100 score.

8 weighted factor groups vote on whether a trade signal should proceed:
  - Trend (20%):   MA5/10/20/30 — price vs moving average
  - Momentum (15%): ROC5/10/20 — rate of change
  - Price Level (15%): RANK20/60 — position in recent range
  - KBAR (10%):    KMID, KSFT2 — candlestick pattern
  - Volatility (10%): STD20/60 — volatility regime
  - TrendStrength (10%): RSQR20/60 — trend quality
  - Volume (10%):  VMA5/20 — volume confirmation
  - VWAP (10%):    VWAP0 — price vs VWAP

Each group score is in [-1, 1]. Weighted average maps to confidence [0, 100].
"""
import numpy as np


def _clip(score):
    return float(np.clip(score, -1.0, 1.0))


def _mean_valid(values):
    vals = [v for v in values if v is not None and not np.isnan(v)]
    return float(np.mean(vals)) if vals else 0.0


# ── Group scoring functions ──

def score_trend(factors, direction):
    """MA factors: >1 = bullish, <1 = bearish.
    Score in [-1, 1]: at 2.5% deviation from 1.0 = full score.
    """
    names = ['MA5', 'MA10', 'MA20', 'MA30']
    scores = []
    for name in names:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        score = _clip((val - 1.0) * 40)
        if direction == 'sell':
            score = -score
        scores.append(score)
    return _mean_valid(scores)


def score_momentum(factors, direction):
    """ROC factors: <1 = declining, >1 = rising. Same scaling as trend."""
    names = ['ROC5', 'ROC10', 'ROC20']
    scores = []
    for name in names:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        score = _clip((val - 1.0) * 40)
        if direction == 'sell':
            score = -score
        scores.append(score)
    return _mean_valid(scores)


def score_price_level(factors, direction):
    """RANK: 0 = period low, 1 = period high.
    For BUY: prefer RANK < 0.5 (room to run, not overbought).
    For SELL: prefer RANK > 0.5 (room to fall, not oversold).
    """
    names = ['RANK20', 'RANK60']
    scores = []
    for name in names:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        if direction == 'buy':
            score = 1.0 - 2.0 * val  # RANK 0 -> +1, RANK 1 -> -1
        else:
            score = 2.0 * val - 1.0  # RANK 0 -> -1, RANK 1 -> +1
        scores.append(score)
    return _mean_valid(scores)


def score_kbar(factors, direction):
    """KMID > 0 = bullish candle, < 0 = bearish.
    KSFT2 > 0 = bullish shape, < 0 = bearish shape.
    """
    scores = []
    for name in ['KMID', 'KSFT2']:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        score = _clip(val * 20)  # 5% body = full score
        if direction == 'sell':
            score = -score
        scores.append(score)
    return _mean_valid(scores)


def score_volatility(factors, direction):
    """Higher vol = less predictable = lower confidence in either direction.
    Typical crypto 4H STD is ~1-3%. Score max at 1%, min at 5%.
    """
    names = ['STD20', 'STD60']
    scores = []
    for name in names:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        # STD 0.01 (1%) -> +0.5, STD 0.05 (5%) -> -0.5
        score = _clip(0.5 - (val - 0.01) * 12.5)
        scores.append(score)
    return _mean_valid(scores)


def score_trend_strength(factors, direction):
    """RSQR high = trend is clean/strong = more confidence.
    RSQR is R^2 of trend fit, range 0-1. Higher = better trend.
    """
    names = ['RSQR20', 'RSQR60']
    scores = []
    for name in names:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        # RSQR 0 -> -0.5, RSQR 0.5 -> 0, RSQR 1 -> +0.5
        score = _clip(val * 2 - 0.5)
        scores.append(score)
    return _mean_valid(scores)


def score_volume(factors, direction):
    """VMA > 1 = volume above average = confirmation.
    For both directions: higher volume = more conviction.
    """
    names = ['VMA5', 'VMA20']
    scores = []
    for name in names:
        val = factors.get(name)
        if val is None or np.isnan(val):
            continue
        # VMA 0.5 -> -0.5, VMA 1.0 -> 0, VMA 1.5 -> +0.5
        score = _clip((val - 1.0) * 2)
        scores.append(score)
    return _mean_valid(scores)


def score_vwap(factors, direction):
    """VWAP0 < 1 = price above VWAP (bullish), > 1 = price below VWAP (bearish)."""
    val = factors.get('VWAP0')
    if val is None or np.isnan(val):
        return 0.0
    score = _clip((1.0 - val) * 40)
    if direction == 'sell':
        score = -score
    return score


# ── Group config ──

GROUPS = [
    ('Trend',         score_trend,          0.20),
    ('Momentum',      score_momentum,       0.15),
    ('PriceLevel',    score_price_level,    0.15),
    ('KBAR',          score_kbar,           0.10),
    ('Volatility',    score_volatility,     0.10),
    ('TrendStrength', score_trend_strength, 0.10),
    ('Volume',        score_volume,         0.10),
    ('VWAP',          score_vwap,           0.10),
]


def compute_confidence(factors, direction, prices=None):
    """Compute trade confidence from Alpha158 factors.

    Args:
        factors: dict of {factor_name: float} from Alpha158Engine.compute_current()
        direction: 'buy' or 'sell'
        prices: optional dict with 'close', 'ema20' etc (used for reason messages)

    Returns:
        dict with keys:
            confidence: float 0-100
            blocked: bool (True if < QLIB_MIN_CONFIDENCE)
            reason: str or None (explanation if blocked)
            details: {group_name: score}
    """
    if not factors:
        return {'confidence': 50.0, 'blocked': False, 'reason': None,
                'details': {}}

    votes = {}   # group_name -> group_score
    total_w = 0.0
    weighted_sum = 0.0

    for name, scorer, weight in GROUPS:
        score = scorer(factors, direction)
        votes[name] = round(score, 4)
        if not np.isnan(score):
            weighted_sum += score * weight
            total_w += weight

    raw_score = weighted_sum / total_w if total_w > 0 else 0.0
    # raw_score is in [-1, 1], map to [0, 100]
    confidence = (raw_score + 1.0) * 50.0
    confidence = float(np.clip(confidence, 0.0, 100.0))

    # Determine which groups voted against
    min_score = min(votes.values()) if votes else 0
    blocked = confidence < 40.0

    reason = None
    if blocked and votes:
        # Find the worst group
        worst_group = min(votes, key=votes.get)
        worst_score = votes[worst_group]
        reason = f'{worst_group}={worst_score:.2f}'

    return {
        'confidence': round(confidence, 1),
        'blocked': blocked,
        'reason': reason,
        'details': votes,
    }
