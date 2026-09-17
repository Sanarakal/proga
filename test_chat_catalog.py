import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pymax.exceptions import ApiError
from workspace_store import Store
from workspace_links import LinkImport
from workspace_engine import Engine
from test_workspace_links import entry, chat


class CatalogStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('First')

    def tearDown(self):
        self.temp.cleanup()

    def test_multiple_files_merge_persist_and_do_not_revive_dead_links(self):
        first = LinkImport(entries=[entry('a'), entry('b')])
        second = LinkImport(entries=[entry('b'), entry('c')])
        self.assertEqual(self.store.save_join_file('one.txt', b'one', first)['added'], 2)
        self.assertEqual(self.store.save_join_file('two.txt', b'two', second)['added'], 1)
        self.store.invalidate_catalog_link(entry('b')['link'], 'expired')
        self.assertEqual(self.store.save_join_file('one.txt', b'one', first)['added'], 0)
        restored = Store(self.temp.name)
        self.assertEqual(restored.catalog_counts(), {'active': 2, 'inactive': 1, 'free': 2})
        self.assertEqual(len(restored.join_files()), 2)
        self.assertEqual(restored.rows('SELECT original FROM join_files WHERE filename=?', ('one.txt',))[0]['original'], b'one')

    def test_aliases_merge_and_live_alias_survives_dead_link(self):
        self.store.save_join_file('list.txt', b'file', LinkImport(entries=[entry('a'), entry('b')]))
        job = self.store.new_join_job(self.account, [entry('a'), entry('b')], 1)
        for uid in (1, 2):
            self.store.join_target_info(job, uid, -7, 'Same group')
        self.assertEqual(self.store.catalog_counts(), {'active': 1, 'duplicate': 1, 'free': 1})
        self.store.invalidate_catalog_link(entry('a')['link'], 'expired')
        self.assertEqual(self.store.catalog_rows()[0]['link'], entry('b')['link'])
        self.assertEqual(self.store.catalog_counts()['inactive'], 1)

    def test_existing_jobs_migrate_catalog_and_global_registry(self):
        job = self.store.new_join_job(self.account, [entry('a'), entry('b')], 1, {'file': 'old.xlsx'})
        self.store.join_target_info(job, 1, -1, 'Old group')
        self.store.item(job, 1, 'confirmed')
        self.store.execute('DELETE FROM join_registry')
        for key in ('join_registry_migrated', 'join_files_migrated', 'chat_catalog_migrated'):
            self.store.execute('DELETE FROM settings WHERE key=?', (key,))
        restored = Store(self.temp.name)
        self.assertEqual(restored.catalog_counts()['active'], 2)
        self.assertEqual(restored.catalog_counts()['free'], 1)
        self.assertEqual(len(restored.global_join_rows()), 1)
        self.assertEqual(len(Store(self.temp.name).join_files()), 1)

    def test_registry_survives_job_and_account_deletion(self):
        job = self.store.new_join_job(self.account, [entry('a')], 1)
        self.store.join_target_info(job, 1, -1, 'Known')
        self.store.item(job, 1, 'confirmed')
        self.store.delete_job(job)
        for table in ('join_targets', 'items'):
            self.store.execute(f'DELETE FROM {table} WHERE job=?', (job,))
        self.store.execute('DELETE FROM jobs WHERE id=?', (job,))
        self.store.execute('DELETE FROM accounts WHERE id=?', (self.account,))
        restored = Store(self.temp.name)
        other = restored.add_account('Other')
        other_job = restored.new_join_job(other, [entry('b')], 1)
        conflict = restored.claim_join(other_job, 1, entry('b')['link'], -1)
        self.assertEqual(conflict['account_name'], 'First')

    def test_atomic_chat_claim_from_multiple_threads(self):
        jobs = [self.store.new_join_job(self.account, [entry(str(i))], 1) for i in range(8)]
        def claim(pair):
            i, job = pair
            return self.store.claim_join(job, 1, entry(str(i))['link'], -1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, enumerate(jobs)))
        self.assertEqual(sum(r is None for r in results), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM join_registry')), 2)

    def test_restart_releases_only_unsent_reservations(self):
        job = self.store.new_join_job(self.account, [entry('a'), entry('b')], 2)
        self.store.claim_join(job, 1, entry('a')['link'], -1)
        self.store.join_target_info(job, 2, -2, 'Pending')
        self.store.claim_join(job, 2, entry('b')['link'], -2)
        self.store.item(job, 2, 'pending', attempted=True)
        restored = Store(self.temp.name)
        rows = restored.rows('SELECT * FROM join_registry')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['state'] for r in rows}, {'pending'})

    def test_folder_and_search_scope(self):
        self.store.save_join_file('list.txt', b'file', LinkImport(entries=[entry('a'), entry('b')]))
        self.store.execute("UPDATE chat_catalog SET title='Рабочая ГРУППА' WHERE link=?", (entry('a')['link'],))
        self.store.execute("INSERT INTO chat_folders VALUES('future','Future')")
        self.store.execute("INSERT INTO chat_folder_links VALUES('future',?)", (entry('b')['link'],))
        self.assertEqual(len(self.store.catalog_rows(search='рабочая группа')), 1)
        self.assertEqual([r['link'] for r in self.store.catalog_rows(folder='future')], [entry('b')['link']])


class GlobalJoinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.engine = Engine(self.store)
        self.accounts = [self.store.add_account(str(i)) for i in range(2)]
        self.clients = []
        for account in self.accounts:
            client = SimpleNamespace(me=SimpleNamespace(contact=SimpleNamespace(id=1)),
                resolve_group_by_link=AsyncMock(return_value=chat()),
                join_group=AsyncMock(return_value=chat(joined=True)))
            self.clients.append(client)
            self.engine.clients[account] = client
            self.store.account_status(account, 'online')
        async def direct(account, operation, *args, **kwargs):
            await asyncio.sleep(0)
            return await operation(*args, **kwargs)
        self.engine.request = direct

    async def asyncTearDown(self):
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(5)
        self.temp.cleanup()

    def jobs(self, tokens):
        return [self.store.new_join_job(account, [entry(token)], 1)
                for account, token in zip(self.accounts, tokens)]

    async def test_parallel_accounts_never_join_same_link(self):
        jobs = self.jobs('aa')
        await asyncio.gather(*(self.engine.run_job(job) for job in jobs))
        self.assertEqual(sum(c.join_group.await_count for c in self.clients), 1)
        self.assertEqual(sum(list(self.store.items(j).values()).count('confirmed') for j in jobs), 1)

    async def test_parallel_alias_links_never_join_same_chat(self):
        jobs = self.jobs('ab')
        await asyncio.gather(*(self.engine.run_job(job) for job in jobs))
        self.assertEqual(sum(c.join_group.await_count for c in self.clients), 1)
        self.assertFalse(self.store.rows("SELECT * FROM join_registry WHERE state='reserved'"))

    async def test_uncertain_write_blocks_another_account_but_read_failure_releases(self):
        jobs = self.jobs('aa')
        self.clients[0].join_group.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.engine.run_job(jobs[0])
        await self.engine.run_job(jobs[1])
        self.clients[1].join_group.assert_not_awaited()
        self.assertEqual(self.store.items(jobs[1]), {1: 'reserved_skip'})
        next_jobs = self.jobs('bb')
        self.clients[0].resolve_group_by_link.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.engine.run_job(next_jobs[0])
        self.assertFalse(self.store.rows("SELECT * FROM join_registry WHERE key=?", ('link:' + entry('b')['link'],)))

    async def test_dead_links_removed_but_full_or_join_denied_stay(self):
        self.store.save_join_file('file.txt', b'file', LinkImport(entries=[entry(c) for c in 'abc']))
        job = self.store.new_join_job(self.accounts[0], [entry(c) for c in 'abc'], 1)
        self.clients[0].resolve_group_by_link.side_effect = [ApiError(opcode=89, error='not.found'), chat(-2), chat(-3)]
        self.clients[0].join_group.side_effect = [ApiError(opcode=57, error='chat.full'), ApiError(opcode=57, error='chat.join.denied')]
        await self.engine.run_job(job)
        self.assertTrue(self.store.catalog_inactive(entry('a')['link']))
        self.assertEqual(len(self.store.catalog_rows()), 2)

    async def test_join_not_found_requires_link_info_confirmation_to_archive(self):
        self.store.save_join_file('file.txt', b'file', LinkImport(entries=[entry(c) for c in 'ab']))
        job = self.store.new_join_job(self.accounts[0], [entry(c) for c in 'ab'], 1)
        self.clients[0].resolve_group_by_link.side_effect = [chat(-1), chat(-1), chat(-2), ApiError(opcode=89, error='not.found')]
        self.clients[0].join_group.side_effect = ApiError(opcode=57, error='not.found')
        await self.engine.run_job(job)
        self.assertFalse(self.store.catalog_inactive(entry('a')['link']))
        self.assertTrue(self.store.catalog_inactive(entry('b')['link']))

    async def test_old_job_snapshot_skips_now_inactive_link_without_network(self):
        self.store.save_join_file('file.txt', b'file', LinkImport(entries=[entry('a')]))
        job = self.jobs('aa')[0]
        self.store.invalidate_catalog_link(entry('a')['link'], 'expired')
        await self.engine.run_job(job)
        self.clients[0].resolve_group_by_link.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
