import asyncio
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PIL import Image
from PySide6.QtWidgets import QApplication, QPushButton
from pymax.types import Message
from pymax.types.domain.element import Element

from workspace_store import Store
from workspace_engine import Engine
from workspace_posts_ui import PostEditor, document_content, load_content
from workspace_post_import_ui import ImportPostDialog
from workspace_post_import import import_message, message_content, check_message, media_url, download_media, message_history, message_thumbnails, ImportedPost
from workspace_broadcast import PostTransport
from transfer_workspace import export_workspace, restore_workspace


def photo_bytes(color='#287961'):
    output = BytesIO()
    Image.new('RGB', (32, 24), color).save(output, 'PNG')
    return output.getvalue()


def message(attaches=None, text='  Текст\n\n    Отступ\nСсылка', elements=None):
    return Message.model_validate(dict(id=123, chatId=-10, time=1700000000000, type='USER', text=text,
                                       elements=elements or [], attaches=attaches or []))


PHOTO = dict(_type='PHOTO', baseUrl='https://cdn.example.com/photo?token=PRIVATE', width=32, height=24,
             photoId=1, photoToken='PRIVATE')
VIDEO = dict(_type='VIDEO', height=24, width=32, videoId=2, duration=1,
             thumbnail='https://cdn.example.com/thumb', token='PRIVATE', videoType=0)
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'
KEYBOARD = dict(_type='INLINE_KEYBOARD', keyboard={'buttons': [[{'type': 'LINK', 'text': 'Site', 'url': 'https://example.com'}]]})


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data')
        self.account = self.store.add_account('Test')
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -10, 'Chat', 'CHAT'))
        self.client = SimpleNamespace(get_message=AsyncMock(), get_video_by_id=AsyncMock(), fetch_history=AsyncMock())
        async def request(account, method, *args, **kwargs):
            kwargs.pop('_timeout', None)
            return await method(*args, **kwargs)
        self.engine = SimpleNamespace(store=self.store, clients={self.account: self.client}, request=request)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_photo_video_order_text_and_format_survive_transfer(self):
        entities = [dict(type='STRONG', **{'from': 2}, length=5),
                    dict(type='LINK', **{'from': 20}, length=6, attributes={'url': 'https://example.com/a(b)'})]
        original = message([PHOTO, VIDEO], elements=entities)
        self.client.get_message.return_value = original
        self.client.get_video_by_id.return_value = SimpleNamespace(url='https://cdn.example.com/movie')
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=[photo_bytes(), MP4]):
            imported = await import_message(self.engine, self.account, -10, 123, 'Импорт')
            post_id = imported.identity
            self.assertFalse(imported.keyboard_omitted)
        post = self.store.post(post_id)
        self.assertEqual(post['content']['text'], original.text)
        self.assertEqual(post['content']['elements'][1]['url'], 'https://example.com/a(b)')
        media = [self.store.rows('SELECT * FROM post_media WHERE id=?', (identity,))[0] for identity in post['content']['photos']]
        self.assertEqual([item['kind'] for item in media], ['PHOTO', 'VIDEO'])
        self.assertEqual(media[0]['data'], photo_bytes())
        self.assertEqual(media[1]['data'], MP4)
        self.assertNotIn('PRIVATE', str(post))
        output = Path(self.temp.name) / 'export'
        export_workspace(self.store.directory, output)
        restore_workspace(output / 'workspace.maxbackup', output / 'workspace.recovery-key', Path(self.temp.name) / 'restored')
        restored = Store(Path(self.temp.name) / 'restored')
        self.assertEqual(restored.post(post_id)['content'], post['content'])
        self.assertEqual(restored.rows("SELECT data FROM post_media WHERE kind='VIDEO'")[0]['data'], MP4)

    async def test_failed_video_download_leaves_no_partial_post_or_photo(self):
        self.client.get_message.return_value = message([PHOTO, VIDEO])
        self.client.get_video_by_id.return_value = SimpleNamespace(url='https://cdn.example.com/movie')
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=[photo_bytes(), ValueError('Unavailable')]):
            with self.assertRaises(ValueError):
                await import_message(self.engine, self.account, -10, 123, 'Import')
        self.assertEqual(self.store.rows('SELECT * FROM posts'), [])
        self.assertEqual(self.store.rows('SELECT * FROM post_media'), [])

    async def test_eight_photos_and_keyboard_import_all_photos_in_order(self):
        original = message([KEYBOARD] + [dict(PHOTO, photoId=i + 1) for i in range(8)],
                           elements=[dict(type='STRONG', **{'from': 2}, length=5)])
        self.client.get_message.return_value = original
        photos = [photo_bytes((i * 30, 100, 150)) for i in range(8)]
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=photos) as download:
            result = await import_message(self.engine, self.account, -10, 123, 'Eight photos')
        self.assertTrue(result.keyboard_omitted)
        content = self.store.post(result.identity)['content']
        self.assertEqual(content['text'], original.text)
        self.assertEqual(content['elements'], message_content(original)['elements'])
        self.assertEqual(len(content['photos']), 8)
        self.assertEqual([self.store.rows('SELECT data FROM post_media WHERE id=?', (key,))[0]['data'] for key in content['photos']], photos)
        self.assertEqual(download.await_count, 8)
        self.client.get_video_by_id.assert_not_awaited()

    async def test_keyboard_does_not_count_as_photo_or_allow_other_unsupported_types(self):
        check_message(message([PHOTO] * 10 + [KEYBOARD]))
        with self.assertRaises(ValueError):
            check_message(message([PHOTO] * 11 + [KEYBOARD]))
        with self.assertRaises(ValueError):
            check_message(message([KEYBOARD], text=''))
        with self.assertRaisesRegex(ValueError, 'SHARE'):
            check_message(message([PHOTO, KEYBOARD, dict(_type='SHARE', url='https://example.com')]))

    async def test_photo_failure_with_keyboard_leaves_no_incomplete_post(self):
        self.client.get_message.return_value = message([PHOTO] * 8 + [KEYBOARD])
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=[photo_bytes(), ValueError('Photo unavailable')]):
            with self.assertRaises(ValueError):
                await import_message(self.engine, self.account, -10, 123, 'Import')
        self.assertEqual(self.store.rows('SELECT * FROM posts'), [])
        self.assertEqual(self.store.rows('SELECT * FROM post_media'), [])

    async def test_unsupported_attachment_fails_before_any_download(self):
        self.client.get_message.return_value = message([PHOTO, dict(_type='SHARE', url='https://example.com', title='Card')])
        with patch('workspace_post_import.download_media', new_callable=AsyncMock) as download:
            with self.assertRaisesRegex(ValueError, 'SHARE'):
                await import_message(self.engine, self.account, -10, 123, 'Import')
        download.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM posts'), [])

    async def test_cancellation_leaves_no_partial_import(self):
        self.client.get_message.return_value = message([PHOTO])
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await import_message(self.engine, self.account, -10, 123, 'Import')
        self.assertEqual(self.store.rows('SELECT * FROM posts'), [])

    async def test_wrong_chat_is_rejected(self):
        self.client.get_message.return_value = message()
        with self.assertRaises(ValueError):
            await import_message(self.engine, self.account, -999, 123, 'Import')

    async def test_history_paging_is_read_only(self):
        self.client.fetch_history.return_value = [message()]
        result = await message_history(self.engine, self.account, -10, 1700000000000)
        self.client.fetch_history.assert_awaited_once_with(-10, backward=40, from_=1700000000000)
        self.assertEqual(result[0].id, 123)

    async def test_thumbnails_download_photo_and_video_cover_not_video_file(self):
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, return_value=photo_bytes()) as download:
            identity, thumbnails = await message_thumbnails(message([PHOTO, VIDEO]))
        self.assertEqual(identity, 123)
        self.assertEqual([kind for kind, _ in thumbnails], ['PHOTO', 'VIDEO'])
        self.assertTrue(all(data for _, data in thumbnails))
        self.assertEqual(download.await_args_list[1].args[0], VIDEO['thumbnail'])
        self.assertEqual(download.await_args_list[0].kwargs['maximum'], 10 * 1024 * 1024)
        self.client.get_video_by_id.assert_not_awaited()

    async def test_thumbnail_failure_is_explicit_and_cancellation_propagates(self):
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=ValueError('Unavailable')):
            self.assertEqual(await message_thumbnails(message([PHOTO])), (123, [('PHOTO', None)]))
        with patch('workspace_post_import.download_media', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await message_thumbnails(message([PHOTO]))

    async def test_video_upload_and_extended_entities_use_library_models(self):
        video_upload = AsyncMock(return_value='uploaded')
        invoke = AsyncMock(return_value=SimpleNamespace(payload=dict(id=1, time=1, type='USER')))
        client = SimpleNamespace(_app=SimpleNamespace(invoke=invoke, api=SimpleNamespace(uploads=SimpleNamespace(upload_video=video_upload))))
        transport = PostTransport(client)
        await transport.upload_video(MP4, 'video.mp4')
        self.assertEqual(await video_upload.await_args.args[0].read(), MP4)
        content = dict(text='quote', elements=[{'type': 'QUOTE', 'from': 0, 'length': 5, 'attributes': {'custom': 'value'}}], photos=[])
        await transport.send_post(-10, content, [], 1, False)
        entity = invoke.await_args.args[1]['message']['elements'][0]
        self.assertEqual(entity['type'], 'QUOTE')
        self.assertEqual(entity['attributes']['custom'], 'value')


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_addresses_and_non_https_rejected(self):
        for url in ('http://example.com/a', 'https://127.0.0.1/a', 'https://[::1]/a', 'https://user:pass@example.com/a'):
            with self.assertRaises(ValueError):
                media_url(url)

    async def test_stream_size_limit_without_content_length(self):
        class Response:
            status, content_length = 200, None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def iter_chunked(self, size):
                yield b'1234'
                yield b'5678'
            @property
            def content(self):
                return self
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, *args, **kwargs):
                return Response()
        with patch('workspace_post_import.aiohttp.ClientSession', return_value=Session()), patch('workspace_post_import.aiohttp.TCPConnector'):
            with self.assertRaises(ValueError):
                await download_media('https://example.com/file', maximum=5)


class ImportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Account')
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -10, 'Chat', 'CHAT'))
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        self.engine.thread.join(5)
        self.temp.cleanup()

    def test_complex_format_readonly_preserves_exact_text_and_unknown_entities(self):
        text = '  First\n\n\tSecond\u00a0line'
        content = dict(text=text, elements=[dict(type='QUOTE', **{'from': 2}, length=5, attributes={'custom': 'x'})], photos=[])
        identity = self.store.save_post(None, 'Imported', content, True)
        editor = PostEditor(self.store, identity)
        self.assertTrue(editor.editor.isReadOnly())
        editor.title.setText('Renamed')
        editor.save_ready()
        self.assertEqual(self.store.post(identity)['content'], content)

    def test_unchanged_basic_import_retains_nbsp_and_all_attributes(self):
        content = dict(text='a\u00a0b', elements=[], photos=[])
        identity = self.store.save_post(None, 'Imported', content, True)
        editor = PostEditor(self.store, identity)
        editor.title.setText('Renamed')
        editor.save_ready()
        self.assertEqual(self.store.post(identity)['content'], content)

    def test_import_picker_is_account_scoped_and_does_not_auto_connect(self):
        other = self.store.add_account('Other')
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (other, -20, 'Other chat', 'CHANNEL'))
        dialog = ImportPostDialog(self.store, self.engine)
        self.assertEqual(dialog.chats.count(), 1)
        self.assertEqual(dialog.chats.currentData(), -10)
        dialog.account.setCurrentIndex(1)
        self.assertEqual(dialog.chats.currentData(), -20)
        self.assertFalse(self.engine.futures)
        dialog.reject()

    def test_picker_preview_does_not_show_attachment_tokens(self):
        dialog = ImportPostDialog(self.store, self.engine)
        dialog.request_context = (self.account, -10)
        dialog.engine_event('result', (self.account, ('post_import_history', id(dialog)), [message([PHOTO, VIDEO])]))
        dialog.history.selectRow(0)
        dialog.selection_changed()
        self.assertNotIn('PRIVATE', dialog.preview.toPlainText())
        self.assertIn('видео: 1', dialog.status.text())
        dialog.reject()

    def test_import_picker_fits_minimum_size(self):
        dialog = ImportPostDialog(self.store, self.engine)
        dialog.resize(640, 580)
        dialog.show()
        self.app.processEvents()
        self.assertEqual(dialog.width(), 640)
        for control in dialog.findChildren(QPushButton):
            if control.isVisible():
                self.assertGreaterEqual(control.width(), control.fontMetrics().horizontalAdvance(control.text()) + 32 + (24 if not control.icon().isNull() else 0))
        dialog.reject()

    def test_success_notifies_about_omitted_keyboard_even_for_manual_id(self):
        dialog = ImportPostDialog(self.store, self.engine)
        dialog.request_context = (self.account, -10)
        with patch('workspace_post_import_ui.QMessageBox.information') as notice:
            dialog.engine_event('result', (self.account, ('post_import_commit', id(dialog)), ImportedPost('saved-id', True)))
        self.assertEqual(dialog.identity, 'saved-id')
        self.assertEqual(dialog.result(), 1)
        notice.assert_called_once()
        self.assertIn('Кнопки MAX', notice.call_args.args[2])

    def test_older_page_advances_cursor_selects_new_rows_and_stops_at_end(self):
        dialog = ImportPostDialog(self.store, self.engine)
        def deliver(messages):
            dialog.request_context = (self.account, -10)
            dialog.engine_event('result', (self.account, ('post_import_history', id(dialog)), messages))
        first = message()
        deliver([first])
        self.assertEqual(dialog.before, first.time - 1)
        self.assertTrue(dialog.older_button.isEnabled())
        dialog.loading_older = True
        earlier = first.model_copy(update={'id': 122, 'time': first.time - 1000})
        deliver([earlier])
        self.assertEqual(dialog.history.currentRow(), 1)
        self.assertEqual(dialog.message_id.text(), '122')
        self.assertEqual(dialog.before, earlier.time - 1)
        deliver([earlier])
        self.assertFalse(dialog.older_button.isEnabled())
        self.assertTrue(dialog.exhausted)
        self.assertEqual(dialog.history.rowCount(), 2)
        dialog.clear_history()
        self.assertFalse(dialog.exhausted)
        self.assertFalse(dialog.older_button.isEnabled())
        dialog.reject()

    def test_older_button_passes_cursor_without_clearing_loaded_messages(self):
        dialog = ImportPostDialog(self.store, self.engine)
        dialog.messages = {123: message()}
        dialog.before = 1699999999999
        captured = []
        def submit(kind, operation, **kwargs):
            captured.append(kind)
            operation.close()
        with patch.object(dialog, 'submit', side_effect=submit), patch('workspace_post_import_ui.message_history', new_callable=AsyncMock) as history:
            dialog.load_history(older=True)
            history.assert_called_once_with(self.engine, self.account, -10, 1699999999999)
        self.assertIn(123, dialog.messages)
        self.assertEqual(captured, ['post_import_history'])
        dialog.reject()

    def test_thumbnail_preview_uses_local_bytes_without_saving_post(self):
        dialog = ImportPostDialog(self.store, self.engine)
        dialog.request_context = (self.account, -10)
        dialog.engine_event('result', (self.account, ('post_import_history', id(dialog)), [message([PHOTO, VIDEO])]))
        dialog.request_context = (self.account, -10)
        dialog.engine_event('result', (self.account, ('post_import_thumbnails', id(dialog)), (123, [('PHOTO', photo_bytes()), ('VIDEO', None)])))
        self.assertEqual(dialog.media.count(), 2)
        self.assertFalse(dialog.media.item(0).icon().isNull())
        self.assertEqual(self.store.rows('SELECT * FROM posts'), [])
        dialog.reject()
