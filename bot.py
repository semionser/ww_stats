"""Телеграм-бот «Ozon сегодня».

Команды в разрешённой группе: /now, /Now, /щас (также с @имя_бота).
Каждый день в DAILY_TIME (МСК) присылает итог за вчера.

Безопасность:
- работает только в одном чате (ALLOWED_CHAT_ID); из любых других чатов сразу выходит и ничего не отвечает;
- при включённом privacy mode (по умолчанию у BotFather) Telegram не присылает боту обычные сообщения группы;
  бот всё равно смотрит только на первое слово сообщения и нигде не сохраняет и не логирует текст;
- опционально отвечает только людям из ALLOWED_USER_IDS;
- только исходящие запросы (long polling), никаких открытых портов и вебхуков;
- ключи только из переменных окружения, в лог не пишутся.
"""
import datetime, json, logging, os, sys, tempfile, threading, time, urllib.request, urllib.error, uuid

_missing = [k for k in ('TELEGRAM_BOT_TOKEN', 'ALLOWED_CHAT_ID', 'OZON_CLIENT_ID', 'OZON_API_KEY', 'PRODUCTS_JSON') if not os.environ.get(k)]
if _missing and sys.argv[1:2] != ['test']:
    sys.exit('Не заданы секреты: ' + ', '.join(_missing))

from ozon_data import collect, MSK
from widget import widget_html
from render import to_png

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = int(os.environ['ALLOWED_CHAT_ID'])
USERS = {int(x) for x in os.environ.get('ALLOWED_USER_IDS', '').replace(' ', '').split(',') if x}
DAILY = os.environ.get('DAILY_TIME', '09:00')
STATE = os.environ.get('STATE_FILE', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state', 'state.json'))
SERVE_SECONDS = float(os.environ.get('SERVE_SECONDS', 0))  # 0 = без ограничения; в GitHub Actions ~5 ч 50 мин
COMMANDS = {'/now', '/щас'}
WORDS = {'щас', 'ща', 'сейчас', 'now'}  # ответом на сообщение бота: такие ответы Telegram доставляет и в privacy mode
COOLDOWN = 60  # сек.: чаще одного раза в минуту не дёргаем Ozon, повторяем последнюю картинку

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('bot')
# Любая непойманная ошибка пишется в лог только типом: текст исключения может содержать данные.
sys.excepthook = lambda t, v, tb: log.error('fatal: %s', t.__name__)
threading.excepthook = lambda a: log.error('thread error: %s', a.exc_type.__name__)
lock = threading.Lock()
cache = {'t': 0, 'png': None, 'caption': ''}
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
    s = A['summary']
    d = s['orders_sum'] - s['y_sum']
    head = f'Итог за {s["updated"][:5]}' if final else f'Ozon на {s["updated"]} МСК'
    cmp = ''
    if s['y_sum']:
        cmp = f', на {abs(d) / s["y_sum"] * 100:.0f}% {"больше" if d >= 0 else "меньше"}, чем ' + ('за предыдущий день' if final else 'вчера к этому времени')
    low = [f'{r["item"]} {r["stock"]} шт' for r in A['items'] if r['avg7'] >= 0.5 and r['stock'] <= max(3, r['avg7'] * 2)]
    t = (f'{head}\nЗаказы {s["orders"]} на {fmt(s["orders_sum"])} ₽{cmp}. {A["group_label"]}: {s["group"]}, отмен {s["cancelled"]}.\n'
         f'Начислено {"+" if s["accrued"] >= 0 else ""}{fmt(s["accrued"])} ₽, баланс {fmt(s["balance"])} ₽.')
    if low:
        t += '\nЗаканчиваются: ' + ', '.join(low) + '.'
    return t


def build(final_day=None):
    A = collect(final_day)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'ozon.png')
        to_png(widget_html(A, FINAL=bool(final_day)), p)
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
    tg('sendPhoto', {'chat_id': CHAT_ID, 'caption': cap}, files={'photo': ('ozon.png', png)})


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def scheduler():
    hh, mm = map(int, DAILY.split(':'))
    while True:
      try:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK
        st = load_state()
        if (now.hour, now.minute) >= (hh, mm) and st.get('daily') != str(now.date()):
            st['daily'] = str(now.date())
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            json.dump(st, open(STATE, 'w'))
            log.info('daily report')
            send_dashboard(final_day=now.date() - datetime.timedelta(1))
      except Exception as e:
        log.error('scheduler: %s', type(e).__name__)
      time.sleep(30)


def is_command(text):
    if not text or not text.startswith('/'):
        return False
    w = text.split()[0].lower()
    if '@' in w:
        w, at = w.split('@', 1)
        if at != ME.lower():
            return False
    return w in COMMANDS


def handle(u):
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
    if is_command(text) or (is_reply_to_me and text.strip(' /!.').lower() in WORDS):
        log.info('command from allowed chat')
        threading.Thread(target=send_dashboard, daemon=True).start()


def main():
    global ME
    me = tg('getMe')
    if not me:
        sys.exit('Неверный TELEGRAM_BOT_TOKEN или нет связи с Telegram')
    ME = me['username']
    if me.get('can_read_all_group_messages'):
        sys.exit('Privacy mode выключен: бот видел бы всю переписку. @BotFather → /setprivacy → Enable')
    if me.get('can_join_groups'):
        log.warning('Бота можно добавлять в группы: @BotFather → /setjoingroups → Disable')
    chat = tg('getChat', {'chat_id': CHAT_ID})
    if not chat:
        sys.exit('Бот не состоит в группе ALLOWED_CHAT_ID')
    if chat.get('username'):
        sys.exit('Группа публичная (есть @адрес): сделайте её частной')
    mine = tg('getChatMember', {'chat_id': CHAT_ID, 'user_id': me['id']}) or {}
    if mine.get('status') == 'administrator':
        sys.exit('Бот — админ группы и получает всю переписку: снимите админку')
    tg('deleteWebhook', {'drop_pending_updates': False})  # команды, пришедшие между запусками, не теряем
    tg('setMyCommands', {'commands': [{'command': 'now', 'description': 'Дашборд Ozon сейчас'}],
                         'scope': {'type': 'chat', 'chat_id': CHAT_ID}})
    threading.Thread(target=scheduler, daemon=True).start()
    log.info('bot started')  # имя бота в лог не пишем: логи Actions публичные
    off = None
    end = time.time() + SERVE_SECONDS if SERVE_SECONDS else None
    while end is None or time.time() < end:
        res = tg('getUpdates', {'timeout': 50, 'offset': off, 'allowed_updates': ['message', 'my_chat_member']})
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
    if len(sys.argv) > 1 and sys.argv[1] == 'test':  # python3 bot.py test [final] → картинка в test.png, без Telegram
        fd = (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + MSK).date() - datetime.timedelta(1) if 'final' in sys.argv else None
        png, cap = build(fd); open('test.png', 'wb').write(png); print(cap)
    else:
        main()
