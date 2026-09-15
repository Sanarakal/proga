import asyncio
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

from workspace_store import Store
from workspace_security import crypt, Vault
from workspace_engine import Engine, safe_error, JobPaused
from pymax.exceptions import ApiError


def user(uid):
    return SimpleNamespace(id=uid, names=[])


def members(*ids):
    return [SimpleNamespace(contact=user(uid)) for uid in ids]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_limit_and_isolation(self):
        ids = [self.store.add_account(str(i)) for i in range(20)]
        with self.assertRaises(ValueError):
            self.store.add_account('21')
        self.store.contact(ids[0], 123, 'First')
        self.assertFalse(self.store.contacts(ids[1]))

    def test_templates_and_favorites_survive_restart(self):
        account = self.store.add_account('Local')
        self.store.save_template('Daily', 20, [(account, -123)])
        self.store.set_favorite(account, -123, True)
        restored = Store(self.temp.name)
        self.assertEqual(restored.templates()['Daily'], {'amount': 20, 'sources': {account: -123}})
        self.assertEqual(restored.favorites(account), {-123})
        restored.set_favorite(account, -123, False)
        self.assertFalse(restored.favorites(account))

    def test_batch_report_isolated_and_includes_archived_results(self):
        first = self.store.add_account('First')
        second = self.store.add_account('Second')
        a = self.store.new_job(first, 'collect', -1, 2, [10, 11], {'group': 'run-a'})
        b = self.store.new_job(second, 'collect', -2, 2, [12, 13], {'group': 'run-a'})
        other = self.store.new_job(first, 'collect', -1, 2, [14], {'group': 'run-b'})
        self.store.item(a, 10, 'confirmed')
        self.store.item(b, 12, 'unavailable')
        self.store.ignore(12, 'Blocked')
        self.store.item(b, 13, 'pending')
        self.store.item(other, 14, 'confirmed')
        self.store.delete_job(a)
        rows = self.store.batch_report(b)
        self.assertEqual({row['id'] for row in rows}, {a, b})
        self.assertEqual(sum(row['done'] for row in rows), 1)
        self.assertEqual(sum(row['ignored'] for row in rows), 1)
        self.assertEqual(sum(row['pending'] for row in rows), 1)

    def test_restart_preserves_confirmed_and_pending(self):
        account = self.store.add_account('Test')
        job = self.store.new_job(account, 'collect', 0, 2, [1, 2])
        self.store.status(job, 'running')
        self.store.item(job, 1, 'confirmed')
        self.store.item(job, 2, 'pending')
        restored = Store(self.temp.name)
        self.assertEqual(restored.job(job)['status'], 'interrupted')
        self.assertEqual(restored.items(job), {1: 'confirmed', 2: 'pending'})

    def test_labels_and_forward_options_are_persisted(self):
        account = self.store.add_account('Test')
        self.store.contact(account, 123, 'First', 'Source', 'Batch 1')
        self.assertEqual(self.store.contacts(account)[0]['label'], 'Batch 1')
        job = self.store.new_job(account, 'forward', -1, 1, [-2], {'message_id': 77})
        self.assertEqual(__import__('json').loads(self.store.job(job)['options'])['message_id'], 77)
        with self.assertRaises(ValueError):
            self.store.new_job(account, 'forward', -1, 21, range(21), {'message_id': 77})

    def test_global_source_ledger_claims_are_atomic_across_accounts(self):
        first = self.store.add_account('First')
        second = self.store.add_account('Second')
        first_job = self.store.new_job(first, 'collect', -100, 1, [501])
        second_job = self.store.new_job(second, 'collect', -100, 1, [501])
        self.store.update_source(-100, 'Source group', 1756)
        self.assertEqual(self.store.claim_harvest(-100, 501, first, first_job), 'new')
        self.assertEqual(self.store.claim_harvest(-100, 501, second, second_job), 'pending')
        self.store.confirm_harvest(-100, 501, first, first_job)
        self.assertEqual(self.store.claim_harvest(-100, 501, second, second_job), 'confirmed')
        source = self.store.source_rows()[0]
        self.assertEqual((source['taken'], source['total'], source['accounts']), (1, 1756, 1))
        self.assertEqual(self.store.source_members(-100)[0]['uid'], 501)

    def test_same_participant_is_deduplicated_between_different_sources(self):
        account = self.store.add_account('Test')
        first_job = self.store.new_job(account, 'collect', -100, 1, [501])
        second_job = self.store.new_job(account, 'collect', -200, 1, [501])
        self.store.confirm_harvest(-100, 501, account, first_job)
        self.assertEqual(self.store.claim_harvest(-200, 501, account, second_job), 'confirmed')

    def test_dpapi_roundtrip(self):
        data = b'example-session-secret'
        encrypted = crypt(data)
        self.assertNotIn(data, encrypted)
        self.assertEqual(crypt(encrypted, decrypt=True), data)

    def test_vault_seal_and_restore(self):
        import sqlite3
        account = self.store.add_account('Test')
        vault = Vault(self.store.directory / 'sessions')
        folder = vault.restore(account)
        with closing(sqlite3.connect(folder / 'session.db')) as db:
            with db:
                db.execute('CREATE TABLE example(secret TEXT)')
                db.execute('INSERT INTO example VALUES(?)', ('private',))
        vault.seal(account)
        self.assertFalse((folder / 'session.db').exists())
        self.assertTrue((folder / 'session.protected').exists())
        vault.restore(account)
        with closing(sqlite3.connect(folder / 'session.db')) as db:
            self.assertEqual(db.execute('SELECT secret FROM example').fetchone()[0], 'private')

    def test_errors_hide_payload(self):
        error = ApiError(opcode=59, error='errors.too-many-get-tam-contacts-extended', payload={'token': 'SECRET'}, message='+79123456789')
        text = safe_error(error)
        self.assertIn('ограничил', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('79123456789', text)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Test')
        self.engine = Engine(self.store)
        self.client = SimpleNamespace(me=SimpleNamespace(contact=user(1)), contacts=[],
            get_chat_members=AsyncMock(return_value=(members(1, 2), 0)),
            get_chat=AsyncMock(return_value=SimpleNamespace(type='CHAT')),
            get_message=AsyncMock(return_value=SimpleNamespace(id=77)),
            fetch_history=AsyncMock(return_value=[]),
            get_folders=AsyncMock(return_value=SimpleNamespace(folders=[])),
            fetch_users=AsyncMock(side_effect=lambda ids: [user(uid) for uid in ids]),
            add_contact=AsyncMock(side_effect=lambda uid: user(uid)),
            invite_users_to_group=AsyncMock(), forward_message=AsyncMock(return_value=SimpleNamespace(id=88)))
        self.engine.clients[self.account] = self.client
        async def direct(account, operation, *args, **kwargs):
            return await operation(*args, **kwargs)
        self.engine.request = direct

    async def asyncTearDown(self):
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(5)
        self.temp.cleanup()

    async def test_member_pagination_and_cache(self):
        self.client.get_chat_members.side_effect = [(members(1, 2), 44), (members(2, 3), 0)]
        result = await self.engine.members(self.account, -1)
        self.assertEqual(set(result), {1, 2, 3})
        await self.engine.members(self.account, -1)
        self.assertEqual(self.client.get_chat_members.await_count, 2)

    async def test_bounded_preview_does_not_cache_partial_members(self):
        self.client.get_chat_members.return_value = (members(1, 2, 3), 44)
        preview = await self.engine.preview(self.account, 'collect', -1, 2)
        self.assertEqual(len(preview['candidates']), 2)
        self.client.get_chat_members.assert_awaited_once()
        self.assertNotIn((self.account, -1), self.engine.cache)

    async def test_collection_does_not_rescan_source(self):
        job = self.store.new_job(self.account, 'collect', -1, 1, [3])
        await self.engine.run_job(job)
        self.client.get_chat_members.assert_not_awaited()

    async def test_refill_replaces_blocked_and_preserves_plan(self):
        self.client.add_contact.side_effect = [ApiError(opcode=34, error='user.blocked'), user(4), user(5)]
        self.client.get_chat_members.return_value = (members(3, 4, 5), 0)
        job = self.store.new_job(self.account, 'collect', -1, 2, [3], {'refill': True})
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {3: 'unavailable', 4: 'confirmed', 5: 'confirmed'})
        self.assertEqual(self.store.ignored_ids(), {3})
        self.assertIn('Добавлено в контакты: 2 из 2', self.engine.job_report(job))

    async def test_two_accounts_refill_without_duplicate_contact_requests(self):
        second = self.store.add_account('Second')
        other = SimpleNamespace(me=SimpleNamespace(contact=user(99)), contacts=[],
            get_chat=self.client.get_chat, get_chat_members=AsyncMock(return_value=(members(3, 4, 5, 6), 0)),
            add_contact=AsyncMock(side_effect=lambda uid: user(uid)))
        self.engine.clients[second] = other
        self.client.get_chat_members.return_value = (members(3, 4, 5, 6), 0)
        jobs = [self.store.new_job(a, 'collect', -1, 2, [], {'refill': True}) for a in (self.account, second)]
        await asyncio.gather(*(self.engine.run_job(j) for j in jobs))
        first_ids = {c.args[0] for c in self.client.add_contact.await_args_list}
        second_ids = {c.args[0] for c in other.add_contact.await_args_list}
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(second_ids), 2)
        self.assertFalse(first_ids & second_ids)

    async def test_delete_preserves_harvest_history(self):
        job = self.store.new_job(self.account, 'collect', -1, 1, [3])
        await self.engine.run_job(job)
        self.store.delete_job(job)
        self.assertEqual(self.store.job(job)['deleted'], 1)
        self.assertEqual(self.store.harvested_ids(), {3})

    async def test_refill_saves_candidates_and_resumes_after_rate_limit(self):
        self.client.get_chat_members.return_value = (members(3, 4), 0)
        self.client.add_contact.side_effect = [user(3), ApiError(opcode=34, error='too-many')]
        job = self.store.new_job(self.account, 'collect', -1, 2, [], {'refill': True})
        with self.assertRaises(ApiError):
            await self.engine.run_job(job)
        self.assertEqual(__import__('json').loads(self.store.job(job)['candidates']), [3, 4])
        self.client.add_contact.side_effect = lambda uid: user(uid)
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {3: 'confirmed', 4: 'confirmed'})

    async def test_manual_collection_deduplicates_global_history(self):
        first = self.store.new_job(self.account, 'collect', 0, 1, [3])
        await self.engine.run_job(first)
        self.assertEqual(self.store.claim_harvest(-2, 3, 'another', 'another-job'), 'confirmed')
        self.assertFalse(self.store.rows('SELECT * FROM harvest_claims'))

    async def test_deleted_job_cannot_be_executed(self):
        job = self.store.new_job(self.account, 'collect', -1, 1, [3])
        self.store.delete_job(job)
        with self.assertRaises(ValueError):
            await self.engine.run_job(job)
        self.client.add_contact.assert_not_awaited()

    async def test_unavailable_contacts_are_skipped_without_retry(self):
        self.client.add_contact.side_effect = [ApiError(opcode=34, error='not.found'),
                                               ApiError(opcode=34, error='user.blocked'), user(5)]
        job = self.store.new_job(self.account, 'collect', -1, 1, [3, 4, 5])
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {3: 'unavailable', 4: 'unavailable', 5: 'confirmed'})
        self.assertEqual(self.store.job(job)['status'], 'complete')

    async def test_generic_blocked_error_still_suspends(self):
        self.client.add_contact.side_effect = ApiError(opcode=34, error='blocked')
        job = self.store.new_job(self.account, 'collect', 0, 1, [3])
        with self.assertRaises(ApiError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.job(job)['status'], 'needs_review')

    async def test_full_check_failure_prevents_mutation(self):
        self.client.get_chat_members.side_effect = ApiError(opcode=59, error='too-many')
        job = self.store.new_job(self.account, 'invite', -1, 1, [3])
        with self.assertRaises(ApiError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.job(job)['status'], 'limited')
        self.client.invite_users_to_group.assert_not_awaited()

    async def test_hidden_members_count_does_not_block_completed_pagination(self):
        self.client.get_chat.return_value = SimpleNamespace(type='CHAT', participants_count=8)
        job = self.store.new_job(self.account, 'invite', -1, 1, [3])
        self.client.get_chat_members.side_effect = [(members(1), 0), (members(1, 3), 0)]
        await self.engine.run_job(job)
        self.client.invite_users_to_group.assert_awaited_once_with(-1, [3], show_history=False)
        warnings = self.store.rows("SELECT message FROM logs WHERE message LIKE '%видимых%'")
        self.assertTrue(warnings)

    async def test_embedded_chat_participants_are_used_to_skip_existing_member(self):
        self.client.get_chat.return_value = SimpleNamespace(
            type='CHAT', participants_count=2, participants={1: 0, 3: 0})
        self.client.get_chat_members.return_value = (members(1), 0)
        job = self.store.new_job(self.account, 'invite', -1, 1, [3])
        await self.engine.run_job(job)
        self.client.invite_users_to_group.assert_not_awaited()
        self.assertEqual(self.store.items(job)[3], 'skipped')

    async def test_uncertain_collect_is_blocked_across_source_chats(self):
        first = self.store.new_job(self.account, 'collect', -1, 1, [3])
        self.store.item(first, 3, 'pending')
        second = self.store.new_job(self.account, 'collect', -2, 1, [3])
        with self.assertRaises(ValueError):
            await self.engine.run_job(second)
        self.client.add_contact.assert_not_awaited()

    async def test_collection_persists_each_success(self):
        self.store.contact(self.account, 2)
        job = self.store.new_job(self.account, 'collect', -1, 1, [1, 2, 3, 4])
        await self.engine.run_job(job)
        self.client.add_contact.assert_awaited_once_with(3)
        self.assertEqual(self.store.items(job), {1: 'skipped', 2: 'skipped', 3: 'confirmed'})
        self.assertIn(3, [row['uid'] for row in self.store.contacts(self.account)])
        self.assertEqual(self.store.harvested_ids(-1), {3})

    async def test_collection_preview_excludes_globally_harvested_ids(self):
        other = self.store.add_account('Second')
        old_job = self.store.new_job(self.account, 'collect', -1, 1, [3])
        self.store.confirm_harvest(-1, 3, self.account, old_job)
        other_client = SimpleNamespace(
            me=SimpleNamespace(contact=user(10)), contacts=[],
            get_chat=AsyncMock(return_value=SimpleNamespace(
                type='CHAT', title='Source group', participants_count=3)),
            get_chat_members=AsyncMock(return_value=(members(3, 4, 10), 0)))
        self.engine.clients[other] = other_client
        preview = await self.engine.preview(other, 'collect', -1, 1)
        self.assertEqual([uid for uid, _ in preview['candidates']], [4])
        source = self.store.source_rows()[0]
        self.assertEqual((source['title'], source['total'], source['taken']), ('Source group', 3, 1))

    async def test_collection_report_contains_accumulated_source_progress(self):
        self.client.get_chat.return_value = SimpleNamespace(
            type='CHAT', title='Source group', participants_count=1756)
        await self.engine.preview(self.account, 'collect', -1, 1)
        job = self.store.new_job(self.account, 'collect', -1, 1, [3])
        await self.engine.run_job(job)
        report = self.engine.job_report(job)
        self.assertIn('Источник: Source group', report)
        self.assertIn('Всего взято из источника: 1 из 1756', report)

    async def test_invite_uses_one_batch_and_two_membership_scans(self):
        self.client.get_chat_members.side_effect = [(members(1, 2), 0), (members(1, 2, 3, 4), 0)]
        job = self.store.new_job(self.account, 'invite', -1, 2, [2, 3, 4])
        await self.engine.run_job(job)
        self.assertEqual(self.client.get_chat_members.await_count, 2)
        self.client.invite_users_to_group.assert_awaited_once_with(-1, [3, 4], show_history=False)
        self.assertEqual(self.store.items(job)[3], 'confirmed')

    async def test_previous_confirmed_invite_is_not_repeated_when_api_omits_member(self):
        old = self.store.new_job(self.account, 'invite', -1, 1, [3])
        self.store.item(old, 3, 'confirmed')
        job = self.store.new_job(self.account, 'invite', -1, 1, [3])
        await self.engine.run_job(job)
        self.client.invite_users_to_group.assert_not_awaited()
        self.assertEqual(self.store.items(job)[3], 'skipped')

    async def test_invite_response_can_confirm_member_without_second_scan(self):
        self.client.invite_users_to_group.return_value = SimpleNamespace(participants={3: 0})
        self.client.get_chat_members.return_value = (members(1), 0)
        job = self.store.new_job(self.account, 'invite', -1, 1, [3])
        await self.engine.run_job(job)
        self.assertEqual(self.client.get_chat_members.await_count, 1)
        self.assertEqual(self.store.items(job)[3], 'confirmed')

    async def test_invite_skips_unavailable_before_batch(self):
        blocked = user(3)
        blocked.status = 'blocked'
        self.client.contacts = [blocked, user(4)]
        self.client.get_chat_members.side_effect = [(members(1), 0), (members(1, 4), 0)]
        job = self.store.new_job(self.account, 'invite', -1, 2, [3, 4])
        await self.engine.run_job(job)
        self.client.fetch_users.assert_not_awaited()
        self.client.invite_users_to_group.assert_awaited_once_with(-1, [4], show_history=False)
        self.assertEqual(self.store.items(job)[3], 'unavailable')

    async def test_manual_retry_invites_one_by_one_and_skips_not_found(self):
        self.client.invite_users_to_group.side_effect = [ApiError(opcode=52, error='not.found'), None]
        self.client.get_chat_members.side_effect = [(members(1), 0), (members(1, 4), 0)]
        job = self.store.new_job(self.account, 'invite', -1, 2, [3, 4], {'force_single': True})
        await self.engine.run_job(job)
        self.assertEqual(self.client.invite_users_to_group.await_count, 2)
        self.client.invite_users_to_group.assert_any_await(-1, [3], show_history=False)
        self.client.invite_users_to_group.assert_any_await(-1, [4], show_history=False)
        self.assertEqual(self.store.items(job)[3], 'unavailable')
        self.assertEqual(self.store.items(job)[4], 'confirmed')

    async def test_privacy_filtered_invite_batch_is_skipped(self):
        self.client.invite_users_to_group.side_effect = ApiError(
            opcode=77, error='participants.filter.out',
            localized_message='Не удалось добавить ни одного участника по причине приватности')
        self.client.get_chat_members.return_value = (members(1), 0)
        job = self.store.new_job(self.account, 'invite', -1, 2, [3, 4])
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {3: 'unavailable', 4: 'unavailable'})
        self.assertEqual(self.store.job(job)['status'], 'complete')
        report = self.store.job(job)['message']
        self.assertIn('Добавлено в группу: 0 из 2', report)
        self.assertIn('Не выполнено: 2', report)
        self.assertIn('приватностью: 2', report)
        self.assertIn('ни один участник не добавлен', report)

    async def test_job_report_separates_result_categories(self):
        job = self.store.new_job(self.account, 'invite', -1, 4, [2, 3, 4, 5])
        self.store.item(job, 2, 'confirmed')
        self.store.item(job, 3, 'unavailable')
        self.store.item(job, 4, 'skipped')
        self.store.item(job, 5, 'retryable')
        report = self.engine.job_report(job)
        self.assertIn('Добавлено в группу: 1 из 4', report)
        self.assertIn('Не выполнено: 3', report)
        self.assertIn('Уже были в группе / пропущены: 1', report)
        self.assertIn('разрешён повтор: 1', report)

    async def test_forward_confirms_each_target(self):
        job = self.store.new_job(self.account, 'forward', -1, 2, [-2, -3], {'message_id': 77})
        await self.engine.run_job(job)
        self.assertEqual(self.client.forward_message.await_count, 2)
        self.assertEqual(self.store.items(job), {-2: 'confirmed', -3: 'confirmed'})

    async def test_forward_setup_loads_history_and_folders(self):
        self.client.fetch_history.return_value = [SimpleNamespace(id=77, text='Hello', time=1000, attaches=[])]
        self.client.get_folders.return_value = SimpleNamespace(
            folders=[SimpleNamespace(title='Work', include=[-2, -3])])
        setup = await self.engine.forward_setup(self.account, -1)
        self.assertEqual(setup['messages'][0][0], 77)
        self.assertEqual(setup['folders'], [('Work', [-2, -3])])

    async def test_unconfirmed_operation_is_not_repeated(self):
        job = self.store.new_job(self.account, 'collect', 0, 1, [3])
        self.store.item(job, 3, 'pending')
        with self.assertRaises(ValueError):
            await self.engine.run_job(job)
        self.client.add_contact.assert_not_awaited()

    async def test_confirmed_result_not_repeated_on_resume(self):
        job = self.store.new_job(self.account, 'collect', 0, 2, [3, 4])
        self.store.item(job, 3, 'confirmed')
        await self.engine.run_job(job)
        self.client.add_contact.assert_awaited_once_with(4)

    async def test_pause_preserves_partial_results(self):
        job = self.store.new_job(self.account, 'collect', 0, 2, [3, 4])
        async def added(uid):
            self.engine.flags[job] = 'pause'
            return user(uid)
        self.client.add_contact.side_effect = added
        with self.assertRaises(JobPaused):
            await self.engine.run_job(job)
        self.assertEqual(self.store.job(job)['status'], 'paused')
        self.assertEqual(self.store.items(job), {3: 'confirmed'})

    async def test_rate_reply_can_be_manually_retried(self):
        self.client.add_contact.side_effect = ApiError(opcode=46, error='too-many')
        job = self.store.new_job(self.account, 'collect', 0, 1, [3])
        with self.assertRaises(ApiError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.items(job)[3], 'retryable')
        self.client.add_contact.side_effect = lambda uid: user(uid)
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job)[3], 'confirmed')


if __name__ == '__main__':
    unittest.main()
