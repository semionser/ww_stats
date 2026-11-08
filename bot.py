"""Телеграм-бот «Ozon сегодня».

Команды в разрешённой группе: /now, /Now, /щас — дашборд; /supply, /поставки — поставки на склады Ozon
(также с @имя_бота). Каждый день в DAILY_TIME (МСК) присылает итог за вчера.
После приёмки поставки спрашивает «УПД подписал?» с кнопками ДА/НЕТ и переспрашивает каждые 3 ч днём до «ДА».
Сам пишет в группу (с 08:00 до 24:00 МСК), когда товар закончился, когда запаса осталось меньше 5 дней,
когда днём (10:00–22:00) 3+ часа нет заказов, когда цена товара сдвинулась на 10%+ и когда у поставки сменился статус.

Безопасность:
- работает только в одном чате (ALLOWED_CHAT_ID); из любых других чатов сразу выходит и ничего не отвечает;
- при включённом privacy mode (по умолчанию у BotFather) Telegram не присылает боту обычные сообщения группы;
  бот всё равно смотрит только на первое слово сообщения и нигде не сохраняет и не логирует текст;
- опционально отвечает только людям из ALLOWED_USER_IDS;
- только исходящие запросы (long polling), никаких открытых портов и вебхуков;
- ключи только из переменных окружения, в лог не пишутся.
"""
import collections, datetime, html, json, statistics, logging, os, sys, tempfile, threading, time, urllib.request, urllib.error, uuid

_missing = [k for k in ('TELEGRAM_BOT_TOKEN', 'ALLOWED_CHAT_ID', 'OZON_CLIENT_ID', 'OZON_API_KEY', 'PRODUCTS_JSON') if not os.environ.get(k)]
if _missing and sys.argv[1:2] != ['test']:
    sys.exit('Не заданы секреты: ' + ', '.join(_missing))

from ozon_data import collect, MSK, NAMES, watch, supplies, supply_states, transit_history, prices, SUPPLY_ACTIVE
from widget import widget_html
from render import to_png

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = int(os.environ['ALLOWED_CHAT_ID'])
USERS = {int(x) for x in os.environ.get('ALLOWED_USER_IDS', '').replace(' ', '').split(',') if x}
DAILY = os.environ.get('DAILY_TIME', '09:00')
STATE = os.environ.get('STATE_FILE', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state', 'state.json'))
SERVE_SECONDS = float(os.environ.get('SERVE_SECONDS', 0))  # 0 = без ограничения; в GitHub Actions ~5 ч 50 мин
COMMANDS = {'/now': 'now', '/щас': 'now', '/supply': 'supply', '/поставки': 'supply'}
# ответом на сообщение бота: такие ответы Telegram доставляет и в privacy mode
WORDS = {'щас': 'now', 'ща': 'now', 'сейчас': 'now', 'now': 'now', 'поставки': 'supply', 'supply': 'supply'}
WATCH_EVERY = 15 * 60          # как часто проверять склад и паузу в заказах, сек.
LOW_DAYS, LOW_RESET = 5, 7     # тревога, когда запас < 5 дн; снова следим, когда поднялся до 7+
GAP_HOURS = 3                  # тревога, если днём 3+ ч нет ни одного заказа
DAY_FROM, DAY_TO = 10, 22      # «днём» = с 10:00 до 22:00 МСК; паузу до 10:00 не считаем
PRICE_JUMP = 0.10              # тревога, если цена товара сдвинулась на 10%+ от последней известной
QUIET_UNTIL = 8                # с 00:00 до 08:00 МСК бот сам не пишет
COOLDOWN = 60  # сек.: чаще одного раза в минуту не дёргаем Ozon, повторяем последнюю картинку
# Кто подписывает УПД — только из секрета UPD_SIGNER, в коде нет. Один подписант или список:
# {"id": ..., "username": ..., "name": ...}  или  [{...}, {...}]
try:
    _sg = json.loads(os.environ.get('UPD_SIGNER') or '[]')
    SIGNERS = [x for x in (_sg if isinstance(_sg, list) else [_sg]) if str(x.get('id', '')).isdigit()]
except Exception:
    SIGNERS = []
SIGNER_BY_ID = {int(x['id']): x for x in SIGNERS}
OWNERS = set(USERS)  # люди из ALLOWED_USER_IDS
if SIGNER_BY_ID and USERS:
    USERS = USERS | set(SIGNER_BY_ID)  # подписанты тоже могут вызывать команды
UPD_EVERY = 3 * 3600  # пока нет «ДА», переспрашиваем каждые 3 ч днём
NOT_SIGNER = 'вы не роман, ваш отец не зарабатывает 300к'  # чужим
NOT_SIGNER_OWNER = 'Не по статусу)'                         # людям из ALLOWED_USER_IDS
UPD_DONE = '#упдподпись Все успешно💋'  # ответ в группу, когда подписант нажал ДА

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('bot')
# Любая непойманная ошибка пишется в лог только типом: текст исключения может содержать данные.
sys.excepthook = lambda t, v, tb: log.error('fatal: %s', t.__name__)
threading.excepthook = lambda a: log.error('thread error: %s', a.exc_type.__name__)
lock = threading.Lock()
slock = threading.Lock()  # state.json пишут несколько потоков
cache = {'t': 0, 'png': None, 'caption': ''}
scache = {'t': 0, 'text': ''}
ME = ''


def tg(method, data=None, files=None, timeout=70):
    url = f'https://api.telegram.org/bot{TOKEN}/{method}'
    if files:
        b = uuid.uuid4().hex; body = b''
        for k, v in (data or {}).items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fn, content) in files.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: image/png\r\n\r\n'.encode() + content + b'\r\n'
        body += f'--{b}--\r\n'.encode()
        req = urllib.request.Request(url, data=body, headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    else:
        req = urllib.request.Request(url, data=json.dumps(data or {}).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get('result')
    except urllib.error.HTTPError as e:
        log.warning('telegram %s: HTTP %s', method, e.code)  # без URL: в нём токен
    except Exception as e:
        log.warning('telegram %s: %s', method, type(e).__name__)
    return None


fmt = lambda v: f'{v:,.0f}'.replace(',', ' ')


def caption(A, final):
    """Подпись под картинкой (Telegram HTML): баланс, начислено за день, заказы с разбивкой по группам и сумма."""
    s = A['summary']
    by_code = {r['code']: r['orders'] for r in A['items']}
    parts = [str(sum(by_code.get(c, 0) for c in g['codes'])) for g in A.get('caption_groups', [])]
    br = f' ({"/".join(parts)})' if parts else ''
    acc = ('+' if s['accrued'] > 0 else '') + fmt(s['accrued'])
    t = (f'Баланс: <b>{fmt(s["balance"])} ₽</b>\n'
         f'За день: <b>{acc} ₽</b>\n'
         f'Заказы: <b>{s["orders"]}</b>{br} · <b>{fmt(s["orders_sum"])} ₽</b>')
    if final:
        t = f'<b>Итог за {s["updated"][:5]}</b>\n' + t
    return t


def build(final_day=None):
    A = collect(final_day)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'ozon.png')
        to_png(widget_html(A, FINAL=bool(final_day), wide=True), p, wide=True)
        return open(p, 'rb').read(), caption(A, bool(final_day))


def send_dashboard(final_day=None):
    with lock:
        if not final_day and cache['png'] and time.time() - cache['t'] < COOLDOWN:
            png, cap = cache['png'], cache['caption']
        else:
            try:
                png, cap = build(final_day)
            except Exception as e:
                log.error('build failed: %s', type(e).__name__)
                tg('sendMessage', {'chat_id': CHAT_ID, 'text': 'Не получилось забрать данные Ozon, попробуйте через пару минут.'})
                return
            if not final_day:
                cache.update(t=time.time(), png=png, caption=cap)
    tg('sendPhoto', {'chat_id': CHAT_ID, 'caption': cap, 'parse_mode': 'HTML'}, files={'photo': ('ozon.png', png)})


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    json.dump(st, open(tmp, 'w'))
    os.replace(tmp, STATE)


def msk_now():
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK


# ---------- «УПД подписал?» ----------
def next_ask(t):
    """Через 3 ч, но только днём: если выпадает на ночь — в DAY_FROM:00 ближайшего утра."""
    n = t + datetime.timedelta(seconds=UPD_EVERY)
    if n.hour >= DAY_TO:
        n = (n + datetime.timedelta(days=1)).replace(hour=DAY_FROM, minute=0, second=0, microsecond=0)
    elif n.hour < DAY_FROM:
        n = n.replace(hour=DAY_FROM, minute=0, second=0, microsecond=0)
    return n


def sn(num):
    """Короткий номер поставки: последние 4 цифры (2000073405642 → 5642). Полный номер есть в кабинете."""
    s = str(num)
    return s[-4:] if s.isdigit() and len(s) > 6 else s


def supply_ref(num, q, city=''):
    return f'№{sn(num)}' + (f' → {city}' if city else '') + (f' · {q} шт' if q else '')


def upd_question(num, q, demo=False, city='', shown=None):
    """shown — в примере: номер настоящей поставки в пути, которую показываем (ключ кнопок при этом 'demo')."""
    who = ''.join(f' @{x["username"]}' for x in SIGNERS if x.get('username'))
    tail = 'Поставка ' + supply_ref(shown or num, q, city)
    if demo:
        tail = f'🧪 пример, эта поставка ещё в пути\n{tail}' if shown else '🧪 пример, поставка выдумана'
    return f'УПД подписал?{who}\n{tail}'


def ask_upd(num, q, demo=False, city='', shown=None):
    """Шлёт вопрос с кнопками ДА/НЕТ; у прошлого вопроса по этой поставке кнопки убирает."""
    kb = {'inline_keyboard': [[{'text': 'ДА', 'callback_data': f'upd:{num}:y'}, {'text': 'НЕТ', 'callback_data': f'upd:{num}:n'}]]}
    r = tg('sendMessage', {'chat_id': CHAT_ID, 'text': upd_question(num, q, demo, city, shown), 'reply_markup': kb})
    if not r:
        return
    with slock:
        st = load_state(); u = st.setdefault('upd', {}); old = u.get(num)
        u[num] = {'q': q, 'c': city, 'n': shown, 'msg': r['message_id'], 'next': next_ask(msk_now()).isoformat(), 'demo': demo}
        save_state(st)
    if old and old.get('msg'):
        tg('editMessageReplyMarkup', {'chat_id': CHAT_ID, 'message_id': old['msg'], 'reply_markup': {'inline_keyboard': []}})
    log.info('upd question sent')


def check_upd():
    now = msk_now()
    if not (DAY_FROM <= now.hour < DAY_TO):
        return
    with slock:
        due = [(n, e) for n, e in (load_state().get('upd') or {}).items()
               if not e.get('demo') and datetime.datetime.fromisoformat(e['next']) <= now]
    for n, e in due:
        ask_upd(n, e.get('q'), city=e.get('c') or '')


def upd_signed(num, e, msg_id):
    """ДА: вопрос сворачиваем в две строки «✅ УПД подписан» + поставка,
    в группу — одна строка поздравления ответом на него (поставка видна в цитате)."""
    if e.get('demo') and not e.get('n'):
        text = '✅ УПД подписан\n🧪 пример, поставка выдумана'
    else:
        text = ('✅ УПД подписан' + (' · 🧪 пример' if e.get('demo') else '')
                + '\nПоставка ' + supply_ref(e.get('n') or num, e.get('q'), e.get('c') or ''))
    tg('editMessageText', {'chat_id': CHAT_ID, 'message_id': msg_id, 'text': text})
    tg('sendMessage', {'chat_id': CHAT_ID, 'text': UPD_DONE, 'reply_to_message_id': msg_id})


def demo_upd_question():
    """Пример вопроса про УПД на настоящей поставке в пути, если она есть."""
    try:
        s = next((s for s in supplies(done_max=0) if s['state'] != 'COMPLETED'), None)
    except Exception:
        s = None
    if s:
        ask_upd('demo', qty(s), demo=True, city=s.get('city') or '', shown=s['number'])
    else:
        ask_upd('demo', 0, demo=True)


def demo_upd_yes():
    """Пример по ручному запуску: как будто подписант нажал ДА под последним примером вопроса.
    Если примера в состоянии нет — сначала задаёт его."""
    with slock:
        e = (load_state().get('upd') or {}).get('demo')
    if not e:
        demo_upd_question()
        with slock:
            e = (load_state().get('upd') or {}).get('demo')
    if not e:
        return
    with slock:
        st = load_state(); (st.get('upd') or {}).pop('demo', None); save_state(st)
    log.info('demo upd yes')
    upd_signed('demo', e, e['msg'])


def on_upd_answer(cq):
    """Нажали ДА/НЕТ. Отвечать могут только подписанты (если UPD_SIGNER не задан — люди из ALLOWED_USER_IDS)."""
    msg = cq.get('message') or {}
    ok = set(SIGNER_BY_ID) or USERS
    if ok and cq.get('from', {}).get('id') not in ok:
        who = cq.get('from', {}).get('id')
        tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': NOT_SIGNER_OWNER if who in OWNERS else NOT_SIGNER, 'show_alert': True})
        return
    try:
        _, num, ans = cq.get('data', '').split(':', 2)
    except ValueError:
        tg('answerCallbackQuery', {'callback_query_id': cq['id']}); return
    with slock:
        st = load_state(); u = st.get('upd') or {}; e = u.get(num)
        if e is not None:
            if ans == 'y':
                u.pop(num)
            else:
                e['next'] = next_ask(msk_now()).isoformat()
            st['upd'] = u; save_state(st)
    base = upd_question(num, e.get('q') if e else None, bool(e and e.get('demo')), (e or {}).get('c') or '', (e or {}).get('n'))
    if e is None:
        tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': 'Уже отмечено'})
        tg('editMessageReplyMarkup', {'chat_id': CHAT_ID, 'message_id': msg.get('message_id'), 'reply_markup': {'inline_keyboard': []}})
        return
    log.info('upd answer')
    if ans == 'y':
        tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': '✅'})
        upd_signed(num, e, msg.get('message_id'))
    else:
        if e.get('demo'):
            tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': 'В примере не переспрашиваю'})
            tg('editMessageText', {'chat_id': CHAT_ID, 'message_id': msg.get('message_id'), 'text': base + '\n❌ пока нет'})
            return
        nxt = datetime.datetime.fromisoformat(e['next'])
        when = nxt.strftime('в %H:%M') if nxt.date() == msk_now().date() else nxt.strftime('завтра в %H:%M')
        tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': f'Напомню {when}'})
        tg('editMessageText', {'chat_id': CHAT_ID, 'message_id': msg.get('message_id'), 'text': base + f'\n❌ пока нет · напомню {when}'})


def scheduler():
    hh, mm = map(int, DAILY.split(':'))
    while True:
      try:
        now = msk_now(); due = False
        with slock:
            st = load_state()
            if (now.hour, now.minute) >= (hh, mm) and st.get('daily') != str(now.date()):
                st['daily'] = str(now.date()); save_state(st); due = True
        if due:
            log.info('daily report')
            send_dashboard(final_day=now.date() - datetime.timedelta(1))
        check_upd()
      except Exception as e:
        log.error('scheduler: %s', type(e).__name__)
      time.sleep(30)


# ---------- поставки ----------
# этапы поставки по порядку: шкала 🟩🟩🟩⬜⬜⬜⬜ показывает, сколько пройдено
SUPPLY_STEPS = ['DATA_FILLING', 'READY_TO_SUPPLY', 'ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT',
                'ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING', 'COMPLETED']
SUPPLY_RU = {'DATA_FILLING': 'заполняется', 'READY_TO_SUPPLY': 'готова к отгрузке',
             'ACCEPTED_AT_SUPPLY_WAREHOUSE': 'принята в точке отгрузки', 'IN_TRANSIT': 'едет на склад',
             'ACCEPTANCE_AT_STORAGE_WAREHOUSE': 'приёмка на складе', 'REPORTS_CONFIRMATION_AWAITING': 'ждёт подтверждения актов',
             'COMPLETED': 'принята на складе', 'REPORT_REJECTED': 'акт отклонён',
             'REJECTED_AT_SUPPLY_WAREHOUSE': 'отказ в точке отгрузки', 'CANCELLED': 'отменена', 'OVERDUE': 'просрочена'}
SUPPLY_ICON = {'DATA_FILLING': '📝', 'READY_TO_SUPPLY': '📦', 'ACCEPTED_AT_SUPPLY_WAREHOUSE': '🏭', 'IN_TRANSIT': '🚚',
               'ACCEPTANCE_AT_STORAGE_WAREHOUSE': '🔍', 'REPORTS_CONFIRMATION_AWAITING': '📄', 'COMPLETED': '✅',
               'REPORT_REJECTED': '⚠️', 'REJECTED_AT_SUPPLY_WAREHOUSE': '⛔', 'CANCELLED': '❌', 'OVERDUE': '⌛'}
SUPPLY_FAIL_AT = {'REJECTED_AT_SUPPLY_WAREHOUSE': 3, 'REPORT_REJECTED': 6}  # на каком этапе застряла
dm = lambda t: t.strftime('%d.%m %H:%M') if t else '—'
ru = lambda st: SUPPLY_RU.get(st, html.escape(str(st)))
qty = lambda s: sum(q for _, q, _ in s['items'])


def bar(st):
    n = len(SUPPLY_STEPS)
    if st in SUPPLY_STEPS:
        i = SUPPLY_STEPS.index(st) + 1
        return '🟩' * i + '⬜' * (n - i)
    if st in SUPPLY_FAIL_AT:
        i = SUPPLY_FAIL_AT[st]
        return '🟩' * (i - 1) + '🟥' + '⬜' * (n - i)
    return '🟥' * n


def coming_of(S):
    c = collections.Counter()
    for s in S:
        if s['state'] != 'COMPLETED':
            for k, q, _ in s['items']:
                c[k] += q
    return c


def supply_block(num, st, q=None, changed=False, prev=None, city=''):
    """Карточка поставки, каждая строка короткая, чтобы на телефоне ничего не переносилось:
    №5642 → город / шкала и штуки / статус / («было: …»). changed: отметить 🔔 и выделить статус."""
    e = html.escape
    return ([f'{SUPPLY_ICON.get(st, "📦")} <b>№{e(sn(num))}' + (f' → {e(city)}' if city else '') + '</b>' + (' 🔔' if changed else ''),
             bar(st) + (f' <b>{q} шт</b>' if q else ''),
             f'<b>{ru(st)}</b>' if changed else ru(st)]
            + (['<i>новая поставка</i>' if prev is None else f'<i>было: {ru(prev)}</i>'] if changed else []))


BEFORE_DROP = ('DATA_FILLING', 'READY_TO_SUPPLY')
ON_THE_WAY = ('ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT')
eta_cache = {'t': 0, 'days': None}


def transit_days():
    """Сколько дней кросс-док поставка обычно едет до склада: сначала по тому, что бот сам видел,
    иначе по истории завершённых поставок из Ozon, иначе 3 дня."""
    seen = load_state().get('arrive_days') or []
    if len(seen) >= 2:
        return statistics.median(seen)
    if eta_cache['days'] is None or time.time() - eta_cache['t'] > 12 * 3600:
        try:
            h = transit_history()
            eta_cache.update(t=time.time(), days=statistics.median(h + seen) if h + seen else 3.0)
        except Exception as e:
            log.warning('transit history: %s', type(e).__name__)
            return eta_cache['days'] or 3.0
    return eta_cache['days']


def dur(td):
    m = int(td.total_seconds() // 60)
    if m < 60:
        return 'меньше часа'
    d, h = divmod(m // 60, 24)
    return f'{d} д {h} ч' if d else f'{h} ч'


def timing(s, now, tdays):
    """Одна строка про сроки: когда сдать, сколько уже едет и когда приедет.
    Дата приезда — плановая из Ozon (по коробкам); если её нет, оценка по прошлым поставкам с «~»."""
    if s.get('eta'):
        eta, mark = s['eta'], ''
    elif s['slot'] and s.get('crossdock') and tdays:
        eta, mark = (s['slot'] + datetime.timedelta(days=tdays)).date(), '~'
    else:
        eta = None
    when = lambda d: f'<b>{mark}{d:%d.%m}</b>'
    if s['state'] in BEFORE_DROP and s['slot']:
        return f'сдать {dm(s["slot"])}' + (f' · приедет {when(eta)}' if eta else '')
    if s['state'] in ON_THE_WAY:
        start = s['updated'] or s['slot']  # когда сдали в точку / когда выехала
        ago = dur(now - start) if start and now > start else ''
        if s['state'] == 'ACCEPTED_AT_SUPPLY_WAREHOUSE':
            t = f'сдали {ago} назад' if ago else 'сдали'
        else:
            t = f'в пути {ago}' if ago else 'в пути'
        if eta:
            t += f' · приедет {when(eta)}' if eta >= now.date() else f' · опаздывает, план был {when(eta)}'
        if 0 < s.get('boxes_arrived', 0) < s.get('boxes', 0):
            t += f'\nприехало {s["boxes_arrived"]} из {s["boxes"]} коробок'
        return t
    if s['state'] == 'ACCEPTANCE_AT_STORAGE_WAREHOUSE' and s['updated']:
        return f'на складе с {dm(s["updated"])}'
    return None


def supplies_text(S, changed=None, gone=()):
    """changed: {order_id: прежний статус или None для новой}; gone: ушедшие из работы (приняты, отменены)."""
    e = html.escape; changed = changed or {}
    act = [s for s in S if s['state'] != 'COMPLETED']
    L = []
    for g in gone:  # первая строка — то, что видно в уведомлении
        L += [f'🔔 <b>№{e(sn(g["number"]))}: {ru(g["state"])}</b>']
    for s in act:
        if s['id'] in changed:
            L += [f'🔔 <b>№{e(sn(s["number"]))}: {ru(s["state"])}</b>']
    for g in gone:
        L += [''] + supply_block(g['number'], g['state'], g['qty'], True, g['prev'], g.get('city'))
    if L:
        L.append('')
    L.append(f'🚚 <b>Поставки в работе: {len(act)}</b>' if act else '🚚 <b>Поставок в работе нет</b>')
    now = msk_now()
    tdays = transit_days() if any(s.get('crossdock') and not s.get('eta') and s['state'] in BEFORE_DROP + ON_THE_WAY for s in act) else 0
    for s in act:
        B = supply_block(s['number'], s['state'], qty(s), s['id'] in changed, changed.get(s['id']), s.get('city'))
        t = timing(s, now, tdays)
        L += [''] + B + ([t] if t else [])
    return '\n'.join(L)


def check_supplies():
    """Сравнивает статусы поставок с прошлой проверкой; при изменении шлёт сообщение о поставках с отметкой 🔔."""
    if msk_now().hour < QUIET_UNTIL:  # ночью не пишем: изменения придут утром одним сообщением
        return
    S = supplies(done_max=0)
    cur = {str(s['id']): s for s in S}
    with slock:
        st = load_state()
    first = 'supplies' not in st
    old = st.get('supplies') or {}
    new = {i: {'n': s['number'], 's': s['state'], 'q': qty(s), 'c': s.get('city') or ''} for i, s in cur.items()}
    changed = {s['id']: (old[i]['s'] if i in old else None) for i, s in cur.items() if i not in old or old[i]['s'] != s['state']}
    gone, lost = [], [i for i in old if i not in cur]
    if lost:
        now_st = supply_states([int(i) for i in lost])
        for i in lost:
            g = now_st.get(int(i))
            if g is None or g in SUPPLY_ACTIVE:  # сбой выдачи списка: не шумим, проверим в следующий раз
                new[i] = old[i]
            else:
                gone.append({'number': old[i]['n'], 'state': g, 'qty': old[i]['q'], 'prev': old[i]['s'], 'city': old[i].get('c') or ''})
    arrived = [(msk_now() if not s['updated'] else s['updated']) - s['slot'] for i, s in cur.items()
               if s.get('crossdock') and s['slot'] and i in old and old[i]['s'] in ON_THE_WAY
               and s['state'] in ('ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING')]
    if not first and (changed or gone):
        log.info('supply status changed')
        if not tg('sendMessage', {'chat_id': CHAT_ID, 'text': supplies_text(S, changed, gone), 'parse_mode': 'HTML'}):
            return  # не дошло: состояние не сохраняем, повторим через 15 минут
        for g in gone:
            if g['state'] == 'COMPLETED':
                ask_upd(g['number'], g['qty'], city=g['city'])
    with slock:
        st = load_state(); st['supplies'] = new
        if arrived:
            st['arrive_days'] = ((st.get('arrive_days') or []) + [round(a.total_seconds() / 86400, 2) for a in arrived])[-10:]
        save_state(st)


def send_demo_acceptance():
    """Разовый пример по ручному запуску workflow: как придёт сообщение, когда поставки дойдут до приёмки.
    Состояние не меняет, настоящие уведомления идут своим чередом."""
    try:
        S = supplies(done_max=0)
        act = [s for s in S if s['state'] != 'COMPLETED']
        for s in act:
            s['state'] = 'ACCEPTANCE_AT_STORAGE_WAREHOUSE'
        text = supplies_text(S, {s['id']: 'IN_TRANSIT' for s in act})
    except Exception as e:
        log.error('demo failed: %s', type(e).__name__)
        return
    log.info('demo message')
    tg('sendMessage', {'chat_id': CHAT_ID, 'parse_mode': 'HTML',
                       'text': '🧪 <i>Пример, статусы выдуманы: так придёт сообщение, когда поставки дойдут до приёмки на складе.</i>\n\n' + text})


def send_supplies():
    with lock:
        if not (scache['text'] and time.time() - scache['t'] < COOLDOWN):
            try:
                text = supplies_text(supplies(done_max=0))
            except Exception as e:
                log.error('supplies failed: %s', type(e).__name__)
                tg('sendMessage', {'chat_id': CHAT_ID, 'text': 'Не получилось забрать поставки из Ozon, попробуйте через пару минут.'})
                return
            scache.update(t=time.time(), text=text)
        text = scache['text']
    tg('sendMessage', {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'})


# ---------- тревоги ----------
def usual(base, a, b):
    """Сколько заказов обычно бывает между a и b: средние по часам за 7 прошлых дней."""
    s, t = 0.0, a
    while t < b:
        nxt = min(b, t.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1))
        s += base[t.hour] * (nxt - t).total_seconds() / 3600
        t = nxt
    return s


def check_alerts():
    W = watch(); now = W['now']
    if now.hour < QUIET_UNTIL:
        return
    new_zero, new_low, gap, jumps = [], [], None, []
    try:
        P = prices()
    except Exception as e:
        log.warning('prices: %s', type(e).__name__); P = None
    with slock:
        st = load_state()
        first = 'alerts' not in st
        al = st.get('alerts') or {}
        zero, low = set(al.get('zero', [])), set(al.get('low', []))
        for k in set(W['avg7']) | zero | low:
            n, avg = W['stock'].get(k, 0), W['avg7'].get(k, 0)
            cover = n / avg if avg else float('inf')
            if n == 0:
                if avg and k not in zero:
                    new_zero.append(k)
                if avg:
                    zero.add(k); low.add(k)
            else:
                zero.discard(k)
                if cover < LOW_DAYS:
                    if k not in low:
                        new_low.append((k, n, cover))
                    low.add(k)
                elif cover >= LOW_RESET:
                    low.discard(k)
        L = W['last']
        day0 = now.replace(hour=DAY_FROM, minute=0, second=0, microsecond=0)
        start = max(L, day0) if L else day0
        if DAY_FROM <= now.hour < DAY_TO and now - start >= datetime.timedelta(hours=GAP_HOURS) and al.get('gap') != start.isoformat():
            gap = (L, usual(W['base'], start, now)); al['gap'] = start.isoformat()
        if P is not None:  # цены: сравниваем с последней известной (после тревоги она становится новой точкой отсчёта)
            base = al.get('price')
            if base is None:
                al['price'] = P  # первый раз запоминаем молча
            else:
                for k, v in P.items():
                    b = base.get(k)
                    if b and abs(v - b) / b >= PRICE_JUMP:
                        jumps.append((k, b, v)); base[k] = v
                    elif not b:
                        base[k] = v
                al['price'] = base
        al['zero'], al['low'] = sorted(zero), sorted(low)
        st['alerts'] = al
        save_state(st)
    if first:  # первый запуск: запоминаем текущее состояние молча, пишем только об изменениях
        log.info('alerts initialised')
        return
    if new_zero or new_low:
        try:
            coming = coming_of(supplies(done_max=0))
        except Exception:
            coming = {}
        e = lambda k: html.escape(NAMES.get(k, k))
        go = lambda k: f' · едет {coming[k]} шт' if coming.get(k) else ''
        T = ['📦 <b>Склад</b>']
        T += [f'Закончился: <b>{e(k)}</b>{go(k)}' for k in sorted(new_zero, key=e)]
        T += [f'Мало: <b>{e(k)}</b> · {n} шт, ~{c:.0f} дн{go(k)}' for k, n, c in sorted(new_low, key=lambda x: x[2])]
        log.info('stock alert')
        tg('sendMessage', {'chat_id': CHAT_ID, 'text': '\n'.join(T), 'parse_mode': 'HTML'})
    if gap:
        L, u = gap
        if L:
            m = int((now - L).total_seconds() // 60)
            since = L.strftime('в %H:%M') if L.date() == now.date() else L.strftime('%d.%m в %H:%M')
            t = f'⏱ Уже <b>{m // 60} ч {m % 60} мин</b> нет заказов, последний был {since}.'
        else:
            t = '⏱ Уже 3+ часа нет заказов.'
        if u >= 1:
            t += f' Обычно за это время бывает около {round(u)}.'
        log.info('gap alert')
        tg('sendMessage', {'chat_id': CHAT_ID, 'parse_mode': 'HTML', 'text': t})
    if jumps:
        e = lambda k: html.escape(NAMES.get(k, k))
        T = ['💸 <b>Цена изменилась</b>']
        T += [f'{e(k)}: {fmt(b)} → <b>{fmt(v)} ₽</b> ({"+" if v > b else "−"}{abs(v - b) / b * 100:.0f}%)' for k, b, v in sorted(jumps, key=lambda x: (x[2] - x[1]) / x[1])]
        log.info('price alert')
        tg('sendMessage', {'chat_id': CHAT_ID, 'parse_mode': 'HTML', 'text': '\n'.join(T)})


def watcher():
    time.sleep(90)
    while True:
        try:
            check_alerts()
        except Exception as e:
            log.error('watch: %s', type(e).__name__)
        try:
            check_supplies()
        except Exception as e:
            log.error('supplies watch: %s', type(e).__name__)
        time.sleep(WATCH_EVERY)


def is_command(text):
    """Имя действия ('now', 'supply') или None."""
    if not text or not text.startswith('/'):
        return None
    w = text.split()[0].lower()
    if '@' in w:
        w, at = w.split('@', 1)
        if at != ME.lower():
            return None
    return COMMANDS.get(w)


def handle(u):
    cq = u.get('callback_query')
    if cq:  # кнопки ДА/НЕТ под вопросом про УПД
        if (cq.get('message') or {}).get('chat', {}).get('id') == CHAT_ID and cq.get('data', '').startswith('upd:'):
            on_upd_answer(cq)
        else:
            tg('answerCallbackQuery', {'callback_query_id': cq['id']})
        return
    mcm = u.get('my_chat_member')
    if mcm:
        if mcm['chat']['id'] == CHAT_ID and mcm['new_chat_member']['status'] == 'administrator':
            log.error('bot became admin, stopping')
            os._exit(1)
        if mcm['chat']['id'] != CHAT_ID and mcm['new_chat_member']['status'] in ('member', 'administrator'):
            log.warning('added to foreign chat, leaving')
            tg('leaveChat', {'chat_id': mcm['chat']['id']})
        return
    m = u.get('message')
    if not m:
        return
    chat = m['chat']['id']
    if chat != CHAT_ID:
        if m['chat']['type'] != 'private':
            tg('leaveChat', {'chat_id': chat})
        return  # чужим ничего не отвечаем
    if USERS and m.get('from', {}).get('id') not in USERS:
        return
    rep = m.get('reply_to_message') or {}
    is_reply_to_me = rep.get('from', {}).get('username', '').lower() == ME.lower()
    text = m.get('text', '')
    act = is_command(text) or (WORDS.get(text.strip(' /!.').lower()) if is_reply_to_me else None)
    if act:
        log.info('command from allowed chat')
        threading.Thread(target=send_supplies if act == 'supply' else send_dashboard, daemon=True).start()


def main():
    global ME
    me = tg('getMe')
    if not me:
        sys.exit('Неверный TELEGRAM_BOT_TOKEN или нет связи с Telegram')
    ME = me['username']
    if me.get('can_read_all_group_messages'):
        sys.exit('Privacy mode выключен: бот видел бы всю переписку. @BotFather → /setprivacy → Enable')
    join_warning = bool(me.get('can_join_groups'))
    if join_warning:
        log.warning('can_join_groups is on')
    chat = tg('getChat', {'chat_id': CHAT_ID})
    if not chat:
        sys.exit('Бот не состоит в группе ALLOWED_CHAT_ID')
    if chat.get('username'):
        sys.exit('Группа публичная (есть @адрес): сделайте её частной')
    mine = tg('getChatMember', {'chat_id': CHAT_ID, 'user_id': me['id']}) or {}
    if mine.get('status') == 'administrator':
        sys.exit('Бот — админ группы и получает всю переписку: снимите админку')
    tg('deleteWebhook', {'drop_pending_updates': False})  # команды, пришедшие между запусками, не теряем
    tg('setMyCommands', {'commands': [{'command': 'now', 'description': 'Дашборд Ozon сейчас'},
                                      {'command': 'supply', 'description': 'Поставки на склады Ozon'}],
                         'scope': {'type': 'chat', 'chat_id': CHAT_ID}})
    if join_warning:  # предупреждаем в саму группу (не в публичный лог), не чаще раза в день
        today = str(msk_now().date())
        with slock:
            st = load_state(); warn = st.get('join_warned') != today
            if warn:
                st['join_warned'] = today; save_state(st)
        if warn:
            tg('sendMessage', {'chat_id': CHAT_ID, 'text': '⚠️ Бота сейчас можно добавить в любую группу. Закройте это: @BotFather → /setjoingroups → выбрать бота → Disable.'})
    if os.environ.get('DEMO') == 'supply_acceptance':
        send_demo_acceptance()
    if os.environ.get('DEMO') == 'upd':
        demo_upd_question()
    if os.environ.get('DEMO') == 'upd_yes':
        demo_upd_yes()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    log.info('bot started')  # имя бота в лог не пишем: логи Actions публичные
    off = None
    end = time.time() + SERVE_SECONDS if SERVE_SECONDS else None
    while end is None or time.time() < end:
        res = tg('getUpdates', {'timeout': 50, 'offset': off, 'allowed_updates': ['message', 'my_chat_member', 'callback_query']})
        if res is None:
            time.sleep(5); continue
        for u in res:
            off = u['update_id'] + 1
            try:
                handle(u)
            except Exception as e:
                log.error('handle: %s', type(e).__name__)
    if off:
        tg('getUpdates', {'offset': off, 'timeout': 0})  # подтверждаем обработанные, чтобы следующий запуск не повторил
    time.sleep(40)  # даём дорисоваться начатым ответам
    log.info('run finished')


if __name__ == '__main__':
    if sys.argv[1:3] == ['test', 'supply']:  # python3 bot.py test supply → текст /supply, без Telegram
        print(supplies_text(supplies(done_max=0)))
    elif len(sys.argv) > 1 and sys.argv[1] == 'test':  # python3 bot.py test [final] → картинка в test.png, без Telegram
        fd = (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK).date() - datetime.timedelta(1) if 'final' in sys.argv else None
        png, cap = build(fd); open('test.png', 'wb').write(png); print(cap)
    else:
        main()
