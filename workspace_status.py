import time

from pymax.exceptions import ApiError
from pymax.protocol.enums import Opcode


ACCOUNT_STATES = {
    'online': ('Подключён', 'green'),
    'needs_login': ('Требуется вход', 'yellow'),
    'limited': ('Ограничен', 'red'),
    'blocked': ('Заблокирован', 'red'),
    'offline': ('Отключён', 'gray'),
    'connecting': ('Подключение', 'gray'),
    'network_error': ('Нет соединения', 'gray'),
    'error': ('Ошибка проверки', 'gray'),
}


def classify_account_error(error):
    if isinstance(error, ApiError):
        code = (error.error or '').lower()
        if code in {'fail_login_token', 'fail_logout_all', 'session.revoked', 'session.expired'} or (
                error.opcode == Opcode.LOGIN and error.message in ('FAIL_LOGIN_TOKEN', 'FAIL_LOGOUT_ALL')):
            return 'needs_login', 'Сессия MAX недействительна. Подключите аккаунт заново.'
        if code in {'account.blocked', 'account.banned', 'errors.account.blocked', 'errors.account.banned'} or (
                error.opcode in (Opcode.LOGIN, Opcode.AUTH_REQUEST, Opcode.AUTH) and
                code in {'user.blocked', 'user.banned', 'phone.blocked', 'errors.user.blocked'}):
            return 'blocked', 'MAX сообщил о блокировке аккаунта. Проверьте его в официальном приложении.'
        if 'too-many' in code or 'flood' in code:
            return 'limited', 'MAX ограничил запросы. Срок ограничения сервер не сообщил.'
    elif isinstance(error, (ConnectionError, EOFError, OSError, TimeoutError)):
        return 'network_error', 'Нет ответа MAX. Проверьте сеть и подключение аккаунта.'
    return None


def account_display(row, connected):
    state = row.get('connection_state', 'offline')
    detail = row.get('status_detail', '')
    if state not in ACCOUNT_STATES:
        state = 'offline'
    if row.get('blocked_until', 0) > time.time() and state not in ('blocked', 'needs_login'):
        state = 'limited'
    if state not in ('blocked', 'needs_login', 'limited', 'connecting'):
        if connected and state not in ('network_error', 'error'):
            state = 'online'
        elif state == 'online':
            state, detail = 'network_error', 'Соединение с MAX потеряно.'
    if state == 'limited':
        remaining = max(0, int(row.get('blocked_until', 0) - time.time()))
        detail = ('Локальная пауза до ' + time.strftime('%H:%M:%S', time.localtime(row['blocked_until']))
                  if remaining else 'Пауза закончилась; снятие ограничения ещё не подтверждено.')
    title, color = ACCOUNT_STATES[state]
    return state, title, color, detail
