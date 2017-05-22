# -*- coding: utf-8 -*-
#
# Copyright 2017 Ricequant, Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

import six
import numpy as np
import pandas as pd

from ..const import MARGIN_TYPE, COMMISSION_TYPE
from ..interface import AbstractDataSource
from ..utils.datetime_func import convert_date_to_int, convert_int_to_date
from ..utils.datetime_func import convert_int_to_datetime
from ..utils.py2 import lru_cache
from .converter import FutureDayBarConverter, FundDayBarConverter
from .converter import StockBarConverter, IndexBarConverter
from .date_set import DateSet
from .daybar_store import DayBarStore
from .dividend_store import DividendStore
from .future_info_cn import CN_FUTURE_INFO
from .instrument_store import InstrumentStore
from .trading_dates_store import TradingDatesStore
from .yield_curve_store import YieldCurveStore


class BaseDataSource(AbstractDataSource):
    def __init__(self, path):
        def _p(name):
            return os.path.join(path, name)

        self._day_bars = [
            DayBarStore(_p('stocks.bcolz'), StockBarConverter),
            DayBarStore(_p('indexes.bcolz'), IndexBarConverter),
            DayBarStore(_p('futures.bcolz'), FutureDayBarConverter),
            DayBarStore(_p('funds.bcolz'), FundDayBarConverter),
        ]

        self._instruments = InstrumentStore(_p('instruments.pk'))
        self._adjusted_dividends = DividendStore(_p('adjusted_dividends.bcolz'))
        self._original_dividends = DividendStore(_p('original_dividends.bcolz'))
        self._trading_dates = TradingDatesStore(_p('trading_dates.bcolz'))
        self._yield_curve = YieldCurveStore(_p('yield_curve.bcolz'))

        self._st_stock_days = DateSet(_p('st_stock_days.bcolz'))
        self._suspend_days = DateSet(_p('suspended_days.bcolz'))

        self.get_yield_curve = self._yield_curve.get_yield_curve
        self.get_risk_free_rate = self._yield_curve.get_risk_free_rate

    def get_dividend(self, order_book_id, adjusted=True):
        if adjusted:
            return self._adjusted_dividends.get_dividend(order_book_id)
        else:
            return self._original_dividends.get_dividend(order_book_id)

    def get_trading_minutes_for(self, order_book_id, trading_dt):
        raise NotImplementedError

    def get_trading_calendar(self):
        return self._trading_dates.get_trading_calendar()

    def get_all_instruments(self):
        return self._instruments.get_all_instruments()

    def is_suspended(self, order_book_id, dates):
        return self._suspend_days.contains(order_book_id, dates)

    def is_st_stock(self, order_book_id, dates):
        return self._st_stock_days.contains(order_book_id, dates)

    INSTRUMENT_TYPE_MAP = {
        'CS': 0,
        'INDX': 1,
        'Future': 2,
        'ETF': 3,
        'LOF': 3,
        'FenjiA': 3,
        'FenjiB': 3,
        'FenjiMu': 3,
    }

    def _index_of(self, instrument):
        return self.INSTRUMENT_TYPE_MAP[instrument.type]

    @lru_cache(None)
    def _all_day_bars_of(self, instrument):
        i = self._index_of(instrument)
        return self._day_bars[i].get_bars(instrument.order_book_id, fields=None)

    @lru_cache(None)
    def _filtered_day_bars(self, instrument):
        bars = self._all_day_bars_of(instrument)
        if bars is None:
            return None
        return bars[bars['volume'] > 0]

    def get_bar(self, instrument, dt, frequency):
        if frequency != '1d':
            raise NotImplementedError

        bars = self._all_day_bars_of(instrument)
        if bars is None:
            return
        dt = convert_date_to_int(dt)
        pos = bars['datetime'].searchsorted(dt)
        if pos >= len(bars) or bars['datetime'][pos] != dt:
            return None

        return bars[pos]

    def get_settle_price(self, instrument, date):
        bar = self.get_bar(instrument, date, '1d')
        if bar is None:
            return np.nan
        return bar['settlement']

    @staticmethod
    def _are_fields_valid(fields, valid_fields):
        if fields is None:
            return True
        if isinstance(fields, six.string_types):
            return fields in valid_fields
        for field in fields:
            if field not in valid_fields:
                return False
        return True

    @staticmethod
    def _resample_k_bars(bars, fields, frequency):
        fds = fields
        if isinstance(fds, six.string_types):
            fds = [fds]
        if "datetime" not in fds:
            fds.append("datetime")

        handler = {
            "datetime": "last",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "total_turnover": "sum",
        }
        bars_df = pd.DataFrame(bars)
        bars_df["dt"] = bars_df["datetime"].apply(convert_int_to_datetime)
        bars_df.set_index("dt", inplace=True)
        agg = bars_df.resample(frequency)

        df = pd.DataFrame()
        for field in fds:
            df[field] = getattr(agg[field], handler[field])()

        df = df.dropna()
        df["datetime"] = df["datetime"].astype(np.uint64)
        bars = df.to_records(index=False)

        return bars

    def history_bars(self, instrument, bar_count, frequency, fields, dt,
                     skip_suspended=True, include_now=False):
        if frequency not in ['1d', "W", "M"]:
            raise NotImplementedError

        if skip_suspended and instrument.type == 'CS':
            bars = self._filtered_day_bars(instrument)
        else:
            bars = self._all_day_bars_of(instrument)

        if bars is None or not self._are_fields_valid(fields, bars.dtype.names):
            return None

        if frequency == "W":
            bars = self._resample_k_bars(bars, fields, "W-Fri")
        if frequency == "M":
            bars = self._resample_k_bars(bars, fields, "M")

        dt = convert_date_to_int(dt)
        i = bars['datetime'].searchsorted(dt, side='right')
        left = i - bar_count if i >= bar_count else 0
        if fields is None:
            return bars[left:i]
        else:
            return bars[left:i][fields]

    def get_yield_curve(self, start_date, end_date, tenor=None):
        return self._yield_curve.get_yield_curve(start_date, end_date, tenor)

    def get_risk_free_rate(self, start_date, end_date):
        return self._yield_curve.get_risk_free_rate(start_date, end_date)

    def current_snapshot(self, instrument, frequency, dt):
        raise NotImplementedError

    def get_split(self, order_book_id):
        return None

    def available_data_range(self, frequency):
        if frequency in ['tick', '1d']:
            s, e = self._day_bars[self.INSTRUMENT_TYPE_MAP['INDX']].get_date_range('000001.XSHG')
            return convert_int_to_date(s).date(), convert_int_to_date(e).date()

        raise NotImplementedError

    def get_margin_info(self, instrument):
        return {
            'margin_type': MARGIN_TYPE.BY_MONEY,
            'long_margin_ratio': instrument.margin_rate,
            'short_margin_ratio': instrument.margin_rate,
        }

    def get_commission_info(self, instrument):
        return CN_FUTURE_INFO[instrument.underlying_symbol]['speculation']

    def get_ticks(self, order_book_id, date):
        raise NotImplementedError
