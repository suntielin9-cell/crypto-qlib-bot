"""QlibValidator — main integration class for auto_trader.py.

Provides validate() method that wraps Alpha158 factor computation + confidence
scoring into a single call for the trading bot's main loop.
"""
from datetime import datetime

from .factor_engine import Alpha158Engine
from .scoring import compute_confidence


class QlibValidator:
    """Validates trading signals using Qlib Alpha158 factor analysis."""

    def __init__(self, config=None):
        self.engine = Alpha158Engine(min_history=60)
        self.config = config or {}
        self.stats = {
            'total_validations': 0,
            'trades_confirmed': 0,
            'trades_blocked': 0,
            'errors': 0,
        }
        self._last_validation = None

    def validate(self, client, symbol, direction, klines, prices=None):
        """Run full Qlib factor analysis on a trade signal.

        Args:
            client: Binance client (unused currently, kept for future expansion)
            symbol: Trading pair symbol e.g. 'BTCUSDT'
            direction: 'buy' or 'sell'
            klines: Raw 4H klines from Binance (list of lists)
            prices: Optional dict with close/ema20/ema50/ema200/rsi/volume/avg_vol

        Returns:
            dict: {
                'confidence': float 0-100,
                'blocked': bool,
                'reason': str or None,
                'factor_count': int,
                'timestamp': str,
                'details': {group_name: score},
            }
        """
        self.stats['total_validations'] += 1

        if not klines or len(klines) < 60:
            self.stats['errors'] += 1
            return {
                'confidence': 50.0,
                'blocked': False,
                'reason': 'insufficient_data',
                'factor_count': 0,
                'timestamp': datetime.now().isoformat(),
                'details': {},
            }

        try:
            # Step 1: Compute Alpha158 factors
            factors = self.engine.compute_current(klines)
            if not factors:
                self.stats['errors'] += 1
                return {
                    'confidence': 50.0,
                    'blocked': False,
                    'reason': 'no_factors',
                    'factor_count': 0,
                    'timestamp': datetime.now().isoformat(),
                    'details': {},
                }

            # Step 2: Compute confidence score
            result = compute_confidence(factors, direction, prices)

            # Step 3: Update stats
            if result['blocked']:
                self.stats['trades_blocked'] += 1
            else:
                self.stats['trades_confirmed'] += 1

            self._last_validation = datetime.now().isoformat()

            return {
                'confidence': result['confidence'],
                'blocked': result['blocked'],
                'reason': result['reason'],
                'factor_count': len(factors),
                'timestamp': self._last_validation,
                'details': result['details'],
            }

        except Exception as e:
            self.stats['errors'] += 1
            return {
                'confidence': 50.0,
                'blocked': False,
                'reason': f'error:{str(e)[:80]}',
                'factor_count': 0,
                'timestamp': datetime.now().isoformat(),
                'details': {},
            }

    def get_stats(self):
        """Return validator statistics for logging."""
        return dict(self.stats)

    def reset_stats(self):
        """Reset daily statistics."""
        self.stats = {
            'total_validations': 0,
            'trades_confirmed': 0,
            'trades_blocked': 0,
            'errors': 0,
        }
