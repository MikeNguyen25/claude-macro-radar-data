#!/usr/bin/env python3
"""
claude-macro-radar-data · fetch_history.py

Monthly history (from 2017) for the markets and the US/world macro series that macro-radar-claude scores, so the app can be
backtested over 2018–2026 instead of only since 10/2024. Writes data/history.json. Standard library only (Python 3.10+).

Each series keeps the app's own convention:
  MKT.*            month-end close (Yahoo Finance monthly bars; crypto falls back to Binance monthly klines)
  US.cpi_yoy       CPI-U YoY % from CPIAUCSL          US.core_pce_yoy  core PCE YoY % from PCEPILFE
  US.unemp         UNRATE %                           US.payrolls      monthly change of PAYEMS, thousand
  US.fed_funds     FEDFUNDS, monthly average %        US.us10y         GS10, monthly average %
  US.gdp_qoq       real GDP q/q annualised %, at the quarter's last month (A191RL1Q225SBEA)
  US.vix           VIX month-end close (VIXCLS)       US.dxy           ICE dollar index month-end close (Yahoo DX-Y.NYB)
  WORLD.brent      Brent front-month month-end close (Yahoo BZ=F)
  WORLD.ecb_depo   ECB deposit rate at month-end (ECBDFR)
  CN.usdcny        CNY per USD at month-end (DEXCHUS)
Only finished months are written. data/history_probe.json records what each source returned.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'history.json')
PROBE = os.path.join(ROOT, 'data', 'history_probe.json')
UA = 'claude-macro-radar-data/1.0 (GitHub Actions; monthly history for a personal dashboard; github.com/MikeNguyen25/claude-macro-radar-data)'
START = dt.date(2017, 1, 1)
NOW = dt.datetime.now(dt.timezone.utc)
CUR_M = NOW.strftime('%Y-%m')   # unfinished month, never written


def get(url, accept='*/*', tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code in (400, 401, 403, 404, 451):
                break
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
        time.sleep(3 * (i + 1))
    raise RuntimeError(f'{url} -> {last}')


def ym(d):
    return d.strftime('%Y-%m')


# ---------- FRED ----------
def fred(sid):
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={START.isoformat()}'
    st, raw = get(url, 'text/csv')
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8'))))
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in ('', '.'):
            continue
        try:
            out.append((dt.date.fromisoformat(r[0]), float(r[1])))
        except ValueError:
            continue
    return out, url


def monthly_last(obs):
    m = {}
    for d, v in obs:
        m[ym(d)] = v          # observations are in date order, so the last one of each month wins
    return m


def monthly_first_of(obs):   # monthly FRED series are dated on the 1st of the month
    return {ym(d): v for d, v in obs}


def yoy(m):
    out = {}
    for k, v in m.items():
        y, mo = map(int, k.split('-'))
        k0 = f'{y - 1:04d}-{mo:02d}'
        if k0 in m and m[k0]:
            out[k] = round((v / m[k0] - 1) * 100, 2)
    return out


def diff(m):
    ks = sorted(m)
    return {k: round(m[k] - m[p], 1) for p, k in zip(ks, ks[1:])}


def quarter_end(m):
    out = {}
    for k, v in m.items():
        y, mo = map(int, k.split('-'))
        out[f'{y:04d}-{mo + 2:02d}'] = v
    return out


# ---------- Yahoo / Binance ----------
def yahoo(sym):
    p1 = int(dt.datetime(START.year, START.month, 1, tzinfo=dt.timezone.utc).timestamp())
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}?period1={p1}&period2={int(NOW.timestamp())}&interval=1mo&includeAdjustedClose=false'
    st, raw = get(url, 'application/json')
    j = json.loads(raw)
    res = j['chart']['result'][0]
    ts, close = res['timestamp'], res['indicators']['quote'][0]['close']
    out = {}
    for t, c in zip(ts, close):
        if c is None:
            continue
        # monthly bars are stamped at the first trading day of the month (exchange time); use the calendar month of that stamp + offset
        d = dt.datetime.fromtimestamp(t + res.get('meta', {}).get('gmtoffset', 0), dt.timezone.utc).date()
        out[ym(d)] = float(c)
    return out, url.split('?')[0]


def binance(symbol):
    url = f'https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1M&limit=1000'
    st, raw = get(url, 'application/json')
    out = {}
    for k in json.loads(raw):
        d = dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).date()
        if d >= START:
            out[ym(d)] = float(k[4])
    return out, url.split('?')[0]


def finished(m):
    return {k: v for k, v in sorted(m.items()) if k < CUR_M and k >= ym(START)}


SPEC = [
    # id, fetcher, unit, note
    ('MKT.BTC', lambda: first_ok([lambda: yahoo('BTC-USD'), lambda: binance('BTCUSDT')]), 'USD', 'Giá đóng cửa cuối tháng'),
    ('MKT.SOL', lambda: first_ok([lambda: yahoo('SOL-USD'), lambda: binance('SOLUSDT')]), 'USD', 'Giá đóng cửa cuối tháng'),
    ('MKT.DOGE', lambda: first_ok([lambda: yahoo('DOGE-USD'), lambda: binance('DOGEUSDT')]), 'USD', 'Giá đóng cửa cuối tháng'),
    ('MKT.TRX', lambda: first_ok([lambda: yahoo('TRX-USD'), lambda: binance('TRXUSDT')]), 'USD', 'Giá đóng cửa cuối tháng'),
    ('MKT.SPX', lambda: yahoo('^GSPC'), 'điểm', 'S&P 500 đóng cửa cuối tháng'),
    ('MKT.NDX', lambda: yahoo('^IXIC'), 'điểm', 'Nasdaq Composite đóng cửa cuối tháng'),
    ('MKT.STI', lambda: yahoo('^STI'), 'điểm', 'STI đóng cửa cuối tháng'),
    ('MKT.SET', lambda: yahoo('^SET.BK'), 'điểm', 'SET Index đóng cửa cuối tháng'),
    ('MKT.VNI', lambda: first_ok([lambda: yahoo('^VNINDEX.VN'), lambda: yahoo('VNINDEX.VN'), lambda: yahoo('^VNINDEX')]), 'điểm', 'VN-Index đóng cửa cuối tháng'),
    ('US.dxy', lambda: yahoo('DX-Y.NYB'), 'điểm', 'Chỉ số USD (ICE) đóng cửa cuối tháng'),
    ('WORLD.brent', lambda: yahoo('BZ=F'), 'USD/thùng', 'Brent kỳ hạn gần nhất, đóng cửa cuối tháng'),
    ('US.cpi_yoy', lambda: fred_tf('CPIAUCSL', lambda o: yoy(monthly_first_of(o))), '%', 'CPI-U so với cùng kỳ, tính từ CPIAUCSL'),
    ('US.core_pce_yoy', lambda: fred_tf('PCEPILFE', lambda o: yoy(monthly_first_of(o))), '%', 'PCE lõi so với cùng kỳ, tính từ PCEPILFE'),
    ('US.unemp', lambda: fred_tf('UNRATE', monthly_first_of), '%', 'Tỷ lệ thất nghiệp (UNRATE)'),
    ('US.payrolls', lambda: fred_tf('PAYEMS', lambda o: diff(monthly_first_of(o))), 'nghìn', 'Việc làm phi nông nghiệp tăng trong tháng, từ PAYEMS'),
    ('US.fed_funds', lambda: fred_tf('FEDFUNDS', monthly_first_of), '%', 'Lãi suất quỹ liên bang hiệu dụng, bình quân tháng'),
    ('US.us10y', lambda: fred_tf('GS10', monthly_first_of), '%', 'Lợi suất trái phiếu 10 năm, bình quân tháng (GS10)'),
    ('US.gdp_qoq', lambda: fred_tf('A191RL1Q225SBEA', lambda o: quarter_end(monthly_first_of(o))), '%', 'GDP thực quý so với quý trước, năm hóa; đặt ở tháng cuối quý'),
    ('US.vix', lambda: fred_tf('VIXCLS', monthly_last), 'điểm', 'VIX đóng cửa ngày giao dịch cuối tháng'),
    ('WORLD.ecb_depo', lambda: fred_tf('ECBDFR', monthly_last), '%', 'Lãi suất tiền gửi ECB cuối tháng'),
    ('CN.usdcny', lambda: fred_tf('DEXCHUS', monthly_last), 'CNY', 'Tỷ giá CNY/USD cuối tháng (DEXCHUS)'),
]


def fred_tf(sid, tf):
    obs, url = fred(sid)
    return tf(obs), url


def first_ok(fns):
    errs = []
    for f in fns:
        try:
            m, url = f()
            if m:
                return m, url
        except Exception as e:
            errs.append(str(e)[:200])
    raise RuntimeError(' | '.join(errs))


def main():
    old = {}
    try:
        with open(OUT, encoding='utf-8') as f:
            old = json.load(f).get('series') or {}
    except Exception:
        pass
    series, probe, errors = {}, {'ranAt': NOW.isoformat(timespec='seconds')}, []
    for sid, fn, unit, note in SPEC:
        try:
            m, url = fn()
            pts = finished(m)
            if not pts:
                raise RuntimeError('no points')
            series[sid] = {'unit': unit, 'note': note, 'source': url, 'points': [[k, round(v, 6)] for k, v in pts.items()]}
            probe[sid] = {'ok': True, 'n': len(pts), 'first': next(iter(pts)), 'last': list(pts)[-1], 'source': url}
        except Exception as e:
            errors.append({'id': sid, 'error': str(e)[:400]})
            probe[sid] = {'ok': False, 'error': str(e)[:400]}
            if sid in old:
                series[sid] = old[sid]     # keep the last good copy
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'schema': 1, 'generatedAt': NOW.isoformat(timespec='seconds'), 'series': series, 'errors': errors}, f, ensure_ascii=False, separators=(',', ':'))
    with open(PROBE, 'w', encoding='utf-8') as f:
        json.dump(probe, f, ensure_ascii=False, indent=1)
    print(json.dumps({'ok': sorted(k for k, v in probe.items() if isinstance(v, dict) and v.get('ok')), 'errors': errors}, ensure_ascii=False))
    return 1 if len(errors) == len(SPEC) else 0


if __name__ == '__main__':
    sys.exit(main())
