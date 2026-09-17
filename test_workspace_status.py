import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from pymax.exceptions import ApiError
from pymax.protocol.enums import Opcode, Command
from workspace_engine import Engine, JobPaused
from workspace_status import classify_account_error, account_display
from workspace_store import Store


class ClassificationTests(unittest.TestCase):
    def test_explicit_account_errors(self):
        for code, expected in [('FAIL_LOGIN_TOKEN', 'needs_login'), ('FAIL_LOGOUT_ALL', 'needs_login'),
                               ('account.blocked', 'blocked'), ('errors.account.banned', 'blocked'),
                               ('errors.too-many-requests', 'limited'), ('flood.wait', 'limited')]:
            self.assertEqual(classify_account_error(ApiError(opcode=19, error=code))[0], expected)

    def test_recipient_and_chat_failures_are_not_account_bans(self):
        for code in ('user.blocked', 'contact.blocked', 'chat.blocked', 'forbidden', 'chat.full', 'not.found'):
            self.assertIsNone(classify_account_error(ApiError(opcode=34, error=code)))
        self.assertEqual(classify_account_error(ApiError(opcode=19, error='user.blocked'))[0], 'blocked')

    def test_network_and_timeout_are_neutral(self):
        for error in (ConnectionError(), TimeoutError(), EOFError()):
            self.assertEqual(classify_account_error(error)[0], 'network_error')

    def test_status_palette_and_no_false_recovery(self):
        for state, color in [('online', 'green'), ('needs_login', 'yellow'), ('blocked', 'red'),
                             ('limited', 'red'), ('network_error', 'gray')]:
            row = dict(connection_state=state, blocked_until=0)
            self.assertEqual(account_display(row, True)[2], color)
        self.assertEqual(account_display(dict(connection_state='online'), False)[0], 'network_error')


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Account')
        self.engine = Engine(self.store)
        self.client = NS(is_connected=True, close=AsyncMock(), _app=NS(invoke=AsyncMock()))
        self.engine.clients[self.account] = self.client
        self.store.account_status(self.account, 'online', checked=True)

    async def asyncTearDown(self):
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(3)
        self.temp.cleanup()

    def row(self):
        return self.store.rows('SELECT * FROM accounts WHERE id=?', (self.account,))[0]

    async def test_persistence_and_no_duplicate_log_entries(self):
        self.engine.set_account_status(self.account, 'blocked', 'Blocked')
        before = len(self.store.rows('SELECT * FROM logs'))
        self.engine.set_account_status(self.account, 'blocked', 'Blocked')
        self.assertEqual(len(self.store.rows('SELECT * FROM logs')), before)
        restored = Store(self.temp.name)
        self.assertEqual(restored.rows('SELECT connection_state FROM accounts')[0]['connection_state'], 'blocked')
        self.store.account_status(self.account, 'online')
        restored = Store(self.temp.name)
        self.assertEqual(restored.rows('SELECT connection_state FROM accounts')[0]['connection_state'], 'offline')

    async def test_transport_loss_closes_and_seals_stale_client(self):
        self.client.is_connected = False
        with patch.object(self.engine.vault, 'seal') as seal:
            await self.engine.inspect_connections()
            self.client.close.assert_awaited_once()
            seal.assert_called_once_with(self.account)
        self.assertEqual(self.row()['connection_state'], 'network_error')
        self.assertNotIn(self.account, self.engine.clients)
        self.assertFalse(self.engine.available(self.account))

    async def test_push_logout_and_stale_client_events(self):
        frame = NS(cmd=Command.EVENT, opcode=Opcode.LOGOUT)
        await self.engine.account_event(self.account, object(), frame)
        self.assertEqual(self.row()['connection_state'], 'online')
        await self.engine.account_event(self.account, self.client, frame)
        self.assertEqual(self.row()['connection_state'], 'needs_login')
        self.assertFalse(self.engine.available(self.account))
        operation = AsyncMock()
        with self.assertRaises(JobPaused):
            await self.engine.request(self.account, operation)
        operation.assert_not_awaited()

    async def test_block_is_sticky_during_transport_loss_and_ping(self):
        self.engine.record_account_error(self.account, ApiError(opcode=19, error='account.blocked'))
        self.engine.record_account_error(self.account, ConnectionError())
        await self.engine.account_event(self.account, self.client, NS(cmd=Command.RESPONSE, opcode=Opcode.PING))
        await self.engine.check_account(self.account)
        self.assertEqual(self.row()['connection_state'], 'blocked')

    async def test_flood_is_red_and_timeout_does_not_hide_it(self):
        operation = AsyncMock(side_effect=ApiError(opcode=34, error='flood.wait'))
        with self.assertRaises(ApiError):
            await self.engine.request(self.account, operation)
        self.assertEqual(self.row()['connection_state'], 'limited')
        self.assertGreater(self.row()['blocked_until'], time.time())
        self.assertFalse(self.engine.available(self.account))
        self.engine.record_account_error(self.account, TimeoutError())
        self.assertEqual(self.row()['connection_state'], 'limited')
        with self.assertRaises(JobPaused):
            await self.engine.request(self.account, operation)
        self.assertEqual(operation.await_count, 1)

    async def test_expired_local_pause_is_not_proof_of_recovery(self):
        self.store.account_status(self.account, 'limited')
        await self.engine.check_account(self.account)
        self.assertEqual(self.row()['connection_state'], 'limited')
        operation = AsyncMock(return_value='done')
        self.assertEqual(await self.engine.request(self.account, operation), 'done')
        self.assertEqual(self.row()['connection_state'], 'online')

    async def test_manual_check_success_and_error(self):
        await self.engine.check_account(self.account)
        self.client._app.invoke.assert_awaited_once_with(Opcode.PING, {'interactive': True})
        self.assertGreater(self.row()['status_checked'], 0)
        self.client._app.invoke.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.engine.check_account(self.account)
        self.assertEqual(self.row()['connection_state'], 'network_error')

    async def test_recipient_failure_does_not_change_own_status(self):
        operation = AsyncMock(side_effect=ApiError(opcode=34, error='user.blocked'))
        with self.assertRaises(ApiError):
            await self.engine.request(self.account, operation)
        self.assertEqual(self.row()['connection_state'], 'online')

    async def test_offline_check_does_not_authenticate(self):
        self.engine.clients.clear()
        self.store.account_status(self.account, 'needs_login')
        await self.engine.check_account(self.account)
        self.client._app.invoke.assert_not_awaited()
        self.assertEqual(self.row()['connection_state'], 'needs_login')

    async def test_unknown_check_error_preserves_known_block(self):
        self.store.account_status(self.account, 'blocked')
        self.client._app.invoke.side_effect = ValueError('Unknown response')
        with self.assertRaises(ValueError):
            await self.engine.check_account(self.account)
        self.assertEqual(self.row()['connection_state'], 'blocked')

    async def test_offline_job_is_paused_without_submitting(self):
        self.engine.clients.clear()
        job = self.store.new_job(self.account, 'collect', 0, 1, [101])
        with self.assertRaises(JobPaused):
            await self.engine.run_job(job)
        self.assertEqual(self.store.job(job)['status'], 'paused')
        self.assertFalse(self.store.items(job))

    async def test_logout_during_request_delay_prevents_sending(self):
        self.engine.last_request[(self.account, 'write')] = time.monotonic()
        async def revoke(_):
            self.store.account_status(self.account, 'needs_login')
        operation = AsyncMock()
        with patch('workspace_engine.asyncio.sleep', side_effect=revoke):
            with self.assertRaises(JobPaused):
                await self.engine.request(self.account, operation)
        operation.assert_not_awaited()

    async def test_disconnect_seals_even_when_transport_close_fails(self):
        self.client.close.side_effect = OSError('close failed')
        with patch.object(self.engine.vault, 'seal') as seal:
            with self.assertRaises(OSError):
                await self.engine.disconnect(self.account)
            seal.assert_called_once_with(self.account)
