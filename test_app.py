import time
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.root = app.ctk.CTk()
        self.root.withdraw()
        self.ui = app.App(self.root)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.ui.client = SimpleNamespace(
            search_by_phone=AsyncMock(return_value=SimpleNamespace(id=123)),
            get_user=AsyncMock(return_value=SimpleNamespace(id=456)),
            add_contact=AsyncMock(return_value=SimpleNamespace(id=123)),
            invite_users_to_group=AsyncMock(return_value=None),
            close=AsyncMock(),
        )

    def tearDown(self):
        self.ui.close()
        for _ in range(100):
            try:
                self.root.update()
            except app.tk.TclError:
                break
            if not self.ui.loop.is_running():
                break
            time.sleep(0.01)

    def complete(self):
        deadline = time.monotonic() + 3
        while self.ui.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.ui.busy)

    def test_validation(self):
        self.assertEqual(app.phone('+7 (912) 345-67-89'), '+79123456789')
        for bad in ['123', '+0', 'abc']:
            with self.assertRaises(ValueError):
                app.phone(bad)
        for bad in ['0', '-2', '1.5']:
            with self.assertRaises(ValueError):
                app.user_id(bad)

    def test_api_error_preserves_reason_and_redacts_phone(self):
        error = app.ApiError(opcode=46, error='contact.not.found', localized_message='Не найден +79123456789', payload={'token': 'secret'})
        result = app.api_error_text(error)
        self.assertIn('contact.not.found', result)
        self.assertIn('Не найден', result)
        self.assertNotIn('79123456789', result)
        self.assertNotIn('secret', result)

    def test_api_error_is_shown_without_retry(self):
        self.ui.client.search_by_phone.side_effect = app.ApiError(opcode=46, error='contact.not.found', message='Contact not found')
        self.ui.lookup.insert(0, '+79123456789')
        self.ui.find()
        self.complete()
        self.assertIn('contact.not.found', self.ui.status.get())
        self.ui.client.search_by_phone.assert_awaited_once()

    def test_windowed_build_without_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app.sys, 'stderr', None):
                client = app.create_client(phone='+79990000000', work_dir=directory)
                self.assertEqual(client.phone, '+79990000000')
                app.sys.stderr.close()

    def test_qr_image(self):
        data = app.qr_image_data('https://example.com/test-qr')
        decoded = data
        header, size, maximum, pixels = decoded.split(b'\n', 3)
        width, height = map(int, size.split())
        self.assertEqual(header, b'P6')
        self.assertEqual(maximum, b'255')
        self.assertEqual(len(pixels), width * height * 3)
        image = app.tk.PhotoImage(data=data, format='PPM', master=self.root)
        self.assertEqual(image.width(), width)
        self.assertIn(b'\x00\x00\x00', pixels)
        self.assertIn(b'\xff\xff\xff', pixels)

    def test_qr_login_uses_separate_session_without_phone(self):
        client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), me=object())
        self.ui.client = None
        with patch.object(app.messagebox, 'askokcancel', return_value=True), patch.object(app, 'create_client', return_value=client) as factory:
            self.ui.connect()
            self.complete()
        self.assertTrue(factory.call_args.kwargs['qr'])
        self.assertEqual(factory.call_args.kwargs['session_name'], 'test-web.db')
        self.assertNotIn('phone', factory.call_args.kwargs)
        self.assertIs(self.ui.client, client)

    def test_cancel_qr_login(self):
        async def waiting():
            await app.asyncio.Event().wait()
        self.ui.run(waiting(), lambda result: 'Unexpected success')
        self.ui.show_qr('https://example.com/test-qr')
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertIsNotNone(self.ui.qr_window)
        self.ui.cancel_login()
        self.complete()
        self.assertIsNone(self.ui.qr_window)
        self.assertEqual(self.ui.status.get(), 'Вход отменён')

    def test_phone_lookup(self):
        self.ui.lookup.insert(0, '+79123456789')
        self.ui.find()
        self.complete()
        self.assertEqual(self.ui.target, 123)
        self.ui.client.search_by_phone.assert_awaited_once_with('+79123456789')

    def test_id_lookup(self):
        self.ui.mode.set('id')
        self.ui.lookup.insert(0, '456')
        self.ui.find()
        self.complete()
        self.assertEqual(self.ui.target, 456)

    def test_contact_requires_confirmation(self):
        self.ui.target = 123
        with patch.object(app.messagebox, 'askyesno', return_value=False):
            self.ui.add()
        self.ui.client.add_contact.assert_not_awaited()
        with patch.object(app.messagebox, 'askyesno', return_value=True):
            self.ui.add()
        self.complete()
        self.ui.client.add_contact.assert_awaited_once_with(123)

    def test_invite_is_single_and_not_false_success(self):
        self.ui.target = 123
        self.ui.group.insert(0, '-999')
        with patch.object(app.messagebox, 'askyesno', return_value=True):
            self.ui.invite()
        self.complete()
        self.ui.client.invite_users_to_group.assert_awaited_once_with(-999, [123], show_history=False)
        self.assertEqual(self.ui.status.get(), 'Подтверждение приглашения не получено')


if __name__ == '__main__':
    unittest.main()
