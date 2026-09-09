#
# Collects free Cboe market data and stores it in an HDF5 file:
#
#   /indices/<SYM>        daily OHLC of VIX/VVIX index history (1990 ->)
#   /futures/<ROOT>/<expiry>  per-contract VIX futures OHLC/settle/volume/OI
#   /chains/<SYM>         daily snapshots of the full delayed option chain
#
# Sources (all free, no key required):
#   https://cdn.cboe.com/api/global/us_indices/daily_prices/<SYM>_History.csv
#   https://www-api.cboe.com/us/futures/market_statistics/historical_data/product/list/<ROOT>/
#   https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/<ROOT>/<ROOT>_<expiry>.csv
#   https://cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json
#
# The option chain is a live snapshot only, so run this daily to build
# EOD history. Index and futures files carry their own history, so they
# are only refreshed (idempotent).
#
import sys
import io
import time
import datetime as dt

import requests
import pandas as pd
import yaml

CONFIG = 'cboe_instruments.yaml'
HEADERS = {'User-Agent': 'Mozilla/5.0 (lvvd cboe data collector)'}

CDN = 'https://cdn.cboe.com'
INDICES = f'{CDN}/api/global/us_indices/daily_prices'
FUT_LIST = ('https://www-api.cboe.com/us/futures/market_statistics/'
            'historical_data/product/list')
FUT_FILE = ('https://cdn.cboe.com/data/us/futures/market_statistics/'
            'historical_data')
QUOTES = f'{CDN}/api/global/delayed_quotes/options'


def load_config(path=CONFIG):
    with open(path) as f:
        return yaml.safe_load(f)


def append(store, key, add):
    '''Append rows to store[key], deduplicating on the index.'''
    if len(add) == 0:
        return 0
    if '/' + key.strip('/') in store.keys():
        old = store[key]
        add = add[~add.index.isin(old.index)]
        if len(add) == 0:
            return 0
        store[key] = pd.concat((old, add)).sort_index()
    else:
        store[key] = add.sort_index()
    return len(add)


#
# indices
#
def collect_index(store, symbol, filename, sleep):
    r = requests.get(f'{INDICES}/{filename}', headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df['DATE'] = pd.to_datetime(df['DATE'])
    df = df.set_index('DATE').rename(columns=str.upper)
    n = append(store, f'indices/{symbol}', df)
    time.sleep(sleep)
    return n


#
# futures: mirror every contract file that is new or still trading
#
def collect_futures(store, root, sleep, today=None):
    today = today or dt.date.today()
    r = requests.get(f'{FUT_LIST}/{root}/', headers=HEADERS, timeout=30)
    r.raise_for_status()
    contracts = [c for year in r.json().values() for c in year]

    added = 0
    for c in contracts:
        expiry = dt.date.fromisoformat(c['expire_date'])
        key = f'futures/{root}/{c["expire_date"]}'
        have = '/' + key in store.keys()
        # skip long-expired contracts already stored (files are final)
        if have and expiry < today - dt.timedelta(days=40):
            continue
        if have:
            last = store[key].index.get_level_values(0).max().date()
            if last >= expiry - dt.timedelta(days=5):
                continue  # contract finished trading
        fr = requests.get(f'{CDN}/{c["path"]}', headers=HEADERS, timeout=30)
        fr.raise_for_status()
        df = pd.read_csv(io.StringIO(fr.text))
        df['Trade Date'] = pd.to_datetime(df['Trade Date'])
        df = df.set_index(['Trade Date', 'Futures'])
        added += append(store, key, df)
        time.sleep(sleep)
    return added


#
# option chain snapshot
#
def parse_option_symbol(sym):
    '''VIX260916C00010000 -> (expiry YYMMDD, 'C', strike 100.0)'''
    tail = sym[-8:]
    strike = float(tail) / 1000
    cp = sym[-9]
    expiry = sym[-15:-9]
    return expiry, cp, strike


def collect_chain(store, symbol, sleep):
    r = requests.get(f'{QUOTES}/{symbol}.json', headers=HEADERS, timeout=30)
    r.raise_for_status()
    raw = r.json()
    snap_date = pd.Timestamp(raw['timestamp']).normalize()
    opts = pd.DataFrame(raw['data']['options'])
    if not len(opts):
        return 0
    meta = opts['option'].apply(parse_option_symbol)
    opts['Expiry'] = [m[0] for m in meta]
    opts['Type'] = [m[1] for m in meta]
    opts['Strike'] = [m[2] for m in meta]
    opts['Snapshot date'] = snap_date
    cols = ['option', 'Expiry', 'Type', 'Strike', 'Snapshot date',
            'bid', 'ask', 'iv', 'open_interest', 'volume', 'delta',
            'gamma', 'vega', 'theta', 'rho', 'theo', 'last_trade_price']
    cols = [c for c in cols if c in opts.columns]
    df = opts[cols].set_index(['Snapshot date', 'option'])
    time.sleep(sleep)
    return append(store, f'chains/{symbol.strip("_")}', df)


def collect(config_path=CONFIG):
    cfg = load_config(config_path)
    sleep = cfg.get('time_sleep', 0.5)
    store = pd.HDFStore(cfg['store'], 'a')
    try:
        for sym, fname in cfg.get('indices', {}).items():
            print(f'{sym:>5}: {collect_index(store, sym, fname, sleep)} '
                  f'new index rows')
        for root in cfg.get('futures', []):
            print(f'{root:>5}: {collect_futures(store, root, sleep)} '
                  f'new futures rows')
        for sym in cfg.get('chains', []):
            print(f'{sym:>5}: {collect_chain(store, sym, sleep)} '
                  f'chain quotes snapshotted')
    finally:
        store.close()


if __name__ == '__main__':
    collect(*(sys.argv[1:2] or []))
