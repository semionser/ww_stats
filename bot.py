"""Телеграм-бот «Ozon сегодня».

Команды в разрешённой группе: /now, /Now, /щас — дашборд, под ним кнопки «Сегодня · 7 дней · 30 дней»
(та же картинка меняется на месте); /supply, /поставки — поставки на склады Ozon
(также с @имя_бота). Каждый день в DAILY_TIME (МСК) присылает итог за вчера, по понедельникам — итог недели,
1-го числа — итог месяца. Пишет, когда Ozon отправил выплату, а в 22:00 — если за день был рекорд или веха (records.py).
В подписях к дашбордам рекордов нет, только отдельными сообщениями.
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
import collections, datetime, hashlib, hmac, html, json, re, statistics, logging, os, sys, tempfile, threading, time, urllib.request, urllib.error, uuid

_missing = [k for k in ('TELEGRAM_BOT_TOKEN', 'ALLOWED_CHAT_ID', 'OZON_CLIENT_ID', 'OZON_API_KEY', 'PRODUCTS_JSON') if not os.environ.get(k)]
if _missing and sys.argv[1:2] != ['test']:
    sys.exit('Не заданы секреты: ' + ', '.join(_missing))

import records
from ozon_data import collect, collect_period, collect_summary, call as ozon_call, MSK, NAMES, watch, supplies, supply_states, transit_history, prices, SUPPLY_ACTIVE
from widget import widget_html, top_html
from render import to_png

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = int(os.environ['ALLOWED_CHAT_ID'])
USERS = {int(x) for x in os.environ.get('ALLOWED_USER_IDS', '').replace(' ', '').split(',') if x}
DAILY = os.environ.get('DAILY_TIME', '09:00')
EVENING = (22, 0)  # в это время одним сообщением — рекорды дня и вехи, если были
STATE = os.environ.get('STATE_FILE', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state', 'state.json'))
SERVE_SECONDS = float(os.environ.get('SERVE_SECONDS', 0))  # 0 = без ограничения; в GitHub Actions ~5 ч 50 мин
COMMANDS = {'/now': 'now', '/щас': 'now', '/now_week': 'week', '/неделя': 'week', '/now_month': 'month', '/месяц': 'month',
            '/supply': 'supply', '/поставки': 'supply'}
# ответом на сообщение бота: такие ответы Telegram доставляет и в privacy mode
WORDS = {'щас': 'now', 'ща': 'now', 'сейчас': 'now', 'now': 'now', 'неделя': 'week', 'week': 'week',
         'месяц': 'month', 'month': 'month', 'поставки': 'supply', 'supply': 'supply'}
PERIOD_DAYS = {'week': 7, 'month': 30}  # кнопки под /now: последние 7 и 30 дней, включая сегодня
PERIOD_LABEL = {'now': 'Сегодня', 'week': '7 дней', 'month': '30 дней', 'top': 'Топ'}  # «Топ» — лучшие дни за всё время
WATCH_EVERY = 15 * 60          # как часто проверять склад и паузу в заказах, сек.
LOW_DAYS, LOW_RESET = 5, 7     # тревога, когда запас < 5 дн; снова следим, когда поднялся до 7+
GAP_HOURS = 3                  # тревога, если днём 3+ ч нет ни одного заказа
DAY_FROM, DAY_TO = 10, 22      # «днём» = с 10:00 до 22:00 МСК; паузу до 10:00 не считаем
PRICE_JUMP = 0.10              # тревога, если цена товара сдвинулась на 10%+ от последней известной
QUIET_UNTIL = 8                # с 00:00 до 08:00 МСК бот сам не пишет
COOLDOWN = 60  # сек.: чаще одного раза в минуту не дёргаем Ozon, повторяем последнюю картинку
DAILY_TRIES, DAILY_RETRY = 6, 600   # итог за вчера: если Ozon не ответил, ещё 5 попыток раз в 10 мин
OZON_DOWN_WARN = 3600               # Ozon не отвечает час подряд — один раз пишем в группу
TG_LIMIT = 4000                     # Telegram режет сообщения длиннее 4096 символов
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
lock = threading.Lock()   # /now: картинка рисуется по одной
sup_lock = threading.Lock()  # /supply — отдельно, чтобы не ждать картинку
slock = threading.Lock()  # state.json пишут несколько потоков
cache = {}  # период → {'t', 'png', 'caption'}: повтор команды в течение минуты не дёргает Ozon
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
        log.warning('telegram %s: HTTP %s%s', method, e.code, tg_reason(e))  # без URL: в нём токен
    except Exception as e:
        log.warning('telegram %s: %s', method, type(e).__name__)
    return None


def tg_reason(e):
    """Причина ошибки от Telegram для лога, без данных: текст в кавычках и все числа вырезаем
    (там могли бы оказаться куски подписи или id). Остаётся вроде «Bad Request: message to edit not found»."""
    try:
        d = json.loads(e.read()).get('description') or ''
    except Exception:
        return ''
    d = re.sub(r'\d+', '#', re.sub(r'"[^"]*"|\'[^\']*\'|<[^>]*>', '…', d))
    return f' ({d[:100]})' if d else ''


def tg_retry(method, data=None, tries=10):
    """Для старта: короткий сбой связи не должен ронять бота до следующего запуска (до 2 ч)."""
    for i in range(tries):
        r = tg(method, data)
        if r is not None:
            return r
        time.sleep(min(60, 5 * (i + 1)))
    return None


def send_text(text):
    """sendMessage (HTML). Длинный текст делим по пустым строкам на части, чтобы влезть в лимит Telegram.
    True, если дошли все части."""
    parts, cur = [], ''
    for block in text.split('\n\n'):
        if cur and len(cur) + 2 + len(block) > TG_LIMIT:
            parts.append(cur); cur = block
        else:
            cur = f'{cur}\n\n{block}' if cur else block
    parts.append(cur)
    return all([tg('sendMessage', {'chat_id': CHAT_ID, 'text': p, 'parse_mode': 'HTML'}) for p in parts])


fmt = lambda v: f'{v:,.0f}'.replace(',', ' ')


def caption(A, final):
    """Подпись под картинкой (Telegram HTML): баланс, начислено за день (или за период), заказы с разбивкой по группам и сумма."""
    s = A['summary']; pd = A.get('period')
    by_code = {r['code']: r['orders'] for r in A['items']}
    parts = [str(sum(by_code.get(c, 0) for c in g['codes'])) for g in A.get('caption_groups', [])]
    br = f' ({"/".join(parts)})' if parts else ''
    acc = ('+' if s['accrued'] > 0 else '') + fmt(s['accrued'])
    t = (f'Баланс: <b>{fmt(s["balance"])} ₽</b>\n'
         f'{"За " + pd.get("span", str(pd["n"]) + " дней") if pd else "За день"}: <b>{acc} ₽</b>\n'
         f'Заказы: <b>{s["orders"]}</b>{br} · <b>{fmt(s["orders_sum"])} ₽</b>')
    if final:
        t = f'<b>Итог за {s["updated"][:5]}</b>\n' + t
    if pd:
        dm = lambda d: d[8:] + '.' + d[5:7]
        t = f'<b>{pd.get("title", str(pd["n"]) + " дней")} · {dm(pd["from"])}–{dm(pd["to"])}</b>\n' + t
    return t


def render(A, final=False):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'ozon.png')
        to_png(widget_html(A, FINAL=final, wide=True), p, wide=True)
        return open(p, 'rb').read(), caption(A, final)


def build(final_day=None, period='now'):
    if period == 'top':
        return build_top()
    return render(collect_period(PERIOD_DAYS[period]) if period in PERIOD_DAYS else collect(final_day), bool(final_day))


def build_top():
    """Кнопка «Топ»: лучшие дни за всё время и место сегодняшнего. Нужна вся история заказов: если её ещё нет
    в памяти этой смены, сбор ~2 мин (в 22:00 она собирается всё равно, после этого — секунды)."""
    records.ensure_loaded()
    o, s, _ = records.today_totals()
    T = records.top(cur=(o, s))
    now = msk_now()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'ozon.png')
        to_png(top_html(T, f'{now:%d.%m} · {now:%H:%M} МСК'), p, wide=True)
        png = open(p, 'rb').read()
    f = lambda v: f'{v:,.0f}'.replace(',', ' ')
    bs, bo = T['by_sum'][0], T['by_orders'][0]
    place = f'{T["rank_sum"]} место по сумме' if T['rank_sum'] else ''
    cap = (f'<b>Топ дней · за всё время</b>\n'
           f'Сегодня: <b>{f(s)} ₽</b> · {records.orders_word(o)}' + (f' · {place}' if place else '') + '\n'
           f'Рекорд: <b>{f(bs[2])} ₽</b> ({bs[0]:%d.%m.%Y}) · <b>{records.orders_word(bo[1])}</b> ({bo[0]:%d.%m.%Y})\n'
           f'<i>Все заказы, вместе с отменёнными</i>')
    return png, cap


def dashboard(final_day=None, period='now', quiet=False):
    """(png, подпись) или None. Повтор в течение минуты берём из кэша, не дёргая Ozon."""
    with lock:
        c = cache.get(period)
        if not final_day and c and time.time() - c['t'] < COOLDOWN:
            return c['png'], c['caption']
        try:
            png, cap = build(final_day, period)
        except Exception as e:
            log.error('build failed: %s', type(e).__name__)
            if not quiet:
                tg('sendMessage', {'chat_id': CHAT_ID, 'text': 'Не получилось забрать данные Ozon, попробуйте через пару минут.'})
            return None
        if not final_day:
            cache[period] = {'t': time.time(), 'png': png, 'caption': cap}
        return png, cap


def period_kb(cur):
    """Кнопки под дашбордом; выбранная — в точках."""
    return json.dumps({'inline_keyboard': [[{'text': f'• {t} •' if p == cur else t, 'callback_data': f'dash:{p}'}
                                            for p, t in PERIOD_LABEL.items()]]})


def send_dashboard(final_day=None, quiet=False, period='now'):
    """True, если картинка дошла. quiet: при ошибке не писать в группу (итог за вчера сам повторит попытку).
    period: 'now' — сегодня, 'week'/'month' — последние 7/30 дней. Под ответом на команду — кнопки периодов,
    у итога за вчера их нет."""
    d = dashboard(final_day, period, quiet)
    if not d:
        return False
    data = {'chat_id': CHAT_ID, 'caption': d[1], 'parse_mode': 'HTML'}
    if not final_day:
        data['reply_markup'] = period_kb(period)
    r = tg('sendPhoto', data, files={'photo': ('ozon.png', d[0])})
    if r and not final_day:
        replace_now(r['message_id'])
    return bool(r)


def on_period(cq):
    """Нажали «Сегодня / 7 дней / 30 дней» под дашбордом: меняем картинку в том же сообщении."""
    period = cq.get('data', '')[5:]
    msg = cq.get('message') or {}
    if period not in PERIOD_LABEL or not msg.get('message_id') or (USERS and cq.get('from', {}).get('id') not in USERS):
        tg('answerCallbackQuery', {'callback_query_id': cq['id']}); return
    fresh = (cache.get(period) or {}).get('t', 0) > time.time() - COOLDOWN
    shown = next((b.get('callback_data', '')[5:] for row in (msg.get('reply_markup') or {}).get('inline_keyboard', [])
                  for b in row if b.get('text', '').startswith('•')), None)
    # повторное нажатие выбранной кнопки обновляет цифры; в первую минуту картинка та же, Telegram ответил бы ошибкой
    if period == shown and fresh:
        tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': 'Цифры свежие, обновлены меньше минуты назад'}); return
    wait = 'Собираю всю историю заказов, около 2 минут…' if period == 'top' and not records.H['ready'] else f'Собираю: {PERIOD_LABEL[period].lower()}…'
    tg('answerCallbackQuery', {'callback_query_id': cq['id'], 'text': '' if fresh else wait})
    if period == 'top':  # историю собираем до общего замка дашбордов, чтобы /now не ждал эти 2 минуты
        try:
            records.ensure_loaded()
        except Exception as e:
            log.error('top history: %s', type(e).__name__)
    log.info('period button: %s', 'ok' if edit_dashboard(msg['message_id'], period) else 'failed')


def edit_dashboard(message_id, period):
    """Меняем картинку и подпись в уже отправленном дашборде. Результат Telegram или None."""
    d = dashboard(period=period)
    if not d:
        return None
    return tg('editMessageMedia', {'chat_id': CHAT_ID, 'message_id': message_id, 'reply_markup': period_kb(period),
                                   'media': json.dumps({'type': 'photo', 'media': 'attach://photo', 'caption': d[1], 'parse_mode': 'HTML'})},
              files={'photo': ('ozon.png', d[0])})


def selftest_buttons():
    """Проверка кнопок без людей: тихо шлём дашборд, переключаем 7 дней → 30 дней → сегодня тем же кодом,
    что и кнопка, и удаляем."""
    d = dashboard()
    r = d and tg('sendPhoto', {'chat_id': CHAT_ID, 'caption': d[1], 'parse_mode': 'HTML', 'disable_notification': 'true',
                               'reply_markup': period_kb('now')}, files={'photo': ('ozon.png', d[0])})
    if not r:
        log.warning('selftest: send failed'); return
    for period in ('week', 'month', 'now'):
        log.info('selftest: %s %s', period, 'ok' if edit_dashboard(r['message_id'], period) else 'failed')
    tg('deleteMessage', {'chat_id': CHAT_ID, 'message_id': r['message_id']})


def replace_now(new_id):
    """Новый ответ на /now пришёл — прошлые удаляем, в группе остаётся один свежий дашборд.
    Итог за вчера в 9:00 не трогаем. Свои сообщения бот может удалять только первые 48 часов,
    более старые Telegram не даст удалить — их просто забываем."""
    with slock:
        st = load_state(); old = st.get('now_msgs') or []
        st['now_msgs'] = [new_id]; save_state(st)
    for mid in old:
        if mid != new_id:
            tg('deleteMessage', {'chat_id': CHAT_ID, 'message_id': mid})


# state.json лежит в кэше GitHub Actions, а кэш основной ветки может прочитать любой, кто откроет PR в публичный
# репозиторий. Поэтому файл зашифрован ключом из секретов: поток HMAC-SHA256(ключ, nonce‖счётчик) XOR текст,
# затем HMAC всего файла (encrypt-then-MAC). Без секретов из него не достать ни артикулы, ни цены, ни поставки.
# Сменили токен или ключ Ozon — старый файл не расшифруется, бот начнёт с чистого состояния (тревоги заново молча).
_SK = hashlib.sha256(b'ww-state:' + (os.environ.get('TELEGRAM_BOT_TOKEN', '') + '|' + os.environ.get('OZON_API_KEY', '')).encode()).digest()
_SK_ENC, _SK_MAC = hmac.new(_SK, b'enc', hashlib.sha256).digest(), hmac.new(_SK, b'mac', hashlib.sha256).digest()
_MAGIC = b'WWS1'


def _stream(nonce, n):
    return b''.join(hmac.new(_SK_ENC, nonce + i.to_bytes(8, 'big'), hashlib.sha256).digest() for i in range((n + 31) // 32))[:n]


def seal(data):
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(nonce, len(data))))
    return _MAGIC + nonce + hmac.new(_SK_MAC, _MAGIC + nonce + ct, hashlib.sha256).digest() + ct


def unseal(blob):
    if blob[:1] == b'{':  # старый незашифрованный файл: прочитаем один раз, дальше сохранится зашифрованным
        return blob
    if blob[:4] != _MAGIC or len(blob) < 52:
        raise ValueError('state format')
    nonce, tag, ct = blob[4:20], blob[20:52], blob[52:]
    if not hmac.compare_digest(tag, hmac.new(_SK_MAC, _MAGIC + nonce + ct, hashlib.sha256).digest()):
        raise ValueError('state tag')
    return bytes(a ^ b for a, b in zip(ct, _stream(nonce, len(ct))))


def load_state():
    try:
        return json.loads(unseal(open(STATE, 'rb').read()))
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(seal(json.dumps(st).encode()))
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


# ---------- итоги по расписанию ----------
# по порядку: итог недели — после итога за вчера, итог месяца — после итога недели
JOBS = [('daily', lambda d: True, 'Итог за вчера'),
        ('weekly', lambda d: d.weekday() == 0, 'Итог недели'),
        ('monthly', lambda d: d.day == 1, 'Итог месяца')]


def send_summary(kind, today, demo=False):
    """Итог прошлой недели (kind='week') или прошлого месяца ('month') картинкой, как дашборд, без кнопок.
    Если неделя (месяц) лучшая за всё время или давно не было такой — следом отдельное сообщение: в подписи рекордов нет."""
    with lock:
        try:
            A = collect_summary(kind, today)
            png, cap = render(A)
        except Exception as e:
            log.error('summary failed: %s', type(e).__name__)
            return False
    pd = A['period']
    try:
        records.ensure_loaded()
        lines = records.period_lines(kind, datetime.date.fromisoformat(pd['from']), datetime.date.fromisoformat(pd['to']))
    except Exception as e:
        lines = []; log.error('records: %s', type(e).__name__)
    if demo:
        cap = '🧪 <i>Пример: так итог придёт сам</i>\n' + cap
    ok = bool(tg('sendPhoto', {'chat_id': CHAT_ID, 'caption': cap, 'parse_mode': 'HTML'}, files={'photo': ('ozon.png', png)}))
    if ok and lines:
        tg('sendMessage', {'chat_id': CHAT_ID, 'text': '\n'.join(lines + [records.NOTE]), 'parse_mode': 'HTML'})
    return ok


def run_job(key, today):
    if key == 'daily':
        return send_dashboard(final_day=today - datetime.timedelta(1), quiet=True)
    return send_summary('week' if key == 'weekly' else 'month', today)


def scheduler():
    hh, mm = map(int, DAILY.split(':'))
    tries = {}  # попытки итогов в этом запуске: ключ → {'day', 'n', 't'}
    while True:
      try:
        now = msk_now(); today = str(now.date())
        if (now.hour, now.minute) >= (hh, mm):
            for key, due, name in JOBS:
                if not due(now.date()):
                    continue
                with slock:
                    if load_state().get(key) == today:
                        continue
                tr = tries.get(key)
                if not tr or tr['day'] != today:
                    tr = tries[key] = {'day': today, 'n': 0, 't': 0.0}
                if time.time() - tr['t'] >= DAILY_RETRY:
                    tr['n'] += 1; tr['t'] = time.time()
                    last = tr['n'] >= DAILY_TRIES
                    log.info('%s report, try %s', key, tr['n'])
                    ok = run_job(key, now.date())
                    if ok or last:
                        with slock:
                            st = load_state(); st[key] = today; save_state(st)
                    if not ok and last:
                        tg('sendMessage', {'chat_id': CHAT_ID, 'text': f'⚠️ {name} не собрался: Ozon не отвечал целый час.'})
                break  # следующий итог — только когда этот отправлен
        if (now.hour, now.minute) >= EVENING:
            evening(today, tries)
        check_upd()
      except Exception as e:
        log.error('scheduler: %s', type(e).__name__)
      time.sleep(30)


# ---------- выплаты, рекорды ----------
def seen(key):
    """True, если об этом уже писали; иначе запоминаем (в state — только хэш)."""
    h = records.mark(key)
    with slock:
        st = load_state(); ms = st.get('marks') or []
        if h in ms:
            return True
        st['marks'] = (ms + [h])[-300:]; save_state(st)
    return False


_paycheck = {'t': 0.0}


def check_payouts():
    """Ozon перевёл деньги на счёт: в балансе за день появляются «выплаты» (отрицательное число).
    Смотрим раз в час вчера и сегодня, чтобы ночная выплата пришла утром."""
    now = msk_now()
    if now.hour < QUIET_UNTIL or time.time() - _paycheck['t'] < 3600:
        return
    _paycheck['t'] = time.time()
    found = []
    for d in (now.date() - datetime.timedelta(1), now.date()):
        b = ozon_call('/v1/finance/balance', {"date_from": str(d), "date_to": str(d)})
        pays = b['total'].get('payments') or []
        pays = [pays] if isinstance(pays, dict) else pays
        paid = round(-sum(float(p.get('value') or 0) for p in pays))
        if paid > 0:
            found.append((d, paid))
    for d, paid in found:
        if not seen(f'pay:{d}:{paid}'):
            tg('sendMessage', {'chat_id': CHAT_ID, 'parse_mode': 'HTML', 'text': payout_text(paid, d if d != now.date() else None)})


def payout_text(paid, day=None):
    return f'💸 Ozon отправил выплату <b>{fmt(paid)} ₽</b>' + (f' ({day:%d.%m})' if day else '')


def evening(today, tries):
    """Раз в день в 22:00: рекорды дня и вехи одним сообщением. Ничего не было — молчим.
    Ozon не ответил — ещё 2 попытки раз в 10 минут, потом до завтра."""
    with slock:
        if load_state().get('evening') == today:
            return
    tr = tries.get('evening')
    if not tr or tr['day'] != today:
        tr = tries['evening'] = {'day': today, 'n': 0, 't': 0.0}
    if time.time() - tr['t'] < DAILY_RETRY:
        return
    tr['n'] += 1; tr['t'] = time.time()
    try:
        records.ensure_loaded()
        lines = [text for key, text in records.live() if not seen(key)]
        if lines:
            tg('sendMessage', {'chat_id': CHAT_ID, 'text': '\n'.join(lines + [records.NOTE]), 'parse_mode': 'HTML'})
        done = True
    except Exception as e:
        log.error('evening: %s', type(e).__name__); done = tr['n'] >= 3
    if done:
        with slock:
            st = load_state(); st['evening'] = today; save_state(st)


def demo_records():
    """Как будут выглядеть сообщения о рекордах: на настоящих данных, где они есть."""
    records.ensure_loaded()
    S = records.status(); today = msk_now().date()
    (ro, rod), (rs, rsd) = S['orders'], S['sum']
    y = today - datetime.timedelta(1)
    ex = next(((y - datetime.timedelta(i), L) for i in range(60)
               for L in [[t for _, t in records.day_lines(y - datetime.timedelta(i), *records.history_day(y - datetime.timedelta(i)))]] if L),
              (None, []))  # последний день, когда было бы что написать
    mon = today - datetime.timedelta(today.weekday() + 7)  # прошлая полная неделя
    nxt = lambda v, step: int(v // step + 1) * step
    lines = ['🧪 <b>Так будут выглядеть рекорды</b>', '',
             '<i>В 22:00 одним сообщением, только если за день было что-то особенное (иначе бот молчит). '
             'В подписях к дашбордам рекордов нет.</i>',
             '', '<i>Рекорд дня за всё время:</i>',
             f'🏆 <b>Рекорд дня по сумме!</b> Уже {fmt(rs + 1300)} ₽ заказов. Прошлый рекорд {fmt(rs)} ₽, {records.ddate(rsd)}']
    if ex[0]:
        lines += ['', f'<i>Лучший день за долгое время и рекорды товаров (настоящие, за {ex[0]:%d.%m}):</i>'] + ex[1]
    lines += ['', '<i>Вехи (эти — ближайшие):</i>',
              f'🎉 <b>{fmt(nxt(S["total_sum"], records.STEP_SUM) // 1_000_000)} млн ₽</b> заказов за всё время! Первый заказ был {records.ddate(S["first"])}',
              f'🎉 <b>{fmt(nxt(S["total_orders"], records.STEP_ORDERS))}-й заказ</b> за всё время! Первый был {records.ddate(S["first"])}',
              '', f'<i>Отдельным сообщением после итога недели или месяца (настоящее, за неделю {mon:%d.%m}–{mon + datetime.timedelta(6):%d.%m}):</i>']
    lines += records.period_lines('week', mon, mon + datetime.timedelta(6)) or ['ничего особенного, строк нет']
    lines += ['', '<i>Вехи: каждые 1000 заказов и каждый миллион ₽ за всё время, каждые 500 000 ₽ заказов за месяц.</i>']
    send_text('\n'.join(lines))


def demo_payout():
    """Пример сообщения о выплате — по последней настоящей выплате за 60 дней."""
    today = msk_now().date()
    for i in range(60):
        d = today - datetime.timedelta(i)
        b = ozon_call('/v1/finance/balance', {"date_from": str(d), "date_to": str(d)})
        pays = b['total'].get('payments') or []
        pays = [pays] if isinstance(pays, dict) else pays
        paid = round(-sum(float(p.get('value') or 0) for p in pays))
        if paid > 0:
            tg('sendMessage', {'chat_id': CHAT_ID, 'parse_mode': 'HTML',
                               'text': f'🧪 <i>Пример, это выплата от {d:%d.%m}</i>\n' + payout_text(paid)})
            return


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
        if not send_text(supplies_text(S, changed, gone)):
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
        if not act:
            return
        s = act[0]  # как будто одна поставка только что доехала и встала на приёмку
        s['state'], s['updated'] = 'ACCEPTANCE_AT_STORAGE_WAREHOUSE', msk_now()
        text = supplies_text(S, {s['id']: 'IN_TRANSIT'})
    except Exception as e:
        log.error('demo failed: %s', type(e).__name__)
        return
    log.info('demo message')
    send_text('🧪 <i>Пример, статус выдуман</i>\n\n' + text)


def send_supplies():
    with sup_lock:
        if not (scache['text'] and time.time() - scache['t'] < COOLDOWN):
            try:
                text = supplies_text(supplies(done_max=0))
            except Exception as e:
                log.error('supplies failed: %s', type(e).__name__)
                tg('sendMessage', {'chat_id': CHAT_ID, 'text': 'Не получилось забрать поставки из Ozon, попробуйте через пару минут.'})
                return
            scache.update(t=time.time(), text=text)
        text = scache['text']
    send_text(text)


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


def ozon_health(ok):
    """Проверки тревог и поставок падают час подряд — один раз пишем в группу, что они сейчас не работают
    (истёк ключ Ozon, сбой API). Когда снова заработало — пишем и об этом."""
    now = msk_now(); say = None
    with slock:
        st = load_state(); h = st.get('ozon') or {}
        if ok:
            if h.get('warned'):
                say = '✅ Ozon снова отвечает: тревоги и поставки проверяются.'
            h = {}
        else:
            h.setdefault('since', now.isoformat())
            since = datetime.datetime.fromisoformat(h['since'])
            if not h.get('warned') and (now - since).total_seconds() >= OZON_DOWN_WARN and now.hour >= QUIET_UNTIL:
                h['warned'] = True
                say = (f'⚠️ С {since:%H:%M} не получается проверить Ozon: тревоги и поставки сейчас не приходят. '
                       'Если не пройдёт само, проверьте ключ API в кабинете Ozon.')
        st['ozon'] = h; save_state(st)
    if say:
        tg('sendMessage', {'chat_id': CHAT_ID, 'text': say})


def watcher():
    time.sleep(90)
    while True:
        ok = True
        try:
            check_alerts()
        except Exception as e:
            ok = False; log.error('watch: %s', type(e).__name__)
        try:
            check_supplies()
        except Exception as e:
            ok = False; log.error('supplies watch: %s', type(e).__name__)
        for f in (check_payouts,):  # не влияет на «Ozon не отвечает»: у выплат свой запрос
            try:
                f()
            except Exception as e:
                log.error('%s: %s', f.__name__, type(e).__name__)
        try:
            ozon_health(ok)
        except Exception as e:
            log.error('health: %s', type(e).__name__)
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
    if cq:  # кнопки ДА/НЕТ под вопросом про УПД и кнопки периодов под дашбордом
        mine = (cq.get('message') or {}).get('chat', {}).get('id') == CHAT_ID
        if mine and cq.get('data', '').startswith('upd:'):
            on_upd_answer(cq)
        elif mine and cq.get('data', '').startswith('dash:'):
            threading.Thread(target=on_period, args=(cq,), daemon=True).start()
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
        if act == 'supply':
            threading.Thread(target=send_supplies, daemon=True).start()
        else:
            threading.Thread(target=send_dashboard, kwargs={'period': act}, daemon=True).start()


def main():
    global ME
    me = tg_retry('getMe')
    if not me:
        sys.exit('Неверный TELEGRAM_BOT_TOKEN или нет связи с Telegram')
    ME = me['username']
    if me.get('can_read_all_group_messages'):
        sys.exit('Privacy mode выключен: бот видел бы всю переписку. @BotFather → /setprivacy → Enable')
    join_warning = bool(me.get('can_join_groups'))
    if join_warning:
        log.warning('can_join_groups is on')
    chat = tg_retry('getChat', {'chat_id': CHAT_ID})
    if not chat:
        sys.exit('Бот не состоит в группе ALLOWED_CHAT_ID')
    if chat.get('username'):
        sys.exit('Группа публичная (есть @адрес): сделайте её частной')
    mine = tg_retry('getChatMember', {'chat_id': CHAT_ID, 'user_id': me['id']}) or {}
    if mine.get('status') == 'administrator':
        sys.exit('Бот — админ группы и получает всю переписку: снимите админку')
    tg('deleteWebhook', {'drop_pending_updates': False})  # команды, пришедшие между запусками, не теряем
    # в меню только две команды; неделя и месяц — кнопками под дашбордом (/now_week, /now_month тоже понимает)
    tg('setMyCommands', {'commands': [{'command': 'now', 'description': 'Дашборд Ozon: сегодня, 7 или 30 дней'},
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
    demo = set((os.environ.get('DEMO') or '').split('+'))  # можно несколько: supply_acceptance+upd
    if 'supply_acceptance' in demo:
        send_demo_acceptance()
    if 'upd' in demo:
        demo_upd_question()
    if 'upd_yes' in demo:
        demo_upd_yes()
    if 'now' in demo:  # дашборд с кнопками периодов, как на /now
        send_dashboard()
    if 'summaries' in demo:  # итог прошлой полной недели и прошлого месяца, как придут сами
        today = msk_now().date()
        send_summary('week', today - datetime.timedelta(today.weekday()), demo=True)
        send_summary('month', today.replace(day=1), demo=True)
    if 'records' in demo:
        demo_records()
    if 'payout' in demo:
        demo_payout()
    if 'selftest' in demo:  # проверка кнопок периодов; с +exit бот сразу завершается, чтобы лог был виден
        selftest_buttons()
        if 'exit' in demo:
            log.info('run finished'); return
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
    elif len(sys.argv) > 1 and sys.argv[1] == 'test':  # python3 bot.py test [final|week|month] → картинка в test.png, без Telegram
        fd = (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK).date() - datetime.timedelta(1) if 'final' in sys.argv else None
        per = next((a for a in sys.argv[2:] if a in PERIOD_DAYS), 'now')
        png, cap = build(fd, per); open('test.png', 'wb').write(png); print(cap)
    else:
        main()
