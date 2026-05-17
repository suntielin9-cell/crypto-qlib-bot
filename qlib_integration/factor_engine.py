"""Alpha158 factor computation engine — pure pandas/numpy, no Qlib init needed.

Computes all 158 Alpha158 factors from OHLCV data:
  - 9 kbar factors (candlestick patterns)
  - 4 price factors (normalized OHLC)
  - 145 rolling factors (29 operators x 5 windows: 5,10,20,30,60)

Usage:
    engine = Alpha158Engine()
    factors = engine.compute_current(klines)
    # factors is a dict of {factor_name: float} for the latest candle
"""
import numpy as np
import pandas as pd


ROLLING_WINDOWS = [5, 10, 20, 30, 60]

# 29 rolling operator names
ROLLING_OPS = [
    'ROC', 'MA', 'STD', 'BETA', 'RSQR', 'RESI',
    'MAX', 'MIN', 'QTLU', 'QTLD', 'RANK', 'RSV',
    'IMAX', 'IMIN', 'IMXD',
    'CORR', 'CORD',
    'CNTP', 'CNTN', 'CNTD',
    'SUMP', 'SUMN', 'SUMD',
    'VMA', 'VSTD', 'WVMA',
    'VSUMP', 'VSUMN', 'VSUMD',
]


EPS = 1e-12


def _parse_klines(klines):
    """Convert raw Binance klines list to OHLCV DataFrame.

    Binance format: [time, open, high, low, close, volume, ...]
    """
    arr = np.array([(k[0], float(k[1]), float(k[2]), float(k[3]),
                     float(k[4]), float(k[5])) for k in klines],
                   dtype=[('time', 'i8'), ('open', 'f8'), ('high', 'f8'),
                          ('low', 'f8'), ('close', 'f8'), ('volume', 'f8')])
    df = pd.DataFrame(arr)
    df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3) / (df['volume'] + EPS)
    return df


def _ema(data, period):
    """Simple EMA (same formula used by auto_trader.py)."""
    m = 2 / (period + 1)
    r = [data[0]]
    for i in range(1, len(data)):
        r.append((data[i] - r[-1]) * m + r[-1])
    return np.array(r)


class Alpha158Engine:
    """Computes 158 Alpha158 factors from Binance kline data."""

    def __init__(self, min_history=60):
        self.min_history = min_history

    def compute_factors(self, klines):
        """Compute all 158 factors from raw klines.

        Returns: pd.DataFrame with 158 factor columns + 'close' for reference.
                 Index is the kline timestamps.
        """
        df = _parse_klines(klines)
        n = len(df)

        o = df['open'].values
        h = df['high'].values
        l = df['low'].values
        c = df['close'].values
        v = df['volume'].values
        vwap = df['vwap'].values

        # Collect all factor columns in a dict, build DataFrame once at end
        # to avoid pandas PerformanceWarning from 150+ individual column inserts.
        cols = {}

        # ── Kbar factors (9) ──
        klen = (h - l) / (o + EPS)
        kmid = (c - o) / (o + EPS)
        cols['KMID'] = kmid
        cols['KLEN'] = klen
        cols['KMID2'] = (c - o) / (h - l + EPS)
        cols['KUP'] = (h - np.maximum(o, c)) / (o + EPS)
        cols['KUP2'] = (h - np.maximum(o, c)) / (h - l + EPS)
        cols['KLOW'] = (np.minimum(o, c) - l) / (o + EPS)
        cols['KLOW2'] = (np.minimum(o, c) - l) / (h - l + EPS)
        cols['KSFT'] = (2 * c - h - l) / (o + EPS)
        cols['KSFT2'] = (2 * c - h - l) / (h - l + EPS)

        # ── Price factors (4) ──
        cols['OPEN0'] = o / (c + EPS)
        cols['HIGH0'] = h / (c + EPS)
        cols['LOW0'] = l / (c + EPS)
        cols['VWAP0'] = vwap / (c + EPS)

        # ── Helper arrays ──
        log_vol = np.log(v + 1)
        c_series = df['close']
        v_series = df['volume']

        # ── Rolling factors (145) ──
        for w in ROLLING_WINDOWS:
            if n < w:
                continue

            # -- Momentum --
            # ROC: Ref(close, w) / close
            cols[f'ROC{w}'] = c_series.shift(w) / (c + EPS)

            # MA: rolling mean / close
            cols[f'MA{w}'] = c_series.rolling(w).mean() / (c + EPS)

            # STD: rolling std / close
            cols[f'STD{w}'] = c_series.rolling(w).std() / (c + EPS)

            # -- Trend quality (BETA, RSQR, RESI) --
            # Use rolling apply with linear regression
            beta_arr = np.full(n, np.nan)
            rsqr_arr = np.full(n, np.nan)
            resi_arr = np.full(n, np.nan)
            if n >= w:
                x = np.arange(w, dtype=float)
                sx = x.sum()
                sx2 = (x ** 2).sum()
                for i in range(w - 1, n):
                    y = c[i - w + 1:i + 1]
                    if np.any(np.isnan(y)):
                        continue
                    sy = y.sum()
                    sxy = (x * y).sum()
                    slope = (w * sxy - sx * sy) / (w * sx2 - sx * sx + EPS)
                    intercept = (sy - slope * sx) / w
                    pred = slope * x + intercept
                    ss_res = ((y - pred) ** 2).sum()
                    ss_tot = ((y - y.mean()) ** 2).sum()
                    r2 = ss_res / (ss_tot + EPS)
                    beta_arr[i] = slope / (c[i] + EPS)
                    rsqr_arr[i] = 1.0 - r2
                    resi_arr[i] = (y[-1] - pred[-1]) / (c[i] + EPS)
            cols[f'BETA{w}'] = beta_arr
            cols[f'RSQR{w}'] = rsqr_arr
            cols[f'RESI{w}'] = resi_arr

            # -- Min/Max/Quantile --
            cols[f'MAX{w}'] = df['high'].rolling(w).max() / (c + EPS)
            cols[f'MIN{w}'] = df['low'].rolling(w).min() / (c + EPS)

            q80 = c_series.rolling(w).quantile(0.8)
            q20 = c_series.rolling(w).quantile(0.2)
            cols[f'QTLU{w}'] = q80 / (c + EPS)
            cols[f'QTLD{w}'] = q20 / (c + EPS)

            # -- Rank & stochastic --
            rank_arr = np.full(n, np.nan)
            rsv_arr = np.full(n, np.nan)
            imax_arr = np.full(n, np.nan)
            imin_arr = np.full(n, np.nan)
            for i in range(w - 1, n):
                window_c = c[i - w + 1:i + 1]
                window_h = h[i - w + 1:i + 1]
                window_l = l[i - w + 1:i + 1]
                if np.any(np.isnan(window_c)):
                    continue
                # RANK: percentile rank of current close in window
                rank_arr[i] = np.sum(window_c <= window_c[-1]) / w
                # RSV: (close - LL) / (HH - LL)
                hh = window_h.max()
                ll = window_l.min()
                rsv_arr[i] = (c[i] - ll) / (hh - ll + EPS)
                # IMAX: how many periods since highest high
                hh_idx = np.argmax(window_h)
                imax_arr[i] = (w - 1 - hh_idx) / w
                # IMIN: how many periods since lowest low
                ll_idx = np.argmin(window_l)
                imin_arr[i] = (w - 1 - ll_idx) / w
            cols[f'RANK{w}'] = rank_arr
            cols[f'RSV{w}'] = rsv_arr
            cols[f'IMAX{w}'] = imax_arr
            cols[f'IMIN{w}'] = imin_arr
            cols[f'IMXD{w}'] = (imax_arr - imin_arr) / w

            # -- Price-volume correlation --
            close_pct = c_series.pct_change()
            vol_pct = v_series.pct_change().apply(np.log1p)
            cols[f'CORR{w}'] = c_series.rolling(w).corr(pd.Series(log_vol))
            cols[f'CORD{w}'] = close_pct.rolling(w).corr(vol_pct)

            # -- Up/down counts --
            diff = c_series.diff()
            up = (diff > 0).astype(float)
            down = (diff < 0).astype(float)
            cntp = up.rolling(w).mean()
            cntn = down.rolling(w).mean()
            cols[f'CNTP{w}'] = cntp
            cols[f'CNTN{w}'] = cntn
            cols[f'CNTD{w}'] = cntp - cntn

            # -- Gain/loss sums (RSI-like) --
            gains = np.maximum(diff.values, 0)
            losses = -np.minimum(diff.values, 0)
            abs_change = np.abs(diff.values)
            sump_arr = np.full(n, np.nan)
            sumn_arr = np.full(n, np.nan)
            sumd_arr = np.full(n, np.nan)
            for i in range(w, n):
                sg = gains[i - w + 1:i + 1].sum()
                sl = losses[i - w + 1:i + 1].sum()
                sa = abs_change[i - w + 1:i + 1].sum() + EPS
                sump_arr[i] = sg / sa
                sumn_arr[i] = sl / sa
                sumd_arr[i] = (sg - sl) / sa
            cols[f'SUMP{w}'] = sump_arr
            cols[f'SUMN{w}'] = sumn_arr
            cols[f'SUMD{w}'] = sumd_arr

            # -- Volume factors --
            cols[f'VMA{w}'] = v_series.rolling(w).mean() / (v + EPS)
            cols[f'VSTD{w}'] = v_series.rolling(w).std() / (v + EPS)

            # WVMA: volume-weighted price change volatility
            pc = np.abs(close_pct.values) * v
            wvma = pd.Series(pc).rolling(w).mean() / (pd.Series(np.abs(close_pct.values) * v).rolling(w).mean() + EPS)
            cols[f'WVMA{w}'] = wvma

            # Volume up/down ratios
            v_diff = v_series.diff().values
            v_up = np.maximum(v_diff, 0)
            v_down = -np.minimum(v_diff, 0)
            v_abs = np.abs(v_diff)
            vsump_arr = np.full(n, np.nan)
            vsumn_arr = np.full(n, np.nan)
            for i in range(w, n):
                vu = v_up[i - w + 1:i + 1].sum()
                vd = v_down[i - w + 1:i + 1].sum()
                va = v_abs[i - w + 1:i + 1].sum() + EPS
                vsump_arr[i] = vu / va
                vsumn_arr[i] = vd / va
            cols[f'VSUMP{w}'] = vsump_arr
            cols[f'VSUMN{w}'] = vsumn_arr
            cols[f'VSUMD{w}'] = (pd.Series(v_up).rolling(w).sum() - pd.Series(v_down).rolling(w).sum()) / (pd.Series(v_abs).rolling(w).sum() + EPS)

        out = pd.DataFrame(cols, index=df.index)
        out['close'] = c
        return out

    def compute_current(self, klines):
        """Compute factors and return only the latest row as a dict.

        This is the primary method used by QlibValidator.
        """
        df = self.compute_factors(klines)
        if df.empty:
            return {}
        row = df.iloc[-1].to_dict()
        # Remove 'close' from factors (it's metadata, not a factor)
        row.pop('close', None)
        return row
