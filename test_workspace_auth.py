import asyncio
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from pymax import WebClient
from pymax.exceptions import ApiError
from pymax.protocol.enums import Opcode
from workspace_auth import PhoneAuthFlow, SavedSessionFlow, WorkspaceQrAuthFlow, normalize_phone, login_error
from workspace_engine import Engine, LoginProvider
from workspace_store import Store


class PhoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = NS(get_code=AsyncMock(return_value='123456'), get_password=AsyncMock(return_value='secret'))
        self.auth = NS(request_code=AsyncMock(return_value=NS(token='request-token')),
                       send_code=AsyncMock(return_value=NS(login_token='session-token')),
                       check_password=AsyncMock(return_value=NS(error=None, login_token='session-token')))
        self.app = NS(config=NS(phone=None, registration_config=None), api=NS(auth=self.auth))
        self.flow = PhoneAuthFlow('+79991234567', self.provider)

    def test_normalize(self):
        for phone in ('+7 (999) 123-45-67', '89991234567', '79991234567'):
            self.assertEqual(normalize_phone(phone), '+79991234567')
        for phone in ('123', '7999a1234567', '++79991234567', '+0123456789'):
            with self.assertRaises(ValueError):
                normalize_phone(phone)

    async def test_code_then_session_token_and_no_phone_retained(self):
        self.assertEqual((await self.flow.authenticate(self.app)).token, 'session-token')
        self.auth.request_code.assert_awaited_once_with('+79991234567')
        self.auth.send_code.assert_awaited_once_with('request-token', '123456')
        self.assertIsNone(self.app.config.phone)
        self.assertIsNone(self.flow.phone)

    async def test_delivery_failure_does_not_prompt_or_repeat(self):
        self.auth.request_code.side_effect = ApiError(opcode=17, error='flood', message='secret 123456')
        with self.assertRaises(ApiError):
            await self.flow.authenticate(self.app)
        self.provider.get_code.assert_not_awaited()
        self.auth.request_code.assert_awaited_once()
        self.assertIsNone(self.app.config.phone)

    async def test_2fa(self):
        self.auth.send_code.return_value = NS(login_token=None, password_challenge=NS(track_id='track', hint=None))
        self.assertEqual((await self.flow.authenticate(self.app)).token, 'session-token')
        self.auth.check_password.assert_awaited_once_with('track', 'secret')

    async def test_no_automatic_2fa_retry(self):
        self.auth.check_password.side_effect = ApiError(opcode=115, error='flood')
        with self.assertRaises(ApiError):
            await self.flow._authenticate_with_password(self.app, 'track', None)
        self.provider.get_password.assert_awaited_once()
        self.auth.check_password.assert_awaited_once()

    async def test_no_automatic_registration(self):
        self.auth.send_code.return_value = NS(login_token=None, password_challenge=None, register_token='register')
        with self.assertRaisesRegex(ValueError, 'официальном'):
            await self.flow.authenticate(self.app)

    async def test_cancel_does_not_submit_code(self):
        self.provider.get_code.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.flow.authenticate(self.app)
        self.auth.send_code.assert_not_awaited()
        self.assertIsNone(self.app.config.phone)

    async def test_real_web_config_is_compatible(self):
        client = WebClient(auth_flow=self.flow)
        self.app.config = await client._prepare_config()
        self.assertEqual(str(self.app.config.device.user_agent.device_type.value), 'WEB')
        self.assertEqual((await self.flow.authenticate(self.app)).token, 'session-token')

    async def test_missing_saved_session_never_requests_code(self):
        with self.assertRaisesRegex(ValueError, 'Сохранённая'):
            await SavedSessionFlow().authenticate(self.app)
        self.auth.request_code.assert_not_awaited()

    async def test_provider_validation_and_cancelled_future(self):
        event = Mock()
        provider = LoginProvider(NS(event=event), 'account')
        event.emit.side_effect = lambda kind, data: data[1].set_result('abcd')
        with self.assertRaises(ValueError):
            await provider.get_code('+79991234567')
        self.assertNotIn('+79991234567', str(event.emit.call_args))
        event.emit.side_effect = None
        task = asyncio.create_task(provider.get_code('+79991234567'))
        await asyncio.sleep(0)
        future = event.emit.call_args.args[1][1]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(future.cancelled())

    def test_auth_errors_never_echo_credentials(self):
        for code in ('flood', 'verify.invalid', 'unknown'):
            error = ApiError(opcode=18, error=code, message='123456 secret +79991234567')
            result = str(login_error(error))
            for secret in ('123456', 'secret', '+79991234567'):
                self.assertNotIn(secret, result)


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Test')
        self.engine = Engine(self.store)
        self.client = NS(connect=AsyncMock(), close=AsyncMock(), relogin=AsyncMock(), contacts=[], chats=[], me=NS(contact=NS(id=101)))
        self.factory = patch('workspace_engine.WebClient', return_value=self.client)
        self.construct = self.factory.start()
        self.vault_patch = patch.object(self.engine, 'vault')
        self.vault = self.vault_patch.start()

    async def asyncTearDown(self):
        self.factory.stop()
        self.vault_patch.stop()
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(3)
        self.temp.cleanup()

    async def test_all_connection_modes(self):
        for mode, cls in (('qr', WorkspaceQrAuthFlow), ('phone', PhoneAuthFlow), ('saved', SavedSessionFlow)):
            self.engine.clients.clear()
            await self.engine.connect(self.account, mode, '+79991234567')
            self.assertIsInstance(self.construct.call_args.kwargs['auth_flow'], cls)
            self.assertIn(self.account, self.engine.clients)

    async def test_invalid_phone_does_not_restore_session(self):
        with self.assertRaises(ValueError):
            await self.engine.connect(self.account, 'phone', '123')
        self.vault.restore.assert_not_called()

    async def test_revoked_session_allows_chosen_login_once(self):
        self.client.connect.side_effect = [ApiError(opcode=Opcode.LOGIN, error='FAIL_LOGIN_TOKEN'), None]
        await self.engine.connect(self.account, 'phone', '+79991234567')
        self.client.relogin.assert_awaited_once_with(start=False)
        self.assertEqual(self.client.connect.await_count, 2)

    async def test_saved_revoked_session_does_not_start_interactive_auth(self):
        self.client.connect.side_effect = ApiError(opcode=Opcode.LOGIN, error='FAIL_LOGOUT_ALL')
        with self.assertRaisesRegex(ValueError, 'Сессия отозвана'):
            await self.engine.connect(self.account, 'saved')
        self.client.relogin.assert_awaited_once_with(start=False)
        self.client.connect.assert_awaited_once()

    async def test_failure_closes_and_seals_and_sanitizes(self):
        self.client.connect.side_effect = ApiError(opcode=18, error='code.invalid', message='secret 123456')
        with self.assertRaises(ValueError):
            await self.engine.connect(self.account, 'phone', '+79991234567')
        self.client.close.assert_awaited_once()
        self.client.relogin.assert_not_awaited()
        self.vault.seal.assert_called_once_with(self.account)
        self.assertNotIn('123456', self.store.rows('SELECT last_error FROM accounts')[0]['last_error'])
        self.assertNotIn(self.account, self.engine.clients)

    async def test_cancel_and_timeout_cleanup(self):
        for error in (asyncio.CancelledError(), TimeoutError()):
            self.client.connect.side_effect = error
            with self.assertRaises((asyncio.CancelledError, ValueError)):
                await self.engine.connect(self.account)
        self.assertEqual(self.client.close.await_count, 2)
        self.assertEqual(self.vault.seal.call_count, 2)

    async def test_constructor_failure_still_seals(self):
        self.construct.side_effect = RuntimeError('constructor failed')
        with self.assertRaises(RuntimeError):
            await self.engine.connect(self.account)
        self.vault.seal.assert_called_once_with(self.account)

    async def test_duplicate_account_is_rejected(self):
        other = self.store.add_account('Other')
        self.store.execute('UPDATE accounts SET max_id=101 WHERE id=?', (other,))
        with self.assertRaisesRegex(ValueError, 'уже есть'):
            await self.engine.connect(self.account)
        self.assertNotIn(self.account, self.engine.clients)

    async def test_identity_cannot_change(self):
        self.store.execute('UPDATE accounts SET max_id=202 WHERE id=?', (self.account,))
        with self.assertRaisesRegex(ValueError, 'другой аккаунт'):
            await self.engine.connect(self.account)
        self.assertEqual(self.store.rows('SELECT max_id FROM accounts')[0]['max_id'], 202)
