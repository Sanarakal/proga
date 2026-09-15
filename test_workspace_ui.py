import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
import time
import unittest
from PySide6.QtWidgets import QApplication, QPushButton, QLabel
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from workspace_ui import Window, PreviewDialog, ForwardDialog, SourceMembersDialog, CollectAccountsDialog, configure_app
from workspace_store import Store


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_app(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add_account('Test account')
        self.store.contact(self.account, 101, 'Test contact')
        self.window = Window(self.store, demo=True)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        deadline = time.monotonic() + 3
        while self.window.engine.thread.is_alive() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window.engine.thread.is_alive())
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_all_pages_and_buttons_fit(self):
        self.window.resize(920, 640)
        for index in range(8):
            self.window.nav.setCurrentRow(index)
            self.app.processEvents()
            self.assertEqual(self.window.pages.currentIndex(), index)
            for button in self.window.pages.currentWidget().findChildren(QPushButton):
                if button.isVisible():
                    minimum = button.fontMetrics().horizontalAdvance(button.text()) + 32 + (24 if not button.icon().isNull() else 0)
                    self.assertGreaterEqual(button.width(), minimum, button.text())

    def test_search_and_selection_survive_refresh(self):
        self.window.nav.setCurrentRow(2)
        self.window.contacts.selectRow(0)
        self.window.refresh()
        self.assertEqual(self.window.selected(self.window.contacts), [101])
        self.window.contacts.property('filter').setText('missing')
        self.assertTrue(self.window.contacts.isRowHidden(0))
        self.window.refresh()
        self.assertTrue(self.window.contacts.isRowHidden(0))

    def test_preview_selects_only_requested_count(self):
        preview = {'candidates': [(101, 'First'), (102, 'Second')], 'excluded': 1}
        dialog = PreviewDialog(preview, 1, self.window)
        self.assertEqual(dialog.chosen(), [101])
        self.assertFalse(dialog.consent.isChecked())
        dialog.users.item(1, 0).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(dialog.chosen(), [101, 102])
        dialog.deleteLater()

    def test_qr_dialog_renders_png_without_qt_pillow_adapter(self):
        self.window.on_engine_event('qr', (self.account, 'https://example.com/test-qr'))
        self.app.processEvents()
        dialog = self.window.qr_dialogs[self.account]
        images = [label.pixmap() for label in dialog.findChildren(QLabel) if label.pixmap() is not None and not label.pixmap().isNull()]
        self.assertEqual(len(images), 1)
        self.assertGreater(images[0].width(), 100)
        dialog.accept()

    def test_forward_dialog_filters_explicit_folder_members(self):
        chats = [{'uid': -1, 'name': 'One', 'kind': 'CHAT'},
                 {'uid': -2, 'name': 'Two', 'kind': 'CHANNEL'}]
        dialog = ForwardDialog([(7, 'Message', 1_700_000_000)], chats, [('Folder', [-2])], self.window)
        dialog.folder.setCurrentIndex(1)
        self.app.processEvents()
        self.assertTrue(dialog.targets.isRowHidden(0))
        self.assertFalse(dialog.targets.isRowHidden(1))
        dialog.deleteLater()

    def test_sources_page_shows_global_progress(self):
        other = self.store.add_account('Second account')
        self.store.update_source(-55, 'Source group', 1756)
        first_job = self.store.new_job(self.account, 'collect', -55, 1, [501])
        second_job = self.store.new_job(other, 'collect', -55, 1, [502])
        self.store.confirm_harvest(-55, 501, self.account, first_job)
        self.store.confirm_harvest(-55, 502, other, second_job)
        self.window.refresh()
        self.assertEqual(self.window.sources.rowCount(), 1)
        self.assertEqual(self.window.sources.item(0, 3).text(), '2')
        self.assertEqual(self.window.sources.cellWidget(0, 2).format(), '2 / 1756')
        source = self.store.source_rows()[0]
        dialog = SourceMembersDialog(source, self.store.source_members(-55), self.window)
        self.assertEqual(dialog.findChild(type(self.window.sources)).rowCount(), 2)
        dialog.deleteLater()

    def test_individual_sources_and_select_all(self):
        other = self.store.add_account('Second')
        for account, chat in ((self.account, -101), (other, -202)):
            self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (account, chat, 'Source', 'CHAT'))
            self.store.set_setting('collect_source_' + account, chat)
        dialog = CollectAccountsDialog(self.store, self.store.rows('SELECT * FROM accounts'), self.window)
        dialog.select_all(True)
        self.assertEqual(dict(dialog.chosen()), {self.account: -101, other: -202})
        for _, _, source in dialog.rows:
            self.assertEqual(source.count(), 2)
        self.assertFalse(dialog.start.isEnabled())
        dialog.consent.setChecked(True)
        self.assertTrue(dialog.start.isEnabled())
        dialog.rows[0][2].setEditText('invalid')
        self.assertFalse(dialog.start.isEnabled())
        dialog.select_all(False)
        self.assertEqual(dialog.chosen(), [])
        dialog.deleteLater()

    def test_chat_search_includes_dialogs_and_matches_id(self):
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, 321, 'Direct chat', 'DIALOG'))
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -456, 'Team chat', 'CHAT'))
        dialog = CollectAccountsDialog(self.store, self.store.rows('SELECT * FROM accounts'), self.window)
        source = dialog.rows[0][2]
        self.assertGreaterEqual(source.findData(321), 0)
        source.completer().setCompletionPrefix('team')
        self.assertEqual(source.completer().completionCount(), 1)
        source.completer().setCompletionPrefix('321')
        self.assertEqual(source.completer().completionCount(), 1)
        self.assertTrue(any(b.text() == 'Список' for b in dialog.findChildren(QPushButton)))
        dialog.deleteLater()

    def test_theme_switch_persists_and_restores_palette(self):
        self.window.theme.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(self.store.setting('theme', ''), 'dark')
        palette = self.app.palette()
        self.assertLess(palette.color(QPalette.ColorRole.Base).lightness(), 80)
        self.assertGreater(palette.color(QPalette.ColorRole.Text).lightness(), 200)
        self.window.theme.setCurrentIndex(0)
        self.assertEqual(self.store.setting('theme', ''), 'light')
        self.assertGreater(self.app.palette().color(QPalette.ColorRole.Base).lightness(), 200)

    def test_template_missing_account_and_consent_reset(self):
        other = self.store.add_account('Offline')
        dialog = CollectAccountsDialog(self.store, self.store.rows('SELECT * FROM accounts WHERE id=?', (self.account,)), self.window)
        dialog.consent.setChecked(True)
        missing = dialog.apply_template({'amount': 30, 'sources': {self.account: -11, other: -22}})
        self.assertEqual(missing, {other})
        self.assertIn('Offline', dialog.template_status.text())
        self.assertFalse(dialog.consent.isChecked())
        self.assertFalse(dialog.start.isEnabled())
        self.assertEqual(dialog.amount.value(), 30)
        self.assertEqual(dialog.chosen(), [(self.account, -11)])
        dialog.deleteLater()

    def test_favorites_reorder_without_changing_selected_chat(self):
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -11, 'A', 'CHAT'))
        self.store.execute('INSERT INTO chats VALUES(?,?,?,?)', (self.account, -22, 'Z', 'CHAT'))
        dialog = CollectAccountsDialog(self.store, self.store.rows('SELECT * FROM accounts'), self.window)
        source = dialog.rows[0][2]
        source.setCurrentIndex(source.findData(-22))
        dialog.select_all(True)
        dialog.favorite_selected(True)
        self.assertEqual(source.itemData(1), -22)
        self.assertEqual(dialog.chosen(), [(self.account, -22)])
        dialog.favorite_selected(False)
        self.assertEqual(source.itemData(1), -11)
        self.assertEqual(dialog.chosen(), [(self.account, -22)])
        dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
