#
# Collects daily STOXX index levels for the VSTOXX family (V2TX, V6I1, V6I2)
# from the public index pages at stoxx.com. Each page embeds the full
# historical chart series (back to 1999) server-side as
#     window.chart_data = [[epoch_ms, value], ...]
# so no API key or scraping of rendered content is needed.
#
# Output: data/stoxx_indices.csv with columns DATE, V2TX, V6I1, V6I2
# (one row per trading day, inner-joined on available values; V6I1 is
# undefined on days close to expiry, matching the book's V6I1/V6I2 logic).
#
import re
import datetime as dt

import pandas as pd
import requests

INDICES = {
    'V2TX': 'https://stoxx.com/index/v2tx/',   # EURO STOXX 50 Volatility (VSTOXX)
    'V6I1': 'https://stoxx.com/index/v6i1/',   # VSTOXX 1-month sub-index
    'V6I2': 'https://stoxx.com/index/v6i2/',   # VSTOXX 2-month sub-index
}
HEADERS = {'User-Agent': 'Mozilla/5.0 (lvvd stoxx index collector)'}
OUTPUT = 'data/stoxx_indices.csv'


def fetch_index(url):
    '''Returns a date-indexed Series with the index levels.'''
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r'window\.chart_data = (\[\[.*?\]\])', r.text)
    if m is None:
        raise ValueError('chart_data not found on page: %s' % url)
    raw = eval(m.group(1))
    idx = [dt.datetime.fromtimestamp(ms / 1000, dt.UTC).date() for ms, _ in raw]
    return pd.Series([v for _, v in raw], index=pd.to_datetime(idx), name='values')


def collect():
    data = pd.concat({sym: fetch_index(url) for sym, url in INDICES.items()},
                     axis=1, join='outer')
    data.index.name = 'DATE'
    data = data.sort_index()
    data.to_csv(OUTPUT)
    print('wrote %s: %d rows, %s -> %s'
          % (OUTPUT, len(data), data.index[0].date(), data.index[-1].date()))
    return data


if __name__ == '__main__':
    collect()
