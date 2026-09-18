import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PIL import Image
from PySide6.QtCore import Qt, QMimeData, QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QPushButton
from pymax.exceptions import ApiError
from pymax.protocol import Opcode

from workspace_store import Store
from workspace_engine import Engine, JobPaused, JobStopped
from workspace_posts import next_window, validate_content
from workspace_broadcast import PostTransport, wait_for_slot
from workspace_posts_ui import PostEditor, BroadcastWizard, BroadcastReport, document_content, load_content
from transfer_workspace import export_workspace, restore_workspace


CONTENT = dict(text='Текст [скобки] * без Markdown', elements=[], photos=[])


class PostStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'source')
        self.account = self.store.add_account('Аккаунт')
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -1, 'Группа', 'CHAT'))
        self.post = self.store.save_post(None, 'Пост', CONTENT, True)

    def test_snapshot_is_immutable_and_restart_preserves_post(self):
        job = self.store.new_broadcast(self.post, self.account, [-1, -1], 'Рассылка', {'rounds': 2})
        self.store.save_post(self.post, 'Изменён', dict(text='Другой текст', elements=[], photos=[]), True)
        self.assertEqual(self.store.broadcast(job)['content'], CONTENT)
        self.assertEqual(len(self.store.broadcast_rows(job)), 2)
        restored = Store(self.store.directory)
        self.assertEqual(restored.post(self.post)['title'], 'Изменён')
        self.assertEqual(restored.job(job)['status'], 'queued')

    def test_photo_survives_source_deletion_post_deletion_and_transfer(self):
        path = self.root / 'photo.png'
        Image.new('RGB', (60, 40), 'red').save(path)
        media = self.store.add_post_photo(path)
        content = dict(text='', elements=[], photos=[media])
        self.store.save_post(self.post, 'Фото', content, True)
        job = self.store.new_broadcast(self.post, self.account, [-1], 'Фото', {})
        path.unlink()
        self.store.delete_post(self.post)
        self.assertTrue(self.store.rows('SELECT data FROM post_media WHERE id=?', (media,)))
        output = self.root / 'export'
        export_workspace(self.store.directory, output)
        restore_workspace(output / 'workspace.maxbackup', output / 'workspace.recovery-key', self.root / 'restored')
        restored = Store(self.root / 'restored')
        self.assertEqual(restored.broadcast(job)['content'], content)
        self.assertTrue(restored.rows('SELECT data FROM post_media WHERE id=?', (media,)))

    def test_other_account_rejected_but_all_chat_types_allowed(self):
        other = self.store.add_account('Другой')
        with self.assertRaises(ValueError):
            self.store.new_broadcast(self.post, other, [-1], 'Bad', {})
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -2, 'Канал', 'CHANNEL'))
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, 3, 'Диалог', 'DIALOG'))
        job = self.store.new_broadcast(self.post, self.account, [-1, -2, 3], 'Все чаты', {})
        self.assertEqual([row['chat'] for row in self.store.broadcast_rows(job)], [-1, -2, 3])

    def test_empty_draft_is_allowed_but_cannot_send(self):
        post = self.store.save_post(None, '', dict(text='', elements=[], photos=[]))
        with self.assertRaises(ValueError):
            self.store.new_broadcast(post, self.account, [-1], '', {})

    def test_emoji_offsets_and_invalid_links(self):
        validate_content(dict(text='😀ссылка', elements=[{'type': 'LINK', 'from': 2, 'length': 6, 'url': 'https://example.com/a(b)'}], photos=[]))
        with self.assertRaises(ValueError):
            validate_content(dict(text='x', elements=[{'type': 'LINK', 'from': 0, 'length': 1, 'url': 'javascript:alert(1)'}], photos=[]))
        with self.assertRaises(UnicodeError):
            validate_content(dict(text='😀', elements=[{'type': 'STRONG', 'from': 1, 'length': 1}], photos=[]))

    def test_work_hours_support_overnight(self):
        stamp = datetime(2026, 9, 17, 23, 0).timestamp()
        self.assertEqual(next_window(stamp, 22 * 60, 6 * 60), stamp)
        noon = datetime(2026, 9, 17, 12, 0).timestamp()
        self.assertEqual(datetime.fromtimestamp(next_window(noon, 22 * 60, 6 * 60)).hour, 22)
        self.assertEqual(datetime.fromtimestamp(next_window(stamp, 9 * 60, 20 * 60)).day, 18)


class BroadcastEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Account')
        for chat in (-1, -2):
            self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, chat, str(chat), 'CHAT'))
        self.post = self.store.save_post(None, 'Post', CONTENT, True)
        self.engine = Engine(self.store)
        self.engine.clients[self.account] = SimpleNamespace(is_connected=True)
        self.store.account_status(self.account, 'online')
        async def direct(account, operation, *args, **kwargs):
            return await operation(*args, **kwargs)
        self.engine.request = direct
        self.wait_patch = patch('workspace_broadcast.wait_for_slot', new_callable=AsyncMock)
        self.wait = self.wait_patch.start()
        self.send_patch = patch.object(PostTransport, 'send_post', new_callable=AsyncMock)
        self.send = self.send_patch.start()
        self.send.return_value = SimpleNamespace(id=55, chat_id=None)

    async def asyncTearDown(self):
        self.wait_patch.stop()
        self.send_patch.stop()
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(5)
        self.temp.cleanup()

    def job(self, **options):
        return self.store.new_broadcast(self.post, self.account, [-1, -2], 'Run', options)

    async def test_pending_before_send_and_confirmation_per_round(self):
        identity = self.job(rounds=2)
        async def send(target, content, attachments, cid, notify):
            self.assertEqual(list(self.store.items(identity).values()).count('pending'), 1)
            return SimpleNamespace(id=55, chat_id=target)
        self.send.side_effect = send
        await self.engine.run_job(identity)
        self.assertEqual(self.send.await_count, 4)
        self.assertEqual(set(self.store.items(identity).values()), {'confirmed'})
        self.assertEqual(self.store.job(identity)['status'], 'complete')

    async def test_timeout_is_not_retried_on_resume_or_restart(self):
        identity = self.job()
        self.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.engine.run_job(identity)
        self.assertEqual(self.store.items(identity), {1: 'pending'})
        self.store = Store(self.temp.name)
        self.engine.store = self.store
        with self.assertRaises(ValueError):
            await self.engine.run_job(identity)
        self.assertEqual(self.send.await_count, 1)
        self.store.resolve_delivery(identity, 1, 'skipped')
        self.send.side_effect = None
        await self.engine.run_job(identity)
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.store.items(identity), {1: 'skipped', 2: 'confirmed'})

    async def test_pause_after_first_send_preserves_progress(self):
        identity = self.job()
        async def send(*args):
            self.engine.flags[identity] = 'pause'
            return SimpleNamespace(id=55, chat_id=None)
        self.send.side_effect = send
        with self.assertRaises(JobPaused):
            await self.engine.run_job(identity)
        self.assertEqual(self.store.items(identity), {1: 'confirmed'})
        self.send.side_effect = None
        await self.engine.run_job(identity)
        self.assertEqual(self.send.await_count, 2)

    async def test_photo_uploaded_once_for_job_before_any_send(self):
        path = Path(self.temp.name) / 'photo.png'
        Image.new('RGB', (40, 40), 'green').save(path)
        media = self.store.add_post_photo(path)
        self.store.save_post(self.post, 'Photo', dict(text='', elements=[], photos=[media]), True)
        identity = self.job()
        with patch.object(PostTransport, 'upload_photo', new_callable=AsyncMock, return_value=object()) as upload:
            await self.engine.run_job(identity)
        upload.assert_awaited_once()
        self.assertEqual(self.send.await_count, 2)

    async def test_upload_failure_does_not_mark_message_pending(self):
        path = Path(self.temp.name) / 'photo.png'
        Image.new('RGB', (40, 40), 'green').save(path)
        media = self.store.add_post_photo(path)
        self.store.save_post(self.post, 'Photo', dict(text='', elements=[], photos=[media]), True)
        identity = self.job()
        with patch.object(PostTransport, 'upload_photo', new_callable=AsyncMock, side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                await self.engine.run_job(identity)
        self.assertEqual(self.store.items(identity), {})
        self.send.assert_not_awaited()

    async def test_imported_mixed_media_upload_order_and_video_timeout(self):
        from test_post_import import photo_bytes, MP4
        media = [self.store.prepare_import_media(photo_bytes(), 'PHOTO'),
                 self.store.prepare_import_media(MP4, 'VIDEO')]
        self.post = self.store.save_imported_post('Mixed',
            dict(text='Post', elements=[], photos=[item['id'] for item in media]), media)
        identity = self.job()
        requests = []
        async def direct(account, operation, *args, **kwargs):
            requests.append((operation, kwargs.pop('_timeout', 45)))
            if operation in (photo, video):
                self.assertEqual(self.store.items(identity), {})
            return await operation(*args, **kwargs)
        self.engine.request = direct
        with patch.object(PostTransport, 'upload_photo', new_callable=AsyncMock, return_value='photo') as photo, \
             patch.object(PostTransport, 'upload_video', new_callable=AsyncMock, return_value='video') as video:
            await self.engine.run_job(identity)
            photo.assert_awaited_once_with(media[0]['data'], media[0]['name'])
            video.assert_awaited_once_with(media[1]['data'], media[1]['name'])
            self.assertEqual(requests[:2], [(photo, 45), (video, 300)])
        self.assertEqual(self.send.await_count, 2)
        for call in self.send.await_args_list:
            self.assertEqual(call.args[2], ['photo', 'video'])

    async def test_group_denied_skip_does_not_block_account(self):
        identity = self.job()
        self.send.side_effect = [ApiError(opcode=Opcode.MSG_SEND, error='chat.write.denied'), SimpleNamespace(id=5, chat_id=-2)]
        await self.engine.run_job(identity)
        self.assertEqual(self.store.items(identity), {1: 'unavailable', 2: 'confirmed'})
        self.assertEqual(self.store.rows('SELECT connection_state FROM accounts')[0]['connection_state'], 'online')

    async def test_wrong_chat_response_requires_review(self):
        identity = self.job()
        self.send.return_value = SimpleNamespace(id=55, chat_id=-999)
        with self.assertRaises(ValueError):
            await self.engine.run_job(identity)
        self.assertEqual(self.store.items(identity), {1: 'pending'})

    async def test_unknown_server_error_stays_pending(self):
        identity = self.job()
        self.send.side_effect = ApiError(opcode=Opcode.MSG_SEND, error='unknown.failure')
        with self.assertRaises(ApiError):
            await self.engine.run_job(identity)
        self.assertEqual(self.store.items(identity), {1: 'pending'})
        with self.assertRaises(ValueError):
            await self.engine.run_job(identity)
        self.send.assert_awaited_once()

    async def test_flood_stops_remaining_targets_and_retry_is_manual(self):
        identity = self.job()
        self.send.side_effect = ApiError(opcode=Opcode.MSG_SEND, error='too-many.requests')
        with self.assertRaises(ApiError):
            await self.engine.run_job(identity)
        self.assertEqual(self.store.job(identity)['status'], 'limited')
        self.assertEqual(self.store.items(identity), {1: 'retryable'})
        self.send.assert_awaited_once()

    async def test_duplicate_intent_rejected_atomically(self):
        identity = self.job()
        self.store.begin_delivery(self.account, identity, 1)
        with self.assertRaises(ValueError):
            self.store.begin_delivery(self.account, identity, 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM broadcast_attempts')), 1)

    async def test_manual_review_does_not_move_attempt_to_another_day(self):
        identity = self.job()
        self.store.begin_delivery(self.account, identity, 1)
        before = self.store.rows('SELECT at FROM broadcast_attempts')[0]['at']
        self.store.resolve_delivery(identity, 1, 'confirmed')
        self.assertEqual(self.store.rows('SELECT at FROM broadcast_attempts')[0]['at'], before)

    async def test_end_date_before_slot_sends_nothing(self):
        identity = self.job(start_at=time.time() + 60, end_at=time.time() + 120)
        self.store.execute('UPDATE broadcasts SET next_at=? WHERE job=?', (time.time() + 180, identity))
        await self.engine.run_job(identity)
        self.send.assert_not_awaited()
        self.assertEqual(self.store.job(identity)['status'], 'complete')

    async def test_repeated_attempt_counts_toward_daily_cap(self):
        identity = self.job(daily_limit=2)
        self.store.begin_delivery(self.account, identity, 1)
        self.store.finish_delivery(identity, 1, 'retryable', 'Explicit rejection')
        self.store.begin_delivery(self.account, identity, 1)
        self.store.finish_delivery(identity, 1, 'retryable', 'Explicit rejection')
        await self.engine.run_job(identity)
        first_slot = self.wait.await_args_list[0].args[2]
        self.assertGreater(datetime.fromtimestamp(first_slot).date(), datetime.now().date())

    async def test_schedule_and_daily_limit_do_not_send_early(self):
        start = time.time() + 120
        identity = self.job(start_at=start, daily_limit=1)
        await self.engine.run_job(identity)
        first = self.wait.await_args_list[0].args[2]
        second = self.wait.await_args_list[1].args[2]
        self.assertGreaterEqual(first, start)
        self.assertGreater(second, first)
        self.assertNotEqual(datetime.fromtimestamp(first).date(), datetime.fromtimestamp(second).date())

    async def test_stop_during_wait_prevents_send(self):
        identity = self.job(start_at=time.time() + 60)
        self.wait.side_effect = JobStopped('Stop')
        with self.assertRaises(JobStopped):
            await self.engine.run_job(identity)
        self.send.assert_not_awaited()
        self.assertEqual(self.store.job(identity)['status'], 'stopped')

    async def test_suspend_detection(self):
        with patch('workspace_broadcast.time.time', side_effect=[100, 120]):
            with self.assertRaises(JobPaused):
                await wait_for_slot(self.engine, 'job', 200)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_links_are_not_reparsed_as_markdown(self):
        invoke = AsyncMock(return_value=SimpleNamespace(payload={'id': 1, 'time': 1, 'type': 'USER', 'chatId': -1}))
        transport = PostTransport(SimpleNamespace(_app=SimpleNamespace(invoke=invoke)))
        content = dict(text='😀текст [x]', elements=[{'type': 'LINK', 'from': 2, 'length': 5, 'url': 'https://example.com/a(b)'}], photos=[])
        await transport.send_post(-1, content, [], 123, True)
        payload = invoke.await_args.args[1]
        self.assertEqual(payload['message']['text'], content['text'])
        self.assertEqual(payload['message']['elements'][0]['from'], 2)
        self.assertEqual(payload['message']['elements'][0]['attributes']['url'], 'https://example.com/a(b)')
        invoke.assert_awaited_once()


class PostInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Account')
        for chat, name in ((-1, 'Alpha'), (-2, 'Beta')):
            self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, chat, name, 'CHAT'))
        self.post = self.store.save_post(None, 'Post', CONTENT, True)
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(5)

    def test_editor_roundtrip_link_bold_emoji_autosave(self):
        editor = PostEditor(self.store, self.post)
        content = dict(text='😀Ссылка\nТекст', elements=[{'type': 'LINK', 'from': 2, 'length': 6, 'url': 'https://example.com'}, {'type': 'STRONG', 'from': 9, 'length': 5}], photos=[])
        load_content(editor.editor, content)
        self.assertEqual(document_content(editor.editor, [])['text'], content['text'])
        self.assertEqual(document_content(editor.editor, [])['elements'], content['elements'])
        editor.reject()
        self.assertEqual(self.store.post(self.post)['content'], content)

    def test_filter_keeps_selection_and_account_change_clears_it(self):
        wizard = BroadcastWizard(self.store, self.engine, self.post)
        wizard.select_visible(True)
        wizard.search.setText('Alpha')
        self.assertEqual(wizard.choices, {-1, -2})
        other = self.store.add_account('Other')
        wizard.account.addItem('Other', other)
        wizard.account.setCurrentIndex(1)
        self.assertEqual(wizard.choices, set())
        wizard.reject()

    def test_all_account_chat_types_are_shown_with_type(self):
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -3, 'Channel', 'CHANNEL'))
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, 4, 'Dialog', 'DIALOG'))
        other = self.store.add_account('Other')
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (other, 5, 'Foreign', 'DIALOG'))
        wizard = BroadcastWizard(self.store, self.engine, self.post)
        wizard.select_visible(True)
        self.assertEqual(wizard.choices, {-1, -2, -3, 4})
        kinds = {wizard.targets.item(row, 2).text() for row in range(wizard.targets.rowCount())}
        self.assertEqual(kinds, {'Группа', 'Канал', 'Личный диалог'})
        wizard.reject()

    def test_connected_account_refreshes_chats_on_recipient_step(self):
        wizard = BroadcastWizard(self.store, self.engine, self.post)
        with patch.object(self.engine, 'available', return_value=True), patch.object(wizard, 'refresh_groups') as refresh:
            wizard.advance()
        refresh.assert_called_once()
        wizard.reject()

    def test_failed_autosave_does_not_silently_discard_editor(self):
        editor = PostEditor(self.store, self.post)
        editor.show()
        editor.editor.setPlainText('x' * 4001)
        self.assertFalse(editor.save_draft())
        editor.autosave.stop()
        editor.reject()
        self.assertTrue(editor.isVisible())
        self.assertTrue(editor.dirty)
        editor.editor.setPlainText('Исправлено')
        editor.reject()
        self.assertEqual(self.store.post(self.post)['content']['text'], 'Исправлено')

    def test_file_drop_in_text_editor_is_not_inserted_as_file_path(self):
        editor = PostEditor(self.store, self.post)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(Path(self.temp.name) / 'photo.png'))])
        with patch.object(editor, 'import_photos') as importer:
            editor.editor.insertFromMimeData(mime)
        importer.assert_called_once()
        self.assertEqual(editor.editor.toPlainText(), CONTENT['text'])
        editor.reject()

    def test_wizard_creates_job_without_network(self):
        wizard = BroadcastWizard(self.store, self.engine, self.post)
        wizard.advance()
        wizard.select_visible(True)
        wizard.advance()
        wizard.advance()
        wizard.consent.setChecked(True)
        wizard.advance()
        self.assertEqual(self.store.job(wizard.identity)['status'], 'queued')
        self.assertFalse(self.engine.futures)

    def test_dialogs_fit_minimum_size(self):
        identity = self.store.new_broadcast(self.post, self.account, [-1], 'Run', {})
        dialogs = [PostEditor(self.store, self.post), BroadcastWizard(self.store, self.engine, self.post), BroadcastReport(self.store, self.engine, identity)]
        for dialog in dialogs:
            dialog.resize(640, 560)
            dialog.show()
            self.app.processEvents()
            self.assertLessEqual(dialog.width(), 640)
            for control in dialog.findChildren(QPushButton):
                if control.isVisible():
                    self.assertGreaterEqual(control.width(), control.fontMetrics().horizontalAdvance(control.text()) + 32 + (24 if not control.icon().isNull() else 0), control.text())
            dialog.reject()
