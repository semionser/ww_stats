"""Сбор данных Ozon для дашборда. collect() = сегодня на текущий момент, collect(final_day) = итог за день."""
import json, os, time, datetime, collections, urllib.request, urllib.error

API = 'https://api-seller.ozon.ru'
MSK = datetime.timedelta(hours=3)


def _products():
    """Названия товаров и группа для отдельного счётчика — только из секрета PRODUCTS_JSON, в коде их нет."""
    try:
        c = json.loads(os.environ['PRODUCTS_JSON'])
        groups = c.get('caption_groups') or [{'label': c.get('group_label', 'Группа'), 'codes': c['group']}]
        return c['names'], set(c['group']), c.get('group_label', 'Группа'), groups
    except Exception:
        raise SystemExit('PRODUCTS_JSON не задан или испорчен')


NAMES, GROUP, GROUP_LABEL, CAPTION_GROUPS = _products()


def call(path, body):
    hdr = {'Content-Type': 'application/json',
           'Client-Id': os.environ['OZON_CLIENT_ID'],
           'Api-Key': os.environ['OZON_API_KEY']}
    err = None
    for i in range(5):
        try:
            req = urllib.request.Request(API + path, data=json.dumps(body).encode(), headers=hdr, method='POST')
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = f'{e.code}'
            if e.code in (401, 403):
                break
        except Exception as e:
            err = type(e).__name__
        time.sleep(2 * (i + 1))
    raise RuntimeError(f'Ozon API {path}: {err}')


def collect(final_day=None):
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK
    if final_day:  # итог за прошедший день
        today = final_day
        cut = datetime.time(23, 59, 59)
    else:
        today = now.date()
        cut = now.time()
    yest = today - datetime.timedelta(1)
    start = today - datetime.timedelta(days=6)
    since = (datetime.datetime.combine(start, datetime.time()) - MSK).strftime('%Y-%m-%dT%H:%M:%SZ')
    to = (datetime.datetime.combine(today + datetime.timedelta(1), datetime.time()) - MSK).strftime('%Y-%m-%dT%H:%M:%SZ')
    posts, cur = [], ''
    while True:
        d = call('/v3/posting/fbo/list', {"dir": "ASC", "filter": {"since": since, "to": to}, "limit": 100, "cursor": cur})
        posts += d['postings']
        if not d.get('has_next'):
            break
        cur = d['cursor']
    byday = collections.defaultdict(lambda: [0, 0.0, 0, 0])
    items = collections.defaultdict(lambda: [0, 0, 0.0]); week = collections.Counter()
    hourly = {today: [0] * 24, yest: [0] * 24}; last = None; ysame = [0, 0.0, 0]
    for x in posts:
        t = datetime.datetime.fromisoformat(x['created_at'][:19]) + MSK; day = t.date()
        for p in x['products']:
            q = p['quantity']; pr = float(p['price']['amount'] if isinstance(p['price'], dict) else p['price']); oid = p['offer_id']
            if x['status'] == 'cancelled':
                byday[day][2] += q
                if day == today: items[oid][1] += q
                continue
            byday[day][0] += q; byday[day][1] += pr * q
            if oid in GROUP: byday[day][3] += q
            week[oid] += q
            if day in hourly: hourly[day][t.hour] += q
            if day == today: items[oid][0] += q; items[oid][2] += pr * q
            if day == yest and t.time() <= cut:
                ysame[0] += q; ysame[1] += pr * q
                if oid in GROUP: ysame[2] += q
        if day == today and x['status'] != 'cancelled' and (last is None or t > last): last = t
    st = call('/v2/analytics/stock_on_warehouses', {"limit": 1000, "offset": 0, "warehouse_type": "ALL"})
    stock = collections.Counter()
    for r in st['result']['rows']: stock[r['item_code']] += r['free_to_sell_amount']
    days = []; summary = None
    for i in range(7):
        day = start + datetime.timedelta(i)
        b = call('/v1/finance/balance', {"date_from": str(day), "date_to": str(day)})
        t = b['total']; cf = b['cashflows']
        days.append({"day": str(day), "accrued": round(t['accrued']['value']), "orders": byday[day][0], "orders_sum": round(byday[day][1]), "cancelled": byday[day][2], "group": byday[day][3]})
        if day == today:
            summary = {"balance": round(t['closing_balance']['value']), "opening": round(t['opening_balance']['value']), "accrued": round(t['accrued']['value']), "sales": round(cf['sales']['amount']['value']), "returns": round(cf['returns']['amount']['value']),
                       "orders": byday[day][0], "orders_sum": round(byday[day][1]), "cancelled": byday[day][2], "group": byday[day][3],
                       "y_orders": ysame[0], "y_sum": round(ysame[1]), "y_group": ysame[2],
                       "last_order": last.strftime('%H:%M') if last else '—',
                       "updated": today.strftime('%d.%m') + ' ' + ('23:59' if final_day else now.strftime('%H:%M')),
                       "hour": 23 if final_day else now.hour}
    cum = lambda a: [sum(a[:i + 1]) for i in range(24)]
    hours = {"today": cum(hourly[today]), "yest": cum(hourly[yest])}
    it = []
    for k in sorted(set(items) | GROUP, key=lambda k: (-items[k][0] if k in items else 0, k)):
        v = items.get(k, [0, 0, 0.0]); avg = week[k] / 7
        it.append({"code": k, "item": NAMES.get(k, k), "orders": v[0], "cancelled": v[1], "sum": round(v[2]), "stock": stock.get(k, 0), "avg7": round(avg, 1), "cover": round(stock.get(k, 0) / avg) if avg else None})
    return {"group_label": GROUP_LABEL, "caption_groups": CAPTION_GROUPS, "summary": summary, "items": it, "days": days, "hours": hours}
