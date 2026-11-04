#!/usr/bin/env python3
"""
claude-macro-radar-data · fetch_sol_daily.py

Pulls daily Solana metrics from public APIs and merges them into data/sol_daily.json, the daily feed of the
macro-radar-claude app (Solana page). Runs in GitHub Actions every 3 hours (.github/workflows/sol-daily.yml).
Python 3.10+, standard library only.

Series (ids match the app; one value per finished UTC day):
  SOL.w_dex       DEX spot volume                         USD tỷ/ngày    DefiLlama dexs overview, chain Solana
  SOL.w_rev       REV = chain fees + MEV tips             USD triệu/ngày DefiLlama fees overview (categories Chain + MEV)
  SOL.w_app_rev   App revenue (all other protocols)       USD triệu/ngày DefiLlama fees overview, dataType=dailyRevenue
  SOL.w_app_fees  App fees (all other protocols)          USD triệu/ngày DefiLlama fees overview, dataType=dailyFees
  SOL.w_etf       US spot SOL ETF net flow (trading days) USD triệu/ngày Farside Investors
  SOL.d_tvl       DeFi TVL (DefiLlama default method)     USD tỷ         DefiLlama historicalChainTvl
  SOL.d_stables   Stablecoin market cap on Solana         USD tỷ         DefiLlama stablecoins

Rules
- Only finished UTC days go into `points`; today's partial value goes to `latest` with the time it was read.
- Points already in the file are kept. Recent days are overwritten with the newest numbers, because sources revise
  them for a few days.
- A source that fails leaves its series unchanged and is listed in `errors`. The script never deletes data.
- data/probe.json records what each endpoint returned (sizes, keys, samples, category totals) so the definitions
  can be checked against the numbers DefiLlama shows on https://defillama.com/chain/solana.
"""
import datetime as dt
import gzip
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'sol_daily.json')
LATEST = os.path.join(ROOT, 'data', 'sol_latest.json')
PROBE = os.path.join(ROOT, 'data', 'probe.json')
UA = 'claude-macro-radar-data/1.0 (GitHub Actions; daily Solana metrics for a personal dashboard; github.com/MikeNguyen25/claude-macro-radar-data)'
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
# categories that are not "apps": the chain's own fees and MEV tips (both go into REV)
NON_APP = {'Chain', 'MEV'}
# NYSE full-day closures: a US ETF records no flow, so these weekdays count as 0
NYSE_HOLIDAYS = {
    '2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18', '2025-05-26', '2025-06-19', '2025-07-04',
    '2025-09-01', '2025-11-27', '2025-12-25',
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
    '2026-11-26', '2026-12-25',
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06',
    '2027-11-25', '2027-12-24',
}
MONTHS = {m: i + 1 for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}

SERIES_META = {
    'SOL.w_dex': dict(unit='USD tỷ', kind='flow', source='DefiLlama', url='https://defillama.com/dexs/chain/solana',
                      note='Khối lượng giao dịch spot trên các DEX của Solana trong từng ngày UTC (DefiLlama, tổng các sàn DEX).'),
    'SOL.w_rev': dict(unit='USD triệu', kind='flow', source='DefiLlama', url='https://defillama.com/chain/solana',
                      note='REV theo ngày UTC: phí giao dịch của chain (phí cơ bản + phí ưu tiên) cộng tip MEV, theo DefiLlama (nhóm Chain + MEV).'),
    'SOL.w_app_rev': dict(unit='USD triệu', kind='flow', source='DefiLlama', url='https://defillama.com/chain/solana',
                          note='Doanh thu của các ứng dụng trên Solana trong từng ngày UTC (DefiLlama dailyRevenue, không gồm phí chain và tip MEV).'),
    'SOL.w_app_fees': dict(unit='USD triệu', kind='flow', source='DefiLlama', url='https://defillama.com/chain/solana',
                           note='Phí người dùng trả cho các ứng dụng trên Solana trong từng ngày UTC (DefiLlama dailyFees, không gồm phí chain và tip MEV).'),
    'SOL.w_etf': dict(unit='USD triệu', kind='flow', calendar='weekdays', source='Farside Investors', url='https://farside.co.uk/sol/',
                      note='Dòng tiền ròng hằng ngày của các ETF SOL giao ngay tại Mỹ (cột Total của Farside), chỉ ngày giao dịch; ngày sàn Mỹ nghỉ lễ ghi 0.'),
    'SOL.d_tvl': dict(unit='USD tỷ', kind='level', source='DefiLlama', url='https://defillama.com/chain/solana',
                      note='TVL DeFi trên Solana theo phương pháp mặc định của DefiLlama, số của từng ngày UTC.'),
    'SOL.d_stables': dict(unit='USD tỷ', kind='level', source='DefiLlama', url='https://defillama.com/stablecoins/Solana',
                          note='Tổng vốn hóa stablecoin đang lưu hành trên Solana (mọi loại neo giá, quy ra USD), số của từng ngày UTC.'),
}
SCALE = {'USD tỷ': 1e9, 'USD triệu': 1e6}
DIGITS = {'USD tỷ': 4, 'USD triệu': 3}


def day_of(ts):
    return dt.datetime.fromtimestamp(int(float(ts)), dt.timezone.utc).date().isoformat()


def fetch(url, accept='application/json', tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept, 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if (r.headers.get('Content-Encoding') or '').lower() == 'gzip':
                    raw = gzip.decompress(raw)
                return r.status, raw
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code in (400, 401, 403, 404, 410):
                break
        except Exception as e:  # network errors, timeouts
            last = f'{type(e).__name__}: {e}'
        time.sleep(4 * (i + 1))
    raise RuntimeError(f'{url} -> {last}')


def fetch_json(url):
    st, raw = fetch(url)
    return st, len(raw), json.loads(raw.decode('utf-8'))


def finished(pts):
    """Drop today's (unfinished) UTC day."""
    return {d: v for d, v in pts.items() if d < TODAY}


# ---------- DefiLlama ----------
def dex(probe):
    url = 'https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=false&excludeTotalDataChartBreakdown=true&dataType=dailyVolume'
    st, n, j = fetch_json(url)
    chart = j.get('totalDataChart') or []
    pts = {day_of(ts): float(v) for ts, v in chart if v is not None}
    fin = finished(pts)
    last7 = sorted(fin)[-7:]
    probe['dex'] = dict(url=url, status=st, bytes=n, keys=sorted(j)[:60], points=len(chart), first=chart[:2], last=chart[-3:],
                        total24h=j.get('total24h'), total48hto24h=j.get('total48hto24h'), total7d=j.get('total7d'), total30d=j.get('total30d'),
                        sum_last7_finished=sum(fin[d] for d in last7), last7_days=last7)
    return {'SOL.w_dex': pts}


def fees_overview(dtype, probe):
    url = f'https://api.llama.fi/overview/fees/solana?excludeTotalDataChart=false&excludeTotalDataChartBreakdown=false&dataType={dtype}'
    st, n, j = fetch_json(url)
    protos = j.get('protocols') or []
    cat = {}
    for p in protos:
        for k in ('name', 'displayName', 'module', 'slug', 'defillamaId'):
            if p.get(k) is not None:
                cat[str(p[k])] = p.get('category') or ''
    total = {day_of(ts): float(v) for ts, v in (j.get('totalDataChart') or []) if v is not None}
    buckets = {}  # day -> {chain, mev, app, unknown}
    unknown_names = {}
    for row in (j.get('totalDataChartBreakdown') or []):
        if not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[1], dict):
            continue
        d = day_of(row[0])
        b = buckets.setdefault(d, {'chain': 0.0, 'mev': 0.0, 'app': 0.0, 'unknown': 0.0})
        for name, v in row[1].items():
            if not isinstance(v, (int, float)):
                continue
            c = cat.get(str(name))
            if c is None:
                b['unknown'] += v
                unknown_names[name] = unknown_names.get(name, 0) + v
            elif c == 'Chain':
                b['chain'] += v
            elif c == 'MEV':
                b['mev'] += v
            else:
                b['app'] += v
    days = sorted(buckets)
    lastd = days[-1] if days else None
    top = []
    if j.get('totalDataChartBreakdown'):
        last_row = j['totalDataChartBreakdown'][-2] if len(j['totalDataChartBreakdown']) > 1 else j['totalDataChartBreakdown'][-1]
        if isinstance(last_row, (list, tuple)) and len(last_row) > 1 and isinstance(last_row[1], dict):
            top = sorted(((k, v, cat.get(str(k))) for k, v in last_row[1].items() if isinstance(v, (int, float))), key=lambda x: -x[1])[:25]
    cats = {}
    for p in protos:
        cats[p.get('category') or ''] = cats.get(p.get('category') or '', 0) + 1
    probe[f'fees_{dtype}'] = dict(url=url, status=st, bytes=n, keys=sorted(j)[:60], protocols=len(protos), categories=cats,
                                  breakdown_rows=len(j.get('totalDataChartBreakdown') or []), last_day=lastd,
                                  recent={d: buckets[d] for d in days[-12:]}, total_recent={d: total.get(d) for d in days[-12:]},
                                  top_protocols_day_before_last=top,
                                  unknown_top=sorted(unknown_names.items(), key=lambda x: -x[1])[:15],
                                  total24h=j.get('total24h'), total7d=j.get('total7d'),
                                  sample_protocol={k: protos[0].get(k) for k in list(protos[0])[:25]} if protos else None)
    return buckets, total


def chain_summary(dtype, probe):
    """Solana's own fees from the protocol summary (fallback when the overview has no 'Chain' row)."""
    url = f'https://api.llama.fi/summary/fees/solana?dataType={dtype}'
    st, n, j = fetch_json(url)
    chart = j.get('totalDataChart') or []
    probe[f'chain_summary_{dtype}'] = dict(url=url, status=st, bytes=n, keys=sorted(j)[:60], points=len(chart), last=chart[-3:],
                                          total24h=j.get('total24h'), category=j.get('category'), name=j.get('name'))
    return {day_of(ts): float(v) for ts, v in chart if v is not None}


def fees(probe):
    out = {}
    fb, ftot = fees_overview('dailyFees', probe)
    rb, rtot = fees_overview('dailyRevenue', probe)
    chain = {d: b['chain'] for d, b in fb.items()}
    if not any(chain.values()):
        try:
            chain = chain_summary('dailyFees', probe)
        except Exception as e:
            probe['chain_summary_error'] = str(e)
    mev = {d: b['mev'] for d, b in fb.items()}
    days = sorted(set(chain) | set(mev))
    out['SOL.w_rev'] = {d: chain.get(d, 0.0) + mev.get(d, 0.0) for d in days if d in chain}
    out['SOL.w_app_fees'] = {d: b['app'] + b['unknown'] for d, b in fb.items()}
    out['SOL.w_app_rev'] = {d: b['app'] + b['unknown'] for d, b in rb.items()}
    probe['rev_components_recent'] = {d: {'chain_fees': chain.get(d), 'mev_tips': mev.get(d)} for d in days[-12:]}
    return out


def tvl(probe):
    url = 'https://api.llama.fi/v2/historicalChainTvl/Solana'
    st, n, j = fetch_json(url)
    pts = {day_of(x['date']): float(x['tvl']) for x in j if isinstance(x, dict) and x.get('tvl') is not None}
    probe['tvl'] = dict(url=url, status=st, bytes=n, points=len(j), first=j[:2], last=j[-3:])
    return {'SOL.d_tvl': pts}


def stables(probe):
    url = 'https://stablecoins.llama.fi/stablecoincharts/Solana'
    st, n, j = fetch_json(url)
    pts = {}
    for x in j:
        if not isinstance(x, dict):
            continue
        c = x.get('totalCirculatingUSD') or {}
        tot = sum(v for v in c.values() if isinstance(v, (int, float)))
        if tot > 0:
            pts[day_of(x['date'])] = float(tot)
    probe['stables'] = dict(url=url, status=st, bytes=n, points=len(j), first_keys=sorted(j[0]) if j else None, last=j[-2:] if j else None)
    return {'SOL.d_stables': pts}


def sol_price(probe):
    url = 'https://coins.llama.fi/prices/current/coingecko:solana'
    st, n, j = fetch_json(url)
    c = (j.get('coins') or {}).get('coingecko:solana') or {}
    probe['price'] = dict(url=url, status=st, coin=c)
    return c.get('price'), c.get('timestamp')


def page_probe(probe):
    """What the chain page itself is built from (helps match DefiLlama's own REV / App Revenue numbers)."""
    url = 'https://defillama.com/chain/solana'
    try:
        st, raw = fetch(url, accept='text/html')
    except Exception as e:
        probe['page'] = dict(url=url, error=str(e))
        return
    t = raw.decode('utf-8', 'replace')
    info = dict(url=url, status=st, bytes=len(raw))
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', t, re.S)
    if m:
        try:
            nd = json.loads(m.group(1))
            pp = (nd.get('props') or {}).get('pageProps') or {}
            info['buildId'] = nd.get('buildId')
            info['pageProps'] = {k: (type(v).__name__, len(v) if hasattr(v, '__len__') else v) for k, v in list(pp.items())[:80]}
        except Exception as e:
            info['next_data_error'] = str(e)
    for label in ('Chain REV', 'App Revenue', 'App Fees', 'Chain Fees', 'Active Addresses', 'Transactions', 'DEXs Volume'):
        i = t.find(label)
        info[f'near:{label}'] = re.sub(r'<[^>]+>', ' ', t[i:i + 400])[:200] if i >= 0 else None
    info['api_urls'] = sorted(set(re.findall(r'https://[a-z0-9.-]*llama\.fi/[A-Za-z0-9_/$.?=&%-]+', t)))[:60]
    probe['page'] = info


# ---------- Farside ----------
class Rows(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.row is not None and self.in_cell:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.in_cell = False
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def farside_num(s):
    s = s.replace(',', '').strip()
    if s in ('', '-', '–', '—'):
        return None
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def etf(probe):
    url = 'https://farside.co.uk/sol/'
    st, raw = fetch(url, accept='text/html')
    p = Rows()
    p.feed(raw.decode('utf-8', 'replace'))
    pts, header = {}, None
    for cells in p.rows:
        if cells and cells[-1].strip().lower() == 'total' and header is None:
            header = cells
        m = re.match(r'^(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})$', cells[0].strip()) if cells else None
        if not m or m.group(2) not in MONTHS:
            continue
        d = dt.date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()
        v = farside_num(cells[-1])
        if v is not None:
            pts[d] = v * 1e6   # Farside shows US$ million; keep raw USD like the other sources
    if pts:
        lo, hi = min(pts), max(pts)
        for h in NYSE_HOLIDAYS:
            if lo <= h <= hi and h not in pts:
                pts[h] = 0.0
    probe['etf'] = dict(url=url, status=st, bytes=len(raw), rows=len(p.rows), header=header, dated=len(pts),
                        first=min(pts) if pts else None, last=max(pts) if pts else None, sample=sorted(pts.items())[-5:])
    return {'SOL.w_etf': pts}


# ---------- merge & write ----------
def load(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def main():
    old = load(OUT, {'schema': 1, 'series': {}})
    probe = {'ranAt': NOW.isoformat(timespec='seconds'), 'today_utc': TODAY}
    fresh, errors, latest = {}, [], {}
    for name, fn in (('defillama_dexs', dex), ('defillama_fees', fees), ('defillama_tvl', tvl),
                     ('defillama_stablecoins', stables), ('farside_etf', etf)):
        try:
            for sid, pts in fn(probe).items():
                fresh[sid] = pts
        except Exception as e:
            errors.append({'source': name, 'error': str(e)[:500]})
    try:
        price, pts_ts = sol_price(probe)
        latest['SOL.price'] = {'value': price, 'time': dt.datetime.fromtimestamp(pts_ts, dt.timezone.utc).isoformat(timespec='seconds') if pts_ts else None, 'source': 'DefiLlama coins (CoinGecko id)'}
    except Exception as e:
        errors.append({'source': 'defillama_coins', 'error': str(e)[:300]})
    try:
        page_probe(probe)
    except Exception as e:
        probe['page_error'] = str(e)[:300]

    series = old.get('series') or {}
    for sid, meta in SERIES_META.items():
        prev = series.get(sid) or {}
        pts = {d: v for d, v in (prev.get('points') or [])}
        new = fresh.get(sid)
        if new:
            sc, dg = SCALE[meta['unit']], DIGITS[meta['unit']]
            live = {d: v for d, v in new.items() if d >= TODAY}
            for d, v in finished(new).items():
                pts[d] = round(v / sc, dg)
            if live:
                d = max(live)
                latest[sid] = {'date': d, 'value': round(live[d] / sc, dg), 'partial': True, 'readAt': NOW.isoformat(timespec='seconds')}
        if not pts:
            continue
        doc = dict(meta)
        doc['points'] = sorted([d, v] for d, v in pts.items())
        doc['first'], doc['last'] = doc['points'][0][0], doc['points'][-1][0]
        doc['updatedAt'] = NOW.isoformat(timespec='seconds') if new else prev.get('updatedAt')
        series[sid] = doc
        latest.setdefault(sid, {'date': doc['last'], 'value': doc['points'][-1][1]})

    out = {'schema': 1, 'generatedAt': NOW.isoformat(timespec='seconds'), 'series': series, 'errors': errors}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    with open(LATEST, 'w', encoding='utf-8') as f:
        json.dump({'generatedAt': out['generatedAt'], 'latest': latest, 'errors': errors}, f, ensure_ascii=False, indent=1)
    with open(PROBE, 'w', encoding='utf-8') as f:
        json.dump(probe, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({'generatedAt': out['generatedAt'], 'errors': errors,
                      'series': {k: (v['first'], v['last'], len(v['points'])) for k, v in series.items()}}, ensure_ascii=False))
    # fail the run only when every source failed, so a partial outage still commits what worked
    return 1 if len(errors) >= 5 else 0


if __name__ == '__main__':
    sys.exit(main())
