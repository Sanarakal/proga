import re

from pymax.auth import QrAuthFlow, SmsAuthFlow
from pymax.exceptions import ApiError


def normalize_phone(value):
    value = re.sub(r'[\s()-]', '', value)
    if re.fullmatch(r'8[0-9]{10}', value):
        value = '+7' + value[1:]
    elif re.fullmatch(r'7[0-9]{10}', value):
        value = '+' + value
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}', value):
        raise ValueError('Введите номер с кодом страны, например +7 999 123-45-67')
    return value


class PasswordCheck:
    async def _authenticate_with_password(self, app, track_id, hint):
        password = await self.password_provider.get_password(hint)
        response = await app.api.auth.check_password(track_id, password)
        if response.error or not response.login_token:
            raise ValueError('MAX не подтвердил пароль. Повторите вход вручную.')
        return response.login_token


class WorkspaceQrAuthFlow(PasswordCheck, QrAuthFlow):
    pass


class PhoneAuthFlow(PasswordCheck, SmsAuthFlow):
    def __init__(self, phone, provider):
        super().__init__(provider, provider)
        self.phone = normalize_phone(phone)

    async def authenticate(self, app):
        # WebClient has no phone argument; its supported auth-flow receives the runtime config.
        app.config.phone = self.phone
        app.config.password_max_attempts = 1
        try:
            return await super().authenticate(app)
        except RuntimeError as error:
            if 'RegistrationConfig' in str(error):
                raise ValueError('На этот номер нет доступного аккаунта MAX. Зарегистрируйтесь в официальном приложении.') from None
            raise
        finally:
            app.config.phone = None
            self.phone = None


class SavedSessionFlow:
    async def authenticate(self, app):
        raise ValueError('Сохранённая сессия недоступна. Выберите вход по QR-коду или номеру телефона.')


def login_error(error):
    # Authentication responses may echo credentials. Never persist server message/payload.
    if isinstance(error, ApiError):
        code = (error.error or '').lower()
        if any(part in code for part in ('flood', 'too-many', 'limit')):
            return ValueError('MAX ограничил попытки входа. Попробуйте позже; код повторно не запрашивался.')
        if any(part in code for part in ('code', 'verify', 'password')):
            return ValueError('MAX отклонил код или пароль: проверьте правильность и срок действия. Можно повторить вход вручную.')
        return ValueError('MAX отклонил вход. Проверьте аккаунт в официальном приложении или попробуйте другой способ входа.')
    if isinstance(error, TimeoutError):
        return ValueError('Время ожидания входа истекло. Если код не пришёл, попробуйте позже или войдите по QR-коду.')
    return error
