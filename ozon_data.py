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


def _now():
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK


def _posts(since, to):
    """Заказы FBO между двумя моментами по МСК (naive datetime)."""
    z = lambda t: (t - MSK).strftime('%Y-%m-%dT%H:%M:%SZ')
    out, cur = [], ''
    while True:
        d = call('/v3/posting/fbo/list', {"dir": "ASC", "filter": {"since": z(since), "to": z(to)}, "limit": 100, "cursor": cur})
        out += d['postings']
        if not d.get('has_next'):
            return out
        cur = d['cursor']


def _stock():
    st = call('/v2/analytics/stock_on_warehouses', {"limit": 1000, "offset": 0, "warehouse_type": "ALL"})
    stock = collections.Counter()
    for r in st['result']['rows']:
        stock[r['item_code']] += r['free_to_sell_amount']
    return stock


def watch():
    """Лёгкий срез для тревог: время последнего заказа, обычный темп по часам (7 прошлых дней),
    остатки и средние продажи в день (как в дашборде: последние 7 дней, включая сегодня)."""
    now = _now(); today = now.date()
    posts = _posts(datetime.datetime.combine(today - datetime.timedelta(7), datetime.time()), now)
    base = [0.0] * 24; week = collections.Counter(); last = None
    for x in posts:
        if x['status'] == 'cancelled':
            continue
        t = datetime.datetime.fromisoformat(x['created_at'][:19]) + MSK
        q = sum(p['quantity'] for p in x['products'])
        if t.date() < today:
            base[t.hour] += q / 7
        if t.date() >= today - datetime.timedelta(6):
            for p in x['products']:
                week[p['offer_id']] += p['quantity']
        if last is None or t > last:
            last = t
    return {"now": now, "last": last, "base": base, "stock": _stock(), "avg7": {k: v / 7 for k, v in week.items()}}


SUPPLY_ACTIVE = ['DATA_FILLING', 'READY_TO_SUPPLY', 'ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT',
                 'ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING', 'REPORT_REJECTED',
                 'REJECTED_AT_SUPPLY_WAREHOUSE']


def _supply_ids(states, limit=100, pages=5):
    out, last = [], ''
    for _ in range(pages):
        body = {"filter": {"states": states}, "limit": limit, "sort_by": "ORDER_CREATION", "sort_dir": "DESC"}
        if last:
            body['last_id'] = last
        d = call('/v3/supply-order/list', body)
        out += d.get('order_ids') or []
        last = d.get('last_id')
        if not last or not d.get('order_ids') or len(out) >= limit:
            break
    return out


def _bundle(bundle_id):
    items, last = [], ''
    for _ in range(10):
        body = {"bundle_ids": [bundle_id], "limit": 100}
        if last:
            body['last_id'] = last
        d = call('/v1/supply-order/bundle', body)
        items += [(i['offer_id'], i['quantity'], i.get('name') or i['offer_id']) for i in d.get('items') or []]
        if not d.get('has_next'):
            break
        last = d.get('last_id')
    return items


def _cargo_tracking(supply_ids):
    """По коробкам поставки: плановая дата приезда на склад (как «Плановая дата доставки» в кабинете)
    и сколько коробок уже приехало. {supply_id: {'eta': date|None, 'boxes': n, 'arrived': n}}."""
    out = {}
    for i in range(0, len(supply_ids), 100):
        try:
            d = call('/v1/cargoes/get', {"supply_ids": supply_ids[i:i + 100]})
        except RuntimeError:
            continue  # без сроков карточка всё равно покажется
        for s in d.get('supply') or []:
            boxes = s.get('cargoes') or []
            exp = [x['tracking_info']['date'] for x in boxes if (x.get('tracking_info') or {}).get('type') == 'EXPECTED_ARRIVAL' and x['tracking_info'].get('date')]
            arr = [x for x in boxes if (x.get('tracking_info') or {}).get('type') == 'ACTUAL_ARRIVAL']
            out[s['supply_id']] = {'eta': datetime.date.fromisoformat(max(exp)[:10]) if exp else None,
                                   'boxes': len(boxes), 'arrived': len(arr)}
    return out


def supplies(done_days=14, done_max=3):
    """Поставки FBO: все незавершённые и до done_max завершённых за последние done_days дней, с составом."""
    ts = lambda s: datetime.datetime.fromisoformat(s[:19]) + MSK if s else None
    active = _supply_ids(SUPPLY_ACTIVE)
    done = _supply_ids(['COMPLETED'], limit=10, pages=1) if done_max else []
    orders = []
    ids = active + done
    for i in range(0, len(ids), 50):
        orders += call('/v3/supply-order/get', {"order_ids": ids[i:i + 50]}).get('orders') or []
    track = _cargo_tracking([s['supply_id'] for o in orders for s in o.get('supplies') or [] if s.get('supply_id')])
    out = []
    since = _now() - datetime.timedelta(done_days)
    n_done = 0
    for o in sorted(orders, key=lambda o: o.get('created_date') or '', reverse=True):
        upd = ts(o.get('state_updated_date'))
        if o['state'] == 'COMPLETED':
            if n_done >= done_max or (upd and upd < since):
                continue
            n_done += 1
        slot = ((o.get('timeslot') or {}).get('timeslot') or {}).get('from')
        stor = [((s.get('storage_warehouse') or {}).get('name') or '') for s in o.get('supplies') or []]
        items = []
        tr = {'eta': None, 'boxes': 0, 'arrived': 0}
        for s in o.get('supplies') or []:
            if s.get('bundle_id'):
                items += _bundle(s['bundle_id'])
            t = track.get(s.get('supply_id'))
            if t:
                tr['boxes'] += t['boxes']; tr['arrived'] += t['arrived']
                tr['eta'] = max(filter(None, [tr['eta'], t['eta']]), default=None)
        out.append({"id": o['order_id'], "number": o.get('order_number') or str(o['order_id']), "state": o['state'],
                    "created": ts(o.get('created_date')), "updated": upd, "slot": ts(slot),
                    "dropoff": (o.get('drop_off_warehouse') or {}).get('name') or '',
                    "crossdock": any(s.get('is_crossdock') for s in o.get('supplies') or []),
                    "eta": tr['eta'], "boxes": tr['boxes'], "boxes_arrived": tr['arrived'],
                    "storage": [x for x in stor if x],
                    "items": [(k, q, NAMES.get(k, nm)) for k, q, nm in items]})
    return out


def supply_states(ids):
    """Текущий статус поставок по их order_id: {order_id: state}."""
    out = {}
    for i in range(0, len(ids), 50):
        for o in call('/v3/supply-order/get', {"order_ids": ids[i:i + 50]}).get('orders') or []:
            out[o['order_id']] = o['state']
    return out


def transit_history(n=30):
    """Сколько дней прошлые кросс-док поставки шли от сдачи в точке отгрузки до приёмки (по завершённым).
    Озон не отдаёт плановую дату приезда, поэтому оцениваем по истории. Явные выбросы (> 10 дн) отбрасываем."""
    ids = _supply_ids(['COMPLETED'], limit=n, pages=1)
    out = []
    for i in range(0, len(ids), 50):
        for o in call('/v3/supply-order/get', {"order_ids": ids[i:i + 50]}).get('orders') or []:
            slot = ((o.get('timeslot') or {}).get('timeslot') or {}).get('from')
            if slot and o.get('state_updated_date') and any(s.get('is_crossdock') for s in o.get('supplies') or []):
                d = (datetime.datetime.fromisoformat(o['state_updated_date'][:19]) - datetime.datetime.fromisoformat(slot[:19])).total_seconds() / 86400
                if 0.2 < d <= 10:
                    out.append(d)
    return out


def prices():
    """Цена каждого товара на сайте до скидок Ozon: marketing_seller_price (с акциями продавца), иначе price."""
    out, cur = {}, ''
    for _ in range(20):
        d = call('/v5/product/info/prices', {"filter": {"visibility": "ALL"}, "limit": 100, "cursor": cur})
        for x in d.get('items') or []:
            p = x.get('price') or {}
            v = float(p.get('marketing_seller_price') or 0) or float(p.get('price') or 0)
            if v > 0:
                out[x['offer_id']] = v
        cur = d.get('cursor')
        if not cur or not d.get('items'):
            break
    return out
