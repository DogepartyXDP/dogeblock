import os
import re
import json
import base64
import logging
import datetime
import time
import copy
import decimal
import cgi
import calendar

import pymongo

from dogeblock.lib import config, database, util, blockchain

D = decimal.Decimal
logger = logging.getLogger(__name__)


def get_market_price(price_data, vol_data):
    assert len(price_data) == len(vol_data)
    assert len(price_data) <= config.MARKET_PRICE_DERIVE_NUM_POINTS
    market_price = util.weighted_average(list(zip(price_data, vol_data)))
    return market_price


def get_market_price_summary(asset1, asset2, with_last_trades=0, start_dt=None, end_dt=None):
    """Gets a synthesized trading "market price" for a specified asset pair (if available), as well as additional info.
    If no price is available, False is returned.
    """
    if not end_dt:
        end_dt = datetime.datetime.utcnow()
    if not start_dt:
        start_dt = end_dt - datetime.timedelta(days=10)  # default to 10 days in the past

    # look for the last max 6 trades within the past 10 day window
    base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
    base_asset_info = config.mongo_db.tracked_assets.find_one({'asset': base_asset})
    quote_asset_info = config.mongo_db.tracked_assets.find_one({'asset': quote_asset})

    if not isinstance(with_last_trades, int) or with_last_trades < 0 or with_last_trades > 30:
        raise Exception("Invalid with_last_trades")

    if not base_asset_info or not quote_asset_info:
        raise Exception("Invalid asset(s)")

    last_trades = config.mongo_db.trades.find({
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        'block_time': {"$gte": start_dt, "$lte": end_dt}
    },
        {'_id': 0, 'block_index': 1, 'block_time': 1, 'unit_price': 1, 'base_quantity_normalized': 1, 'quote_quantity_normalized': 1}
    ).sort("block_time", pymongo.DESCENDING).limit(max(config.MARKET_PRICE_DERIVE_NUM_POINTS, with_last_trades))
    if not last_trades.count():
        return None  # no suitable trade data to form a market price (return None, NOT False here)
    last_trades = list(last_trades)
    last_trades.reverse()  # from newest to oldest

    market_price = get_market_price(
        [last_trades[i]['unit_price'] for i in range(min(len(last_trades), config.MARKET_PRICE_DERIVE_NUM_POINTS))],
        [(last_trades[i]['base_quantity_normalized'] + last_trades[i]['quote_quantity_normalized']) for i in range(min(len(last_trades), config.MARKET_PRICE_DERIVE_NUM_POINTS))])
    result = {
        'market_price': float(D(market_price)),
        'base_asset': base_asset,
        'quote_asset': quote_asset,
    }
    if with_last_trades:
        #[0]=block_time, [1]=unit_price, [2]=base_quantity_normalized, [3]=quote_quantity_normalized, [4]=block_index
        result['last_trades'] = [[
            t['block_time'],
            t['unit_price'],
            t['base_quantity_normalized'],
            t['quote_quantity_normalized'],
            t['block_index']
        ] for t in last_trades]
    else:
        result['last_trades'] = []
    return result


def calc_inverse(quantity):
    return float((D(1) / D(quantity)))


def calc_price_change(open, close):
    return float((D(100) * (D(close) - D(open)) / D(open)))


def get_price_primitives(start_dt=None, end_dt=None):
    mps_xdp_doge = get_market_price_summary(config.XDP, config.DOGE, start_dt=start_dt, end_dt=end_dt)
    xdp_doge_price = mps_xdp_doge['market_price'] if mps_xdp_doge else None  # == XDP/DOGE
    doge_xdp_price = calc_inverse(mps_xdp_doge['market_price']) if mps_xdp_doge else None  # DOGE/XDP
    return mps_xdp_doge, xdp_doge_price, doge_xdp_price


def get_asset_info(asset, at_dt=None):
    asset_info = config.mongo_db.tracked_assets.find_one({'asset': asset})

    if asset not in (config.XDP, config.DOGE) and at_dt and asset_info['_at_block_time'] > at_dt:
        # get the asset info at or before the given at_dt datetime
        for e in reversed(asset_info['_history']):  # newest to oldest
            if e['_at_block_time'] <= at_dt:
                asset_info = e
                break
        else:  # asset was created AFTER at_dt
            asset_info = None
        if asset_info is None:
            return None
        assert asset_info['_at_block_time'] <= at_dt

    # modify some of the properties of the returned asset_info for DOGE and XDP
    if asset == config.DOGE:
        if at_dt:
            start_block_index, end_block_index = database.get_block_indexes_for_dates(end_dt=at_dt)
            asset_info['total_issued'] = blockchain.get_doge_supply(normalize=False, at_block_index=end_block_index)
            asset_info['total_issued_normalized'] = blockchain.normalize_quantity(asset_info['total_issued'])
        else:
            asset_info['total_issued'] = blockchain.get_doge_supply(normalize=False)
            asset_info['total_issued_normalized'] = blockchain.normalize_quantity(asset_info['total_issued'])
    elif asset == config.XDP:
        # BUG: this does not take end_dt (if specified) into account. however, the deviation won't be too big
        # as XDP doesn't deflate quickly at all, and shouldn't matter that much since there weren't any/much trades
        # before the end of the burn period (which is what is involved with how we use at_dt with currently)
        asset_info['total_issued'] = util.call_jsonrpc_api("get_supply", {'asset': config.XDP}, abort_on_error=True)['result']
        asset_info['total_issued_normalized'] = blockchain.normalize_quantity(asset_info['total_issued'])
    if not asset_info:
        raise Exception("Invalid asset: %s" % asset)
    return asset_info


def get_xdp_doge_price_info(asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price, with_last_trades=0, start_dt=None, end_dt=None):
    if asset not in [config.DOGE, config.XDP]:
        # get price data for both the asset with XDP, as well as DOGE
        price_summary_in_xdp = get_market_price_summary(
            asset, config.XDP,
            with_last_trades=with_last_trades, start_dt=start_dt, end_dt=end_dt)
        price_summary_in_doge = get_market_price_summary(
            asset, config.DOGE,
            with_last_trades=with_last_trades, start_dt=start_dt, end_dt=end_dt)

        # aggregated (averaged) price (expressed as XDP) for the asset on both the XDP and DOGE markets
        if price_summary_in_xdp:  # no trade data
            price_in_xdp = price_summary_in_xdp['market_price']
            if xdp_doge_price:
                aggregated_price_in_xdp = float(((D(price_summary_in_xdp['market_price']) + D(xdp_doge_price)) / D(2)))
            else:
                aggregated_price_in_xdp = None
        else:
            price_in_xdp = None
            aggregated_price_in_xdp = None

        if price_summary_in_doge:  # no trade data
            price_in_doge = price_summary_in_doge['market_price']
            if doge_xdp_price:
                aggregated_price_in_doge = float(((D(price_summary_in_doge['market_price']) + D(doge_xdp_price)) / D(2)))
            else:
                aggregated_price_in_doge = None
        else:
            aggregated_price_in_doge = None
            price_in_doge = None
    else:
        # here we take the normal XDP/DOGE pair, and invert it to DOGE/XDP, to get XDP's data in terms of a DOGE base
        # (this is the only area we do this, as DOGE/XDP is NOT standard pair ordering)
        price_summary_in_xdp = mps_xdp_doge  # might be None
        price_summary_in_doge = copy.deepcopy(mps_xdp_doge) if mps_xdp_doge else None  # must invert this -- might be None
        if price_summary_in_doge:
            price_summary_in_doge['market_price'] = calc_inverse(price_summary_in_doge['market_price'])
            price_summary_in_doge['base_asset'] = config.DOGE
            price_summary_in_doge['quote_asset'] = config.XDP
            for i in range(len(price_summary_in_doge['last_trades'])):
                #[0]=block_time, [1]=unit_price, [2]=base_quantity_normalized, [3]=quote_quantity_normalized, [4]=block_index
                price_summary_in_doge['last_trades'][i][1] = calc_inverse(price_summary_in_doge['last_trades'][i][1])
                price_summary_in_doge['last_trades'][i][2], price_summary_in_doge['last_trades'][i][3] = \
                    price_summary_in_doge['last_trades'][i][3], price_summary_in_doge['last_trades'][i][2]  # swap
        if asset == config.XDP:
            price_in_xdp = 1.0
            price_in_doge = price_summary_in_doge['market_price'] if price_summary_in_doge else None
            aggregated_price_in_xdp = 1.0
            aggregated_price_in_doge = doge_xdp_price  # might be None
        else:
            assert asset == config.DOGE
            price_in_xdp = price_summary_in_xdp['market_price'] if price_summary_in_xdp else None
            price_in_doge = 1.0
            aggregated_price_in_xdp = xdp_doge_price  # might be None
            aggregated_price_in_doge = 1.0
    return (price_summary_in_xdp, price_summary_in_doge, price_in_xdp, price_in_doge, aggregated_price_in_xdp, aggregated_price_in_doge)


def calc_market_cap(asset_info, price_in_xdp, price_in_doge):
    market_cap_in_xdp = float((D(asset_info['total_issued_normalized']) / D(price_in_xdp))) if price_in_xdp else None
    market_cap_in_doge = float((D(asset_info['total_issued_normalized']) / D(price_in_doge))) if price_in_doge else None
    return market_cap_in_xdp, market_cap_in_doge


def compile_summary_market_info(asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price):
    """Returns information related to capitalization, volume, etc for the supplied asset(s)
    NOTE: in_doge == base asset is DOGE, in_xdp == base asset is XDP
    @param assets: A list of one or more assets
    """
    asset_info = get_asset_info(asset)
    (price_summary_in_xdp, price_summary_in_doge, price_in_xdp, price_in_doge, aggregated_price_in_xdp, aggregated_price_in_doge
     ) = get_xdp_doge_price_info(asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price, with_last_trades=30)
    market_cap_in_xdp, market_cap_in_doge = calc_market_cap(asset_info, price_in_xdp, price_in_doge)
    return {
        'price_in_{}'.format(config.XDP.lower()): price_in_xdp,  # current price of asset vs XDP (e.g. how many units of asset for 1 unit XDP)
        'price_in_{}'.format(config.DOGE.lower()): price_in_doge,  # current price of asset vs DOGE (e.g. how many units of asset for 1 unit DOGE)
        'price_as_{}'.format(config.XDP.lower()): calc_inverse(price_in_xdp) if price_in_xdp else None,  # current price of asset AS XDP
        'price_as_{}'.format(config.DOGE.lower()): calc_inverse(price_in_doge) if price_in_doge else None,  # current price of asset AS DOGE
        'aggregated_price_in_{}'.format(config.XDP.lower()): aggregated_price_in_xdp,
        'aggregated_price_in_{}'.format(config.DOGE.lower()): aggregated_price_in_doge,
        'aggregated_price_as_{}'.format(config.XDP.lower()): calc_inverse(aggregated_price_in_xdp) if aggregated_price_in_xdp else None,
        'aggregated_price_as_{}'.format(config.DOGE.lower()): calc_inverse(aggregated_price_in_doge) if aggregated_price_in_doge else None,
        'total_supply': asset_info['total_issued_normalized'],
        'market_cap_in_{}'.format(config.XDP.lower()): market_cap_in_xdp,
        'market_cap_in_{}'.format(config.DOGE.lower()): market_cap_in_doge,
    }


def compile_24h_market_info(asset):
    asset_data = {}
    start_dt_1d = datetime.datetime.utcnow() - datetime.timedelta(days=1)

    # perform aggregation to get 24h statistics
    # TOTAL volume and count across all trades for the asset (on ALL markets, not just XDP and DOGE pairings)
    _24h_vols = {'vol': 0, 'count': 0}
    _24h_vols_as_base = config.mongo_db.trades.aggregate([
        {"$match": {
            "base_asset": asset,
            "block_time": {"$gte": start_dt_1d}}},
        {"$project": {
            "base_quantity_normalized": 1  # to derive volume
        }},
        {"$group": {
            "_id":   1,
            "vol":   {"$sum": "$base_quantity_normalized"},
            "count": {"$sum": 1},
        }}
    ])
    _24h_vols_as_base = list(_24h_vols_as_base)
    _24h_vols_as_base = {} if not len(_24h_vols_as_base) else _24h_vols_as_base[0]
    _24h_vols_as_quote = config.mongo_db.trades.aggregate([
        {"$match": {
            "quote_asset": asset,
            "block_time": {"$gte": start_dt_1d}}},
        {"$project": {
            "quote_quantity_normalized": 1  # to derive volume
        }},
        {"$group": {
            "_id":   1,
            "vol":   {"$sum": "quote_quantity_normalized"},
            "count": {"$sum": 1},
        }}
    ])
    _24h_vols_as_quote = list(_24h_vols_as_quote)
    _24h_vols_as_quote = {} if not len(_24h_vols_as_quote) else _24h_vols_as_quote[0]
    _24h_vols['vol'] = _24h_vols_as_base.get('vol', 0) + _24h_vols_as_quote.get('vol', 0)
    _24h_vols['count'] = _24h_vols_as_base.get('count', 0) + _24h_vols_as_quote.get('count', 0)

    # XDP market volume with stats
    if asset != config.XDP:
        _24h_ohlc_in_xdp = config.mongo_db.trades.aggregate(
            [{"$match": {
                "base_asset": config.XDP,
                "quote_asset": asset,
                "block_time": {"$gte": start_dt_1d}}},
             {"$project": {
                 "unit_price": 1,
                 "base_quantity_normalized": 1  # to derive volume
             }},
                {"$group": {
                    "_id":   1,
                    "open":  {"$first": "$unit_price"},
                    "high":  {"$max": "$unit_price"},
                    "low":   {"$min": "$unit_price"},
                    "close": {"$last": "$unit_price"},
                    "vol":   {"$sum": "$base_quantity_normalized"},
                    "count": {"$sum": 1},
                }}
             ])
        _24h_ohlc_in_xdp = list(_24h_ohlc_in_xdp)
        _24h_ohlc_in_xdp = {} if not len(_24h_ohlc_in_xdp) else _24h_ohlc_in_xdp[0]
        if _24h_ohlc_in_xdp:
            del _24h_ohlc_in_xdp['_id']
    else:
        _24h_ohlc_in_xdp = {}

    # DOGE market volume with stats
    if asset != config.DOGE:
        _24h_ohlc_in_doge = config.mongo_db.trades.aggregate([
            {"$match": {
                "base_asset": config.DOGE,
                "quote_asset": asset,
                "block_time": {"$gte": start_dt_1d}}},
            {"$project": {
                "unit_price": 1,
                "base_quantity_normalized": 1  # to derive volume
            }},
            {"$group": {
                "_id":   1,
                "open":  {"$first": "$unit_price"},
                "high":  {"$max": "$unit_price"},
                "low":   {"$min": "$unit_price"},
                "close": {"$last": "$unit_price"},
                "vol":   {"$sum": "$base_quantity_normalized"},
                "count": {"$sum": 1},
            }}
        ])
        _24h_ohlc_in_doge = list(_24h_ohlc_in_doge)
        _24h_ohlc_in_doge = {} if not len(_24h_ohlc_in_doge) else _24h_ohlc_in_doge[0]
        if _24h_ohlc_in_doge:
            del _24h_ohlc_in_doge['_id']
    else:
        _24h_ohlc_in_doge = {}

    return {
        '24h_summary': _24h_vols,
        #^ total quantity traded of that asset in all markets in last 24h
        '24h_ohlc_in_{}'.format(config.XDP.lower()): _24h_ohlc_in_xdp,
        #^ quantity of asset traded with DOGE in last 24h
        '24h_ohlc_in_{}'.format(config.DOGE.lower()): _24h_ohlc_in_doge,
        #^ quantity of asset traded with XDP in last 24h
        '24h_vol_price_change_in_{}'.format(config.XDP.lower()): calc_price_change(_24h_ohlc_in_xdp['open'], _24h_ohlc_in_xdp['close'])
        if _24h_ohlc_in_xdp else None,
        #^ aggregated price change from 24h ago to now, expressed as a signed float (e.g. .54 is +54%, -1.12 is -112%)
        '24h_vol_price_change_in_{}'.format(config.DOGE.lower()): calc_price_change(_24h_ohlc_in_doge['open'], _24h_ohlc_in_doge['close'])
        if _24h_ohlc_in_doge else None,
    }


def compile_7d_market_info(asset):
    start_dt_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)

    # get XDP and DOGE market summarized trades over a 7d period (quantize to hour long slots)
    _7d_history_in_xdp = None  # xdp/asset market (or xdp/doge for xdp or doge)
    _7d_history_in_doge = None  # doge/asset market (or doge/xdp for xdp or doge)
    if asset not in [config.DOGE, config.XDP]:
        for a in [config.XDP, config.DOGE]:
            _7d_history = config.mongo_db.trades.aggregate([
                {"$match": {
                    "base_asset": a,
                    "quote_asset": asset,
                    "block_time": {"$gte": start_dt_7d}
                }},
                {"$project": {
                    "year":  {"$year": "$block_time"},
                    "month": {"$month": "$block_time"},
                    "day":   {"$dayOfMonth": "$block_time"},
                    "hour":  {"$hour": "$block_time"},
                    "unit_price": 1,
                    "base_quantity_normalized": 1  # to derive volume
                }},
                {"$sort": {"block_time": pymongo.ASCENDING}},
                {"$group": {
                    "_id":   {"year": "$year", "month": "$month", "day": "$day", "hour": "$hour"},
                    "price": {"$avg": "$unit_price"},
                    "vol":   {"$sum": "$base_quantity_normalized"},
                }},
            ])
            _7d_history = list(_7d_history)
            if a == config.XDP:
                _7d_history_in_xdp = _7d_history
            else:
                _7d_history_in_doge = _7d_history
    else:  # get the XDP/DOGE market and invert for DOGE/XDP (_7d_history_in_doge)
        _7d_history = config.mongo_db.trades.aggregate([
            {"$match": {
                "base_asset": config.XDP,
                "quote_asset": config.DOGE,
                "block_time": {"$gte": start_dt_7d}
            }},
            {"$project": {
                "year":  {"$year": "$block_time"},
                "month": {"$month": "$block_time"},
                "day":   {"$dayOfMonth": "$block_time"},
                "hour":  {"$hour": "$block_time"},
                "unit_price": 1,
                "base_quantity_normalized": 1  # to derive volume
            }},
            {"$sort": {"block_time": pymongo.ASCENDING}},
            {"$group": {
                "_id":   {"year": "$year", "month": "$month", "day": "$day", "hour": "$hour"},
                "price": {"$avg": "$unit_price"},
                "vol":   {"$sum": "$base_quantity_normalized"},
            }},
        ])
        _7d_history_in_xdp = list(_7d_history)
        _7d_history_in_doge = copy.deepcopy(_7d_history_in_xdp)
        for i in range(len(_7d_history_in_doge)):
            _7d_history_in_doge[i]['price'] = calc_inverse(_7d_history_in_doge[i]['price'])
            _7d_history_in_doge[i]['vol'] = calc_inverse(_7d_history_in_doge[i]['vol'])

    for l in [_7d_history_in_xdp, _7d_history_in_doge]:
        for e in l:  # convert our _id field out to be an epoch ts (in ms), and delete _id
            e['when'] = calendar.timegm(datetime.datetime(e['_id']['year'], e['_id']['month'], e['_id']['day'], e['_id']['hour']).timetuple()) * 1000
            del e['_id']

    return {
        '7d_history_in_{}'.format(config.XDP.lower()): [[e['when'], e['price']] for e in _7d_history_in_xdp],
        '7d_history_in_{}'.format(config.DOGE.lower()): [[e['when'], e['price']] for e in _7d_history_in_doge],
    }


def compile_asset_pair_market_info():
    """Compiles the pair-level statistics that show on the View Prices page of dogewallet, for instance"""
    # loop through all open orders, and compile a listing of pairs, with a count of open orders for each pair
    end_dt = datetime.datetime.utcnow()
    start_dt = end_dt - datetime.timedelta(days=1)
    start_block_index, end_block_index = database.get_block_indexes_for_dates(start_dt=start_dt, end_dt=end_dt)
    open_orders = util.call_jsonrpc_api(
        "get_orders",
        {'filters': [
            {'field': 'give_remaining', 'op': '>', 'value': 0},
            {'field': 'get_remaining', 'op': '>', 'value': 0},
            {'field': 'fee_required_remaining', 'op': '>=', 'value': 0},
            {'field': 'fee_provided_remaining', 'op': '>=', 'value': 0},
        ],
            'status': 'open',
            'show_expired': False,
        }, abort_on_error=True)['result']
    pair_data = {}
    asset_info = {}

    def get_price(base_quantity_normalized, quote_quantity_normalized):
        return float(D(quote_quantity_normalized / base_quantity_normalized))

    # COMPOSE order depth, lowest ask, and highest bid column data
    for o in open_orders:
        (base_asset, quote_asset) = util.assets_to_asset_pair(o['give_asset'], o['get_asset'])
        pair = '%s/%s' % (base_asset, quote_asset)
        base_asset_info = asset_info.get(base_asset, config.mongo_db.tracked_assets.find_one({'asset': base_asset}))
        if base_asset not in asset_info:
            asset_info[base_asset] = base_asset_info
        quote_asset_info = asset_info.get(quote_asset, config.mongo_db.tracked_assets.find_one({'asset': quote_asset}))
        if quote_asset not in asset_info:
            asset_info[quote_asset] = quote_asset_info

        pair_data.setdefault(
            pair,
            {'open_orders_count': 0, 'lowest_ask': None, 'highest_bid': None,
             'completed_trades_count': 0, 'vol_base': 0, 'vol_quote': 0})
        #^ highest ask = open order selling base, highest bid = open order buying base
        #^ we also initialize completed_trades_count, vol_base, vol_quote because every pair inited here may
        # not have cooresponding data out of the trades_data_by_pair aggregation below
        pair_data[pair]['open_orders_count'] += 1
        base_quantity_normalized = blockchain.normalize_quantity(o['give_quantity'] if base_asset == o['give_asset'] else o['get_quantity'], base_asset_info['divisible'])
        quote_quantity_normalized = blockchain.normalize_quantity(o['give_quantity'] if quote_asset == o['give_asset'] else o['get_quantity'], quote_asset_info['divisible'])
        order_price = get_price(base_quantity_normalized, quote_quantity_normalized)
        if base_asset == o['give_asset']:  # selling base
            if pair_data[pair]['lowest_ask'] is None or order_price < pair_data[pair]['lowest_ask']:
                pair_data[pair]['lowest_ask'] = order_price
        elif base_asset == o['get_asset']:  # buying base
            if pair_data[pair]['highest_bid'] is None or order_price > pair_data[pair]['highest_bid']:
                pair_data[pair]['highest_bid'] = order_price

    # COMPOSE volume data (in XDP and DOGE), and % change data
    # loop through all trade volume over the past 24h, and match that to the open orders
    trades_data_by_pair = config.mongo_db.trades.aggregate([
        {"$match": {
            "block_time": {"$gte": start_dt, "$lte": end_dt}}
         },
        {"$project": {
            "base_asset": 1,
            "quote_asset": 1,
            "base_quantity_normalized": 1,  # to derive base volume
            "quote_quantity_normalized": 1  # to derive quote volume
        }},
        {"$group": {
            "_id":   {"base_asset": "$base_asset", "quote_asset": "$quote_asset"},
            "vol_base":   {"$sum": "$base_quantity_normalized"},
            "vol_quote":   {"$sum": "$quote_quantity_normalized"},
            "count": {"$sum": 1},
        }}
    ])
    for e in trades_data_by_pair:
        pair = '%s/%s' % (e['_id']['base_asset'], e['_id']['quote_asset'])
        pair_data.setdefault(pair, {'open_orders_count': 0, 'lowest_ask': None, 'highest_bid': None})
        #^ initialize an empty pair in the event there are no open orders for that pair, but there ARE completed trades for it
        pair_data[pair]['completed_trades_count'] = e['count']
        pair_data[pair]['vol_base'] = e['vol_base']
        pair_data[pair]['vol_quote'] = e['vol_quote']

    # compose price data, relative to DOGE and XDP
    mps_xdp_doge, xdp_doge_price, doge_xdp_price = get_price_primitives()
    for pair, e in pair_data.items():
        base_asset, quote_asset = pair.split('/')
        _24h_vol_in_doge = None
        _24h_vol_in_xdp = None
        # derive asset price data, expressed in DOGE and XDP, for the given volumes
        if base_asset == config.XDP:
            _24h_vol_in_xdp = e['vol_base']
            _24h_vol_in_doge = blockchain.round_out(e['vol_base'] * xdp_doge_price) if xdp_doge_price else 0
        elif base_asset == config.DOGE:
            _24h_vol_in_xdp = blockchain.round_out(e['vol_base'] * doge_xdp_price) if doge_xdp_price else 0
            _24h_vol_in_doge = e['vol_base']
        else:  # base is not XDP or DOGE
            price_summary_in_xdp, price_summary_in_doge, price_in_xdp, price_in_doge, aggregated_price_in_xdp, aggregated_price_in_doge = \
                get_xdp_doge_price_info(base_asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price, with_last_trades=0, start_dt=start_dt, end_dt=end_dt)
            if price_in_xdp:
                _24h_vol_in_xdp = blockchain.round_out(e['vol_base'] * price_in_xdp)
            if price_in_doge:
                _24h_vol_in_doge = blockchain.round_out(e['vol_base'] * price_in_doge)

            if _24h_vol_in_xdp is None or _24h_vol_in_doge is None:
                # the base asset didn't have price data against DOGE or XDP, or both...try against the quote asset instead
                price_summary_in_xdp, price_summary_in_doge, price_in_xdp, price_in_doge, aggregated_price_in_xdp, aggregated_price_in_doge = \
                    get_xdp_doge_price_info(quote_asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price, with_last_trades=0, start_dt=start_dt, end_dt=end_dt)
                if _24h_vol_in_xdp is None and price_in_xdp:
                    _24h_vol_in_xdp = blockchain.round_out(e['vol_quote'] * price_in_xdp)
                if _24h_vol_in_doge is None and price_in_doge:
                    _24h_vol_in_doge = blockchain.round_out(e['vol_quote'] * price_in_doge)
            pair_data[pair]['24h_vol_in_{}'.format(config.XDP.lower())] = _24h_vol_in_xdp  # might still be None
            pair_data[pair]['24h_vol_in_{}'.format(config.DOGE.lower())] = _24h_vol_in_doge  # might still be None

        # get % change stats -- start by getting the first trade directly before the 24h period starts
        prev_trade = config.mongo_db.trades.find({
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "block_time": {'$lt': start_dt}}).sort('block_time', pymongo.DESCENDING).limit(1)
        latest_trade = config.mongo_db.trades.find({
            "base_asset": base_asset,
            "quote_asset": quote_asset}).sort('block_time', pymongo.DESCENDING).limit(1)
        if not prev_trade.count():  # no previous trade before this 24hr period
            pair_data[pair]['24h_pct_change'] = None
        else:
            prev_trade = prev_trade[0]
            latest_trade = latest_trade[0]
            prev_trade_price = get_price(prev_trade['base_quantity_normalized'], prev_trade['quote_quantity_normalized'])
            latest_trade_price = get_price(latest_trade['base_quantity_normalized'], latest_trade['quote_quantity_normalized'])
            pair_data[pair]['24h_pct_change'] = ((latest_trade_price - prev_trade_price) / prev_trade_price) * 100
        pair_data[pair]['last_updated'] = end_dt
        # print "PRODUCED", pair, pair_data[pair]
        config.mongo_db.asset_pair_market_info.update({'base_asset': base_asset, 'quote_asset': quote_asset}, {"$set": pair_data[pair]}, upsert=True)

    # remove any old pairs that were not just updated
    config.mongo_db.asset_pair_market_info.remove({'last_updated': {'$lt': end_dt}})
    logger.info("Recomposed 24h trade statistics for %i asset pairs: %s" % (len(pair_data), ', '.join(list(pair_data.keys()))))


def compile_asset_market_info():
    """Run through all assets and compose and store market ranking information."""

    if not config.state['caught_up']:
        logger.warn("Not updating asset market info as dogeblockd is not caught up.")
        return False

    # grab the last block # we processed assets data off of
    last_block_assets_compiled = config.mongo_db.app_config.find_one()['last_block_assets_compiled']
    last_block_time_assets_compiled = database.get_block_time(last_block_assets_compiled)
    #logger.debug("Comping info for assets traded since block %i" % last_block_assets_compiled)
    current_block_index = config.state['my_latest_block']['block_index']  # store now as it may change as we are compiling asset data :)
    current_block_time = database.get_block_time(current_block_index)

    if current_block_index == last_block_assets_compiled:
        # all caught up -- call again in 10 minutes
        return True

    mps_xdp_doge, xdp_doge_price, doge_xdp_price = get_price_primitives()
    all_traded_assets = list(set(list([config.DOGE, config.XDP]) + list(config.mongo_db.trades.find({}, {'quote_asset': 1, '_id': 0}).distinct('quote_asset'))))

    #######################
    # get a list of all assets with a trade within the last 24h (not necessarily just against XDP and DOGE)
    # ^ this is important because compiled market info has a 24h vol parameter that designates total volume for the asset across ALL pairings
    start_dt_1d = datetime.datetime.utcnow() - datetime.timedelta(days=1)

    assets = list(set(
        list(config.mongo_db.trades.find({'block_time': {'$gte': start_dt_1d}}).distinct('quote_asset'))
        + list(config.mongo_db.trades.find({'block_time': {'$gte': start_dt_1d}}).distinct('base_asset'))
    ))
    for asset in assets:
        market_info_24h = compile_24h_market_info(asset)
        config.mongo_db.asset_market_info.update({'asset': asset}, {"$set": market_info_24h})
    # for all others (i.e. no trade in the last 24 hours), zero out the 24h trade data
    non_traded_assets = list(set(all_traded_assets) - set(assets))
    config.mongo_db.asset_market_info.update({'asset': {'$in': non_traded_assets}}, {"$set": {
        '24h_summary': {'vol': 0, 'count': 0},
        '24h_ohlc_in_{}'.format(config.XDP.lower()): {},
        '24h_ohlc_in_{}'.format(config.DOGE.lower()): {},
        '24h_vol_price_change_in_{}'.format(config.XDP.lower()): None,
        '24h_vol_price_change_in_{}'.format(config.DOGE.lower()): None,
    }}, multi=True)
    logger.info("Block: %s -- Calculated 24h stats for: %s" % (current_block_index, ', '.join(assets)))

    #######################
    # get a list of all assets with a trade within the last 7d up against XDP and DOGE
    start_dt_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    assets = list(set(
        list(config.mongo_db.trades.find({'block_time': {'$gte': start_dt_7d}, 'base_asset': {'$in': [config.XDP, config.DOGE]}}).distinct('quote_asset'))
        + list(config.mongo_db.trades.find({'block_time': {'$gte': start_dt_7d}}).distinct('base_asset'))
    ))
    for asset in assets:
        market_info_7d = compile_7d_market_info(asset)
        config.mongo_db.asset_market_info.update({'asset': asset}, {"$set": market_info_7d})
    non_traded_assets = list(set(all_traded_assets) - set(assets))
    config.mongo_db.asset_market_info.update({'asset': {'$in': non_traded_assets}}, {"$set": {
        '7d_history_in_{}'.format(config.XDP.lower()): [],
        '7d_history_in_{}'.format(config.DOGE.lower()): [],
    }}, multi=True)
    logger.info("Block: %s -- Calculated 7d stats for: %s" % (current_block_index, ', '.join(assets)))

    #######################
    # update summary market data for assets traded since last_block_assets_compiled
    # get assets that were traded since the last check with either DOGE or XDP, and update their market summary data
    assets = list(set(
        list(config.mongo_db.trades.find({'block_index': {'$gt': last_block_assets_compiled}, 'base_asset': {'$in': [config.XDP, config.DOGE]}}).distinct('quote_asset'))
        + list(config.mongo_db.trades.find({'block_index': {'$gt': last_block_assets_compiled}}).distinct('base_asset'))
    ))
    # update our storage of the latest market info in mongo
    for asset in assets:
        logger.info("Block: %s -- Updating asset market info for %s ..." % (current_block_index, asset))
        summary_info = compile_summary_market_info(asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price)
        config.mongo_db.asset_market_info.update({'asset': asset}, {"$set": summary_info}, upsert=True)

    #######################
    # next, compile market cap historicals (and get the market price data that we can use to update assets with new trades)
    # NOTE: this algoritm still needs to be fleshed out some...I'm not convinced it's laid out/optimized like it should be
    # start by getting all trades from when we last compiled this data
    trades = config.mongo_db.trades.find({'block_index': {'$gt': last_block_assets_compiled}}).sort('block_index', pymongo.ASCENDING)
    trades_by_block = []  # tracks assets compiled per block, as we only want to analyze any given asset once per block
    trades_by_block_mapping = {}
    # organize trades by block
    for t in trades:
        if t['block_index'] in trades_by_block_mapping:
            assert trades_by_block_mapping[t['block_index']]['block_index'] == t['block_index']
            assert trades_by_block_mapping[t['block_index']]['block_time'] == t['block_time']
            trades_by_block_mapping[t['block_index']]['trades'].append(t)
        else:
            e = {'block_index': t['block_index'], 'block_time': t['block_time'], 'trades': [t, ]}
            trades_by_block.append(e)
            trades_by_block_mapping[t['block_index']] = e

    for t_block in trades_by_block:
        # reverse the tradelist per block, and ensure that we only process an asset that hasn't already been processed for this block
        # (as there could be multiple trades in a single block for any specific asset). we reverse the list because
        # we'd rather process a later trade for a given asset, as the market price for that will take into account
        # the earlier trades on that same block for that asset, and we don't want/need multiple cap points per block
        assets_in_block = {}
        mps_xdp_doge, xdp_doge_price, doge_xdp_price = get_price_primitives(end_dt=t_block['block_time'])
        for t in reversed(t_block['trades']):
            assets = []
            if t['base_asset'] not in assets_in_block:
                assets.append(t['base_asset'])
                assets_in_block[t['base_asset']] = True
            if t['quote_asset'] not in assets_in_block:
                assets.append(t['quote_asset'])
                assets_in_block[t['quote_asset']] = True
            if not len(assets):
                continue

            for asset in assets:
                # recalculate the market cap for the asset this trade is for
                asset_info = get_asset_info(asset, at_dt=t['block_time'])
                (price_summary_in_xdp, price_summary_in_doge, price_in_xdp, price_in_doge, aggregated_price_in_xdp, aggregated_price_in_doge
                 ) = get_xdp_doge_price_info(asset, mps_xdp_doge, xdp_doge_price, doge_xdp_price, with_last_trades=0, end_dt=t['block_time'])
                market_cap_in_xdp, market_cap_in_doge = calc_market_cap(asset_info, price_in_xdp, price_in_doge)
                #^ this will get price data from the block time of this trade back the standard number of days and trades
                # to determine our standard market price, relative (anchored) to the time of this trade

                for market_cap_as in (config.XDP, config.DOGE):
                    market_cap = market_cap_in_xdp if market_cap_as == config.XDP else market_cap_in_doge
                    # if there is a previously stored market cap for this asset, add a new history point only if the two caps differ
                    prev_market_cap_history = config.mongo_db.asset_marketcap_history.find(
                        {'market_cap_as': market_cap_as, 'asset': asset,
                         'block_index': {'$lt': t['block_index']}}).sort('block_index', pymongo.DESCENDING).limit(1)
                    prev_market_cap_history = list(prev_market_cap_history)[0] if prev_market_cap_history.count() == 1 else None

                    if market_cap and (not prev_market_cap_history or prev_market_cap_history['market_cap'] != market_cap):
                        config.mongo_db.asset_marketcap_history.insert(
                            {
                                'block_index': t['block_index'],
                                'block_time': t['block_time'],
                                'asset': asset,
                                'market_cap': market_cap,
                                'market_cap_as': market_cap_as,
                            })
                        logger.info("Block %i -- Calculated market cap history point for %s as %s (mID: %s)" % (t['block_index'], asset, market_cap_as, t['message_index']))

    config.mongo_db.app_config.update({}, {'$set': {'last_block_assets_compiled': current_block_index}})
    return True
