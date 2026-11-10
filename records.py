"""Рекорды и вехи.

История заказов живёт только в памяти этого запуска: при старте бот пересчитывает её из Ozon (~2 мин)
и никуда не сохраняет, так что объёмы продаж не попадают ни в state.json, ни в кэш GitHub Actions.
В state бот хранит только отметки «об этом уже написали» — хэши, по которым ничего не восстановить.

Что считается: все заказы, вместе с отменёнными («заказано»), см. _agg.
В подписи к дашбордам рекорды не пишутся, только отдельными сообщениями:
- в 22:00 одним сообщением: рекорд дня за всё время (заказы, сумма), «столько не было с …» (если день лучший
  хотя бы за 30 дней), личный рекорд товара (от 5 шт за день), вехи;
- лучшая неделя / месяц — отдельным сообщением сразу после автоитога;
- вехи: каждые 1000 заказов и каждый миллион ₽ за всё время, каждые 500 000 ₽ заказов за месяц.
История грузится, только когда нужна (22:00 и автоитоги по понедельникам и 1-го числа), а не при каждом запуске.
"""
import collections, datetime, hashlib, html, os, threading

from ozon_data import _posts, _now, MSK, NAMES, MONTH_NOM, MONTH_GEN

FIRST = datetime.datetime(2019, 1, 1)   # с запасом: первый заказ магазина позже
DAY_GAP, WEEK_GAP, MONTH_GAP = 30, 28, 60   # «не было с …» пишем, только если прошло столько дней
PROD_MIN = 5                     # личный рекорд товара — от стольких штук за день
NOTE = '<i>Считаем все заказы, вместе с отменёнными: так дни сравниваются на равных.</i>'  # строка под рекордами
STEP_ORDERS, STEP_SUM, STEP_MONTH = 1000, 1_000_000, 500_000
H = {'ready': False, 'upto': None, 'day': {}, 'prod': {}}   # day: дата → [шт, ₽]; prod: артикул → {дата: шт}
hl = threading.Lock()
_salt = hashlib.sha256(('records:' + (os.environ.get('OZON_API_KEY') or '')).encode()).digest()

fmt = lambda v: f'{v:,.0f}'.replace(',', ' ')


def mark(key):
    """Отметка «уже писали» для state.json: хэш с секретом, сами числа из него не достать."""
    return hashlib.sha256(_salt + key.encode()).hexdigest()[:16]


def plural(n, a, b, c):
    return a if n % 10 == 1 and n % 100 != 11 else (b if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else c)


def orders_word(n):
    return f'{fmt(n)} {plural(n, "заказ", "заказа", "заказов")}'


def ddate(d, today=None):
    today = today or _now().date()
    return d.strftime('%d.%m') if d.year == today.year else d.strftime('%d.%m.%Y')


def _agg(posts):
    """Все заказы, вместе с отменёнными. Отмены приходят днями позже: если их вычитать, у прошлых дней они уже
    вычтены, а у сегодняшнего ещё нет, и обычный день выходит «рекордом» (так было 10.11.2026: 53 заказа на 229 тыс.
    против 27.10, где к вечеру было 70 на 289 тыс., а после отмен осталось 51 на 212 тыс.). «Заказано» задним
    числом не меняется."""
    day = collections.defaultdict(lambda: [0, 0.0]); prod = collections.defaultdict(collections.Counter)
    for x in posts:
        d = (datetime.datetime.fromisoformat(x['created_at'][:19]) + MSK).date()
        for p in x['products']:
            q = p['quantity']; pr = float(p['price']['amount'] if isinstance(p['price'], dict) else p['price'])
            day[d][0] += q; day[d][1] += pr * q; prod[p['offer_id']][d] += q
    return day, prod


def load():
    """Вся история до начала сегодняшнего дня."""
    today = _now().date()
    day, prod = _agg(_posts(FIRST, datetime.datetime.combine(today, datetime.time())))
    with hl:
        H.update(day=dict(day), prod={k: dict(v) for k, v in prod.items()}, upto=today, ready=True)


def refresh(back=8):
    """Пересчитываем последние дни: вчерашний день целиком и отмены, пришедшие задним числом."""
    if not H['ready']:
        return
    today = _now().date(); start = min(H['upto'], today) - datetime.timedelta(back)
    day, prod = _agg(_posts(datetime.datetime.combine(start, datetime.time()), datetime.datetime.combine(today, datetime.time())))
    with hl:
        H['day'] = {d: v for d, v in H['day'].items() if d < start}; H['day'].update(day)
        for c in H['prod'].values():
            for d in [d for d in c if d >= start]:
                del c[d]
        for k, c in prod.items():
            H['prod'].setdefault(k, {}).update(c)
        H['upto'] = today


def ensure_fresh():
    if H['ready'] and H['upto'] < _now().date():
        refresh()


def _since(series, d, v):
    """Последний день (неделя, месяц) до d, когда было не меньше v. None — такого не было никогда."""
    best = None
    for k, x in series.items():
        if k < d and x >= v and (best is None or k > best):
            best = k
    return best


def _top(series, before):
    """Рекорд до даты before: (значение, дата)."""
    items = [(v, d) for d, v in series.items() if d < before]
    return max(items) if items else (0, None)


# ---------- «столько не было с …» и личные рекорды товаров за день ----------
def day_lines(d, o, s, prodq):
    """(отметка, текст) для дня d с o заказами на s ₽ и штуками по товарам prodq — против истории до d.
    Рекорд за всё время здесь не пишем: это отдельное сообщение «Рекорд дня!» в live()."""
    with hl:
        so = {k: v[0] for k, v in H['day'].items() if k < d}; ss = {k: v[1] for k, v in H['day'].items() if k < d}
        prods = [(k, q, max([x for y, x in H['prod'].get(k, {}).items() if y < d] or [0]),
                  min([y for y in H['prod'].get(k, {}) if y < d] or [d]))
                 for k, q in prodq.items() if q >= PROD_MIN and k in NAMES]
    out = []
    for v, ser, what in ((o, so, 'o'), (s, ss, 's')):
        since = _since(ser, d, v) if v else None
        if since and (d - since).days >= DAY_GAP:
            out.append((f'B:{what}:{d}', f'🔥 {orders_word(o)} за день: столько не было с {ddate(since)}' if what == 'o'
                        else f'🔥 {fmt(s)} ₽ заказов за день: такого не было с {ddate(since)}'))
    # у нового товара первые дни — всегда «рекорд», поэтому только для тех, что продаются 2+ недели
    best = sorted([(q, k) for k, q, was, first in prods if q > was and (d - first).days >= 14], reverse=True)[:3]
    out += [(f'E:{d}:{k}', f'⭐ {html.escape(NAMES.get(k, k))}: {q} шт за день, личный рекорд') for q, k in best]  # уходит с parse_mode=HTML
    return out


def history_day(d):
    """o, s, штуки по товарам за прошедший день из истории — для примера."""
    with hl:
        o, s = H['day'].get(d, (0, 0.0))
        return o, s, {k: c[d] for k, c in H['prod'].items() if d in c}


# ---------- строки в итоги недели и месяца ----------
def period_lines(kind, start, end):
    if not H['ready']:
        return []
    key = (lambda d: d - datetime.timedelta(d.weekday())) if kind == 'week' else (lambda d: d.replace(day=1))
    so, ss = collections.Counter(), collections.Counter()
    with hl:
        for d, (o, s) in H['day'].items():
            if d <= end:
                so[key(d)] += o; ss[key(d)] += s
    k0 = key(start); o, s = so.get(k0, 0), ss.get(k0, 0)
    first = min(so) if so else k0
    so = {k: v for k, v in so.items() if k != first or k == k0}; ss = {k: v for k, v in ss.items() if k != first or k == k0}  # первый неполный период не в счёт
    name = 'неделя' if kind == 'week' else 'месяц'
    gap = WEEK_GAP if kind == 'week' else MONTH_GAP
    label = (lambda k: f'недели {ddate(k)}') if kind == 'week' else (lambda k: f'{MONTH_GEN[k.month]} {k.year}')
    out = []
    for v, ser, what in ((o, so, 'orders'), (s, ss, 'sum')):
        if not v:
            continue
        since = _since(ser, k0, v)
        val = orders_word(v) if what == 'orders' else f'{fmt(v)} ₽ заказов'
        if since is None:
            out.append(f'🏆 Лучшая {name} за всё время: {val}' if kind == 'week' else f'🏆 Лучший {name} за всё время: {val}')
        elif (k0 - since).days >= gap:
            out.append(f'🔥 {val} за {"неделю" if kind == "week" else "месяц"}: {"столько" if what == "orders" else "такого"} не было с {label(since)}')
    return out


# ---------- проверка в течение дня ----------
def today_totals():
    now = _now()
    day, prod = _agg(_posts(datetime.datetime.combine(now.date(), datetime.time()), now))
    o, s = day.get(now.date(), [0, 0.0])
    return o, s, {k: c[now.date()] for k, c in prod.items() if now.date() in c}


def ensure_loaded():
    if not H['ready']:
        load()
    else:
        ensure_fresh()


def live(now=None, cur=None):
    """Список (отметка, текст) для сообщения в 22:00: рекорд дня за всё время, «столько не было с …»,
    личные рекорды товаров, вехи, пройденные сегодня. Отметки — чтобы не писать дважды."""
    if not H['ready']:
        return []
    now = now or _now(); today = now.date()
    o, s, prodq = cur if cur is not None else today_totals()
    with hl:
        so = {k: v[0] for k, v in H['day'].items() if k < today}; ss = {k: v[1] for k, v in H['day'].items() if k < today}
        month = sum(v[1] for k, v in H['day'].items() if k < today and k.year == today.year and k.month == today.month)
    out = []
    (ro, rod), (rs, rsd) = _top(so, today), _top(ss, today)
    if ro and o > ro:
        out.append((f'A:o:{today}', f'🏆 <b>Рекорд дня!</b> Уже {orders_word(o)}, столько за день ещё не было. Прошлый рекорд {fmt(ro)}, {ddate(rod)}'))
    if rs and s > rs:
        out.append((f'A:s:{today}', f'🏆 <b>Рекорд дня по сумме!</b> Уже {fmt(s)} ₽ заказов. Прошлый рекорд {fmt(rs)} ₽, {ddate(rsd)}'))
    out += day_lines(today, o, s, prodq)
    # вехи: только если перешли границу сегодня
    first = min(so) if so else today
    for before, after, step, key, text in (
            (sum(so.values()), sum(so.values()) + o, STEP_ORDERS, 'D:o',
             lambda m: f'🎉 <b>{fmt(m)}-й заказ</b> за всё время! Первый был {ddate(first)}'),
            (sum(ss.values()), sum(ss.values()) + s, STEP_SUM, 'D:s',
             lambda m: f'🎉 <b>{fmt(m // 1_000_000)} млн ₽</b> заказов за всё время! Первый заказ был {ddate(first)}'),
            (month, month + s, STEP_MONTH, f'D:m:{today:%Y-%m}',
             lambda m: f'🎯 {MONTH_NOM[today.month].capitalize()}: уже <b>{fmt(m)} ₽</b> заказов')):
        if before and int(after // step) > int(before // step):
            m = int(after // step) * step
            out.append((f'{key}:{m}', text(m)))
    return out


def status():
    """Для демо и проверки: рекорды и итоги без объёмов в логе."""
    today = _now().date()
    with hl:
        so = {k: v[0] for k, v in H['day'].items() if k < today}; ss = {k: v[1] for k, v in H['day'].items() if k < today}
    return {'orders': _top(so, today), 'sum': _top(ss, today), 'total_orders': sum(so.values()), 'total_sum': sum(ss.values()),
            'first': min(so) if so else None}
