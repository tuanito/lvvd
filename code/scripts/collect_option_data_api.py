#
# Collects Eurex instrument quotes from the statistics JSON API and
# appends them to an HDF5 store. The instrument universe (futures and
# options, with their Eurex product ids) is configured in
# instruments.yaml.
#
# The API only serves roughly the last 21 trading days, so this script
# is meant to be run periodically (e.g. weekly) to accumulate history.
#
# Storage layout (keys in the HDF5 store):
#   /futures/<CODE>           one row per (pricing day, expiry)
#   /options/<CODE>/<MmmYY>   per-strike call/put settlement prices,
#                             same format as the book's
#                             index_option_series.h5 series
#
# Source: https://www.eurex.com/api/v1/overallstatistics/<product id>
#
import sys
import time
import datetime as dt

import requests
import pandas as pd
import yaml

CONFIG = 'instruments.yaml'
HEADERS = {'User-Agent': 'Mozilla/5.0 (lvvd option data collector)'}


def load_config(path=CONFIG):
    with open(path) as f:
        return yaml.safe_load(f)


def api(product_id, **params):
    r = requests.get(f'https://www.eurex.com/api/v1/overallstatistics/'
                     f'{product_id}', params=params,
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def trading_days_back(start, lookback):
    '''Recent days (most recent first), weekends skipped.'''
    days, d = [], start
    for _ in range(lookback):
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    return days


#
# futures: one overview call per day holds every listed contract
#
def fetch_futures_day(product_id, busdate):
    raw = api(product_id, busdate=busdate.strftime('%Y%m%d'),
              filtertype='overview')
    rows = raw.get('dataRows') or []
    if not rows:
        return None
    df = pd.DataFrame(rows).rename(columns={'date': 'Expiry'})
    df['Expiry'] = pd.to_datetime(df['Expiry'], format='%Y%m%d')
    df['Pricing day'] = pd.Timestamp(busdate.date())
    return df.set_index(['Pricing day', 'Expiry']).sort_index()


#
# options: overview gives the live expiries, detail gives per-strike data
#
def fetch_option_expiries(product_id, busdate):
    '''Monthly expiries only; the detail view serves contracttype=M.'''
    raw = api(product_id, busdate=busdate.strftime('%Y%m%d'),
              filtertype='overview')
    return [r['date'] for r in (raw.get('dataRows') or [])
            if r.get('contractType') == 'M']


def fetch_option_series(product_id, productdate, busdate):
    '''One (pricing day, expiry) pair -> filtered DataFrame or None.

    Same MultiIndex/columns as the book's index_option_series.h5.
    '''
    raw = api(product_id, productdate=productdate,
              busdate=busdate.strftime('%Y%m%d'),
              filtertype='detail', contracttype='M')
    if not raw.get('dataRowsCall') or not raw.get('dataRowsPut'):
        return None

    frames = {}
    for key, col in (('dataRowsCall', 'Call_Price'),
                     ('dataRowsPut', 'Put_Price')):
        df = pd.DataFrame(raw[key])
        df['Pricing day'] = pd.Timestamp(busdate.date())
        df = df.rename(columns={'strike': 'Strike price',
                                'dSettle': col})
        frames[col] = df.set_index(['Pricing day', 'Strike price'])[col] \
                        .astype(float)

    dataset = pd.concat(frames, axis=1, join='inner')
    # same filter as the book: drop strikes with prices < 0.5
    dataset = dataset[(dataset.Call_Price >= 0.5) & (dataset.Put_Price >= 0.5)]
    return dataset if len(dataset) else None


#
# store handling: append with dedup on the index
#
def append(store, key, add):
    if '/' + key.strip('/') in store.keys() and len(add):
        old = store[key]
        add = add[~add.index.isin(old.index)]
        if len(add) == 0:
            return 0
        store[key] = pd.concat((old, add)).sort_index()
    elif len(add):
        store[key] = add.sort_index()
    else:
        return 0
    return len(add)


def collect_futures(store, inst, days, sleep):
    key = f'futures/{inst["code"]}'
    frames = []
    for day in days:
        try:
            df = fetch_futures_day(inst['id'], day)
        except requests.RequestException as e:
            print(f'warning: {inst["code"]} {day:%Y-%m-%d}: {e}')
            continue
        if df is not None:
            frames.append(df)
        time.sleep(sleep)
    if not frames:
        return 0
    return append(store, key, pd.concat(frames))


def collect_options(store, inst, days, sleep):
    added = 0
    for day in days:
        try:
            expiries = fetch_option_expiries(inst['id'], day)
        except requests.RequestException as e:
            print(f'warning: {inst["code"]} {day:%Y-%m-%d}: {e}')
            continue
        for productdate in expiries:
            try:
                dataset = fetch_option_series(inst['id'], productdate, day)
            except requests.RequestException as e:
                print(f'warning: {inst["code"]} {productdate}: {e}')
                continue
            if dataset is None:
                continue
            expiry = dt.datetime.strptime(productdate, '%Y%m%d')
            key = f'options/{inst["code"]}/{expiry:%b%y}'
            added += append(store, key, dataset)
            time.sleep(sleep)
    return added


def collect(config_path=CONFIG, today=None):
    cfg = load_config(config_path)
    today = today or dt.datetime.now()
    days = trading_days_back(today, cfg.get('lookback_days', 28))
    sleep = cfg.get('time_sleep', 0.5)
    instruments = [i for i in cfg['instruments'] if i.get('enabled', True)]

    store = pd.HDFStore(cfg['store'], 'a')
    try:
        for inst in instruments:
            if inst['type'] == 'F':
                n = collect_futures(store, inst, days, sleep)
            else:
                n = collect_options(store, inst, days, sleep)
            print(f'{inst["code"]:>5}: {n} new rows')
    finally:
        store.close()


if __name__ == '__main__':
    collect(*(sys.argv[1:2] or []))
