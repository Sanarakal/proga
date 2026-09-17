import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook
from pymax.exceptions import ApiError

from workspace_links import read_links, normalize_link, formula_link, csv_value
from workspace_store import Store
from workspace_engine import Engine, JobPaused


def entry(token):
    return dict(link='https://max.ru/join/' + token, title='Group ' + token, source='1')


def chat(uid=-1, joined=False, kind='CHAT'):
    return SimpleNamespace(id=uid, title='Test group', type=kind, status='ACTIVE' if joined else 'LEFT',
                           participants={1: 0} if joined else {}, join_time=1 if joined else 0)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_normalization_and_host_validation(self):
        for link in ('https://web.max.ru/join/Ab_C-1/', 'http://max.ru/join/Ab_C-1', 'max.ru/join/Ab_C-1'):
            self.assertEqual(normalize_link(link), 'https://max.ru/join/Ab_C-1')
        for link in ('https://max.ru.evil/join/a', 'https://evil/max.ru/join/a',
                     'file:///max.ru/join/a', 'https://a@max.ru/join/a', 'https://max.ru:443/join/a',
                     'https://max.ru/join/a?redirect=evil', 'https://max.ru/id123', 'https://max.ru/join/',
                     'https://max.ru/join/a/b', 'https://max.ru/join/a\nb'):
            self.assertIsNone(normalize_link(link), link)

    def test_xlsx_headers_hyperlink_formula_duplicates_multiple_sheets(self):
        book = Workbook()
        sheet = book.active
        sheet.append(['Database'])
        sheet.append(['Название', 'Ссылка на чат', 'Описание'])
        sheet.append(['A', 'https://max.ru/join/a', 'https://max.ru/join/not-a-target'])
        sheet.append(['A copy', 'https://web.max.ru/join/a/'])
        sheet.append(['B', 'Open group'])
        sheet['B5'].hyperlink = 'https://max.ru/join/b'
        sheet.append(['C', '=HYPERLINK("https://max.ru/join/c","Open")'])
        sheet.append(['Bad', 'https://evil.example/join/a'])
        book.create_sheet('More').append(['https://max.ru/join/d'])
        filename = self.path / 'fixture.xlsx'
        book.save(filename)
        result = read_links(filename)
        self.assertEqual([r['link'] for r in result.entries], [entry(c)['link'] for c in 'abcd'])
        self.assertEqual((len(result.invalid), result.duplicates), (1, 1))

    def test_formula_is_only_parsed_never_evaluated(self):
        self.assertEqual(formula_link('=HYPERLINK("https://max.ru/join/a","Go")'), entry('a')['link'])
        self.assertIsNone(formula_link('=HYPERLINK(A1,"Go")'))
        self.assertIsNone(formula_link('=WEBSERVICE("https://max.ru/join/a")'))

    def test_csv_neutralizes_formula_titles_without_changing_numeric_ids(self):
        self.assertEqual(csv_value('=1+2'), "'=1+2")
        self.assertEqual(csv_value('  @SUM(1)'), "'  @SUM(1)")
        self.assertEqual(csv_value(-123), -123)

    def test_txt_csv_and_bad_file(self):
        filename = self.path / 'links.txt'
        filename.write_text('https://max.ru/join/a\nhttps://web.max.ru/join/a\nnot-a-link', encoding='utf-8')
        result = read_links(filename)
        self.assertEqual((len(result.entries), len(result.invalid), result.duplicates), (1, 1, 1))
        filename = self.path / 'links.csv'
        filename.write_text('Название;Ссылка\nГруппа;https://max.ru/join/b', encoding='cp1251')
        self.assertEqual(read_links(filename).entries[0]['title'], 'Группа')
        filename = self.path / 'bad.xlsx'
        filename.write_bytes(b'not a zip')
        with self.assertRaises(ValueError):
            read_links(filename)


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('First')
        self.engine = Engine(self.store)
        self.client = SimpleNamespace(me=SimpleNamespace(contact=SimpleNamespace(id=1)),
                                      resolve_group_by_link=AsyncMock(return_value=chat()),
                                      join_group=AsyncMock(return_value=chat(joined=True)))
        self.engine.clients[self.account] = self.client
        self.store.account_status(self.account, 'online')
        async def direct(account, operation, *args, **kwargs):
            return await operation(*args, **kwargs)
        self.engine.request = direct

    async def asyncTearDown(self):
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(5)
        self.temp.cleanup()

    def job(self, tokens='a', amount=1, account=None):
        return self.store.new_join_job(account or self.account, [entry(c) for c in tokens], amount)

    async def test_confirmed_and_history_survives_delete_restart(self):
        job = self.job()
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'confirmed'})
        self.assertIn('Вступил: 1 из 1', self.engine.job_report(job))
        self.store.delete_job(job)
        restored = Store(self.temp.name)
        self.assertEqual(restored.join_history(self.account)[0]['chat'], -1)
        self.assertEqual(restored.join_history(self.account)[0]['state'], 'confirmed')
        self.assertEqual(self.store.rows('SELECT uid FROM chats')[0]['uid'], -1)

    async def test_history_search_state_and_account_isolation(self):
        job = self.job('ab')
        self.store.join_target_info(job, 1, -1, 'Рабочая ГРУППА')
        self.store.item(job, 1, 'confirmed')
        self.store.item(job, 2, 'unavailable')
        self.assertEqual(len(self.store.join_history(self.account, 'рабочая группа')), 1)
        self.assertEqual(len(self.store.join_history(self.account, state='confirmed')), 1)
        self.assertFalse(self.store.join_history('another-account'))

    async def test_job_creation_is_atomic_and_rejects_invalid_links(self):
        with self.assertRaises(ValueError):
            self.store.new_join_job(self.account, [entry('a'), entry('wrong/token')], 1)
        self.assertFalse(self.store.rows('SELECT * FROM jobs'))
        self.store.execute("CREATE TRIGGER fail_join BEFORE INSERT ON join_targets BEGIN SELECT RAISE(ABORT, 'test'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.job()
        self.assertFalse(self.store.rows('SELECT * FROM jobs'))

    async def test_cancelled_write_is_pending_and_never_auto_retried(self):
        job = self.job()
        self.client.join_group.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.job(job)['status'], 'interrupted')
        self.assertEqual(self.store.items(job), {1: 'pending'})
        with self.assertRaises(ValueError):
            await self.engine.run_job(job)
        self.assertEqual(self.client.join_group.await_count, 1)

    async def test_skip_invalid_existing_channel_and_refill(self):
        job = self.job('abcde')
        self.client.resolve_group_by_link.side_effect = [
            ApiError(opcode=1, error='link.expired'), None, chat(kind='CHANNEL'), chat(joined=True), chat(-2)]
        self.client.join_group.return_value = chat(-2, True)
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'unavailable', 2: 'unavailable', 3: 'unavailable',
                                                4: 'skipped', 5: 'confirmed'})
        self.client.join_group.assert_awaited_once_with(entry('e')['link'])

    async def test_new_job_dedup_by_link_and_chat_across_all_accounts(self):
        first = self.job()
        await self.engine.run_job(first)
        second = self.job('ab')
        await self.engine.run_job(second)
        self.assertEqual(self.store.items(second), {1: 'skipped', 2: 'skipped'})
        self.assertEqual(self.client.join_group.await_count, 1)
        other = self.store.add_account('Second')
        self.engine.clients[other] = self.client
        self.store.account_status(other, 'online')
        other_job = self.job(account=other)
        await self.engine.run_job(other_job)
        self.assertEqual(self.client.join_group.await_count, 1)
        self.assertEqual(self.store.items(other_job), {1: 'global_skip'})

    async def test_quota_stops_before_resolving_extra_link(self):
        job = self.job('abc')
        await self.engine.run_job(job)
        self.client.resolve_group_by_link.assert_awaited_once()
        self.assertEqual([r['state'] for r in self.store.join_rows(job)], ['confirmed', 'queued', 'queued'])

    async def test_timeout_never_resubmits_even_from_new_job(self):
        job = self.job()
        self.client.join_group.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'pending'})
        with self.assertRaises(ValueError):
            await self.engine.run_job(job)
        with self.assertRaises(ValueError):
            await self.engine.run_job(self.job())
        self.assertEqual(self.client.join_group.await_count, 1)
        self.client.resolve_group_by_link.return_value = chat(joined=True)
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'confirmed'})
        self.assertEqual(self.client.join_group.await_count, 1)

    async def test_pending_other_link_to_same_chat_blocks_write(self):
        first = self.job('a')
        self.store.join_target_info(first, 1, -1, 'Test')
        self.store.item(first, 1, 'pending', attempted=True)
        with self.assertRaises(ValueError):
            await self.engine.run_job(self.job('b'))
        self.client.join_group.assert_not_awaited()

    async def test_unknown_response_or_approval_is_not_success(self):
        job = self.job()
        self.client.join_group.return_value = chat()
        with self.assertRaises(ValueError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'pending'})
        self.assertEqual(self.store.job(job)['status'], 'needs_review')

    async def test_write_invalid_continues_and_rate_limit_stops(self):
        job = self.job('abc')
        self.client.resolve_group_by_link.side_effect = [chat(-1), chat(-2), chat(-3)]
        self.client.join_group.side_effect = [ApiError(opcode=1, error='chat.join.denied'),
                                            ApiError(opcode=1, error='flood')]
        with self.assertRaises(ApiError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'unavailable', 2: 'retryable'})
        self.assertEqual(self.store.job(job)['status'], 'limited')
        self.assertEqual(self.client.resolve_group_by_link.await_count, 2)

    async def test_not_found_join_skips_and_fills_remaining_quota(self):
        job = self.job('abcdef', amount=5)
        self.client.resolve_group_by_link.side_effect = [chat(-i) for i in range(1, 5)] + [
            ApiError(opcode=89, error='not.found'), chat(-5), chat(-6)]
        self.client.join_group.side_effect = [chat(-1, True), chat(-2, True), chat(-3, True),
                                            ApiError(opcode=57, error='not.found'),
                                            chat(-5, True), chat(-6, True)]
        await self.engine.run_job(job)
        self.assertEqual(self.store.job(job)['status'], 'complete')
        states = self.store.items(job)
        self.assertEqual(sum(s == 'confirmed' for s in states.values()), 5)
        self.assertEqual(states[4], 'unavailable')
        self.assertNotIn('pending', states.values())

    async def test_not_found_link_info_is_skipped_without_write(self):
        job = self.job('ab')
        self.client.resolve_group_by_link.side_effect = [ApiError(opcode=89, error='not.found'), chat(-2)]
        self.client.join_group.return_value = chat(-2, True)
        await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'unavailable', 2: 'confirmed'})
        self.client.join_group.assert_awaited_once_with(entry('b')['link'])

    async def test_generic_not_found_is_scoped_to_join_operations(self):
        for code in ('not.found', 'errors.not.found'):
            for opcode in (57, 89):
                self.assertTrue(self.engine.invalid_group(ApiError(opcode=opcode, error=code)))
            for opcode in (1, 59, None):
                self.assertFalse(self.engine.invalid_group(ApiError(opcode=opcode, error=code)))

    async def test_legacy_join_rejection_repaired_without_resubmitting(self):
        job = self.job('abc', amount=2)
        self.store.item(job, 1, 'confirmed', attempted=True)
        self.store.item(job, 2, 'pending', 'Не найдено (код: not.found, операция: 57)', attempted=True)
        self.store.status(job, 'needs_review')
        self.engine.store = self.store = Store(self.temp.name)
        self.assertEqual(self.store.items(job), {1: 'confirmed', 2: 'unavailable'})
        self.assertEqual(self.store.job(job)['status'], 'needs_review')
        self.client.resolve_group_by_link.assert_not_awaited()
        await self.engine.run_job(job)
        self.client.join_group.assert_awaited_once_with(entry('c')['link'])
        self.assertEqual(self.store.items(job), {1: 'confirmed', 2: 'unavailable', 3: 'confirmed'})
        self.assertEqual(self.store.join_rows(job)[1]['attempts'], 1)

    async def test_legacy_repair_keeps_ambiguous_operations_and_other_job_types(self):
        job = self.job('abcdefg')
        details = ['Ответ MAX не получен вовремя.',
                   'Не найдено (код: not.found, операция: 89)',
                   'Не найдено (код: another.not.found, операция: 57)',
                   'Не найдено (код: not.found, операция: 57) trailing',
                   'Не найдено (код: not.found, операция: 57)',
                   'Не найдено (код: errors.not.found, операция: 57)',
                   'Не найдено (код: not.found, операция: 57)']
        for uid, detail in enumerate(details, 1):
            self.store.item(job, uid, 'pending' if uid != 7 else 'confirmed', detail, attempted=uid != 5)
        collect = self.store.new_job(self.account, 'collect', 0, 1, [1])
        self.store.item(collect, 1, 'pending', details[-1], attempted=True)
        restored = Store(self.temp.name)
        self.assertEqual(restored.items(job), {1: 'pending', 2: 'pending', 3: 'pending', 4: 'pending',
                                              5: 'pending', 6: 'unavailable', 7: 'confirmed'})
        self.assertEqual(restored.items(collect), {1: 'pending'})
        self.assertEqual(Store(self.temp.name).items(job), restored.items(job))

    async def test_unknown_api_error_and_read_timeout_do_not_mark_invalid(self):
        job = self.job()
        self.client.resolve_group_by_link.side_effect = ApiError(opcode=1, error='auth.failed')
        with self.assertRaises(ApiError):
            await self.engine.run_job(job)
        self.assertEqual(self.store.items(job), {1: 'retryable'})
        self.client.join_group.assert_not_awaited()
        self.client.resolve_group_by_link.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.engine.run_job(job)
        self.assertNotIn('unavailable', self.store.items(job).values())

    async def test_pause_after_lookup_does_not_send(self):
        job = self.job()
        async def resolve(link):
            self.engine.flags[job] = 'pause'
            return chat()
        self.client.resolve_group_by_link.side_effect = resolve
        with self.assertRaises(JobPaused):
            await self.engine.run_job(job)
        self.client.join_group.assert_not_awaited()
        self.assertEqual(self.store.job(job)['status'], 'paused')

    async def test_empty_wrong_or_mismatched_result_remains_pending(self):
        for value in (None, chat(-99, True)):
            with self.subTest(value=value):
                job = self.job('a' if value is None else 'b')
                self.client.resolve_group_by_link.return_value = chat(-1 if value is None else -2)
                self.client.join_group.return_value = value
                with self.assertRaises(ValueError):
                    await self.engine.run_job(job)
                self.assertEqual(self.store.items(job), {1: 'pending'})


if __name__ == '__main__':
    unittest.main()
