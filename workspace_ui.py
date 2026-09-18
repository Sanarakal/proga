import asyncio
import concurrent.futures
import csv
import json
import os
import sys
import time
import tempfile
from pathlib import Path
from io import BytesIO

import qrcode
from PySide6.QtCore import Qt, QTimer, QLockFile, QStandardPaths, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtGui import QFont, QPixmap, QDesktopServices, QFontDatabase, QPalette, QColor
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QStackedWidget, QComboBox, QLineEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QDialog, QDialogButtonBox,
    QSpinBox, QFormLayout, QMessageBox, QInputDialog, QFileDialog, QProgressBar,
    QCheckBox, QStyle, QGridLayout, QToolButton, QMenu, QCompleter, QStyledItemDelegate,
)

from workspace_store import Store
from workspace_engine import Engine
from workspace_auth import normalize_phone
from workspace_status import account_display
from workspace_links import read_links, LINK_STATES, csv_value, LinkImport
from workspace_posts_ui import PostsPage, BroadcastReport


STATUS = {'queued': 'Готово к запуску', 'running': 'Выполняется', 'paused': 'На паузе',
          'stopped': 'Остановлено', 'limited': 'Ограничение MAX', 'complete': 'Завершено',
          'interrupted': 'Прервано', 'needs_review': 'Требуется проверка'}


def configure_app(app):
    fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for name in ('segoeui.ttf', 'segoeuib.ttf'):
        if (fonts / name).exists():
            QFontDatabase.addApplicationFont(str(fonts / name))
    app.setStyle('Fusion')
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, '#f5f7f8'),
                        (QPalette.ColorRole.Base, '#ffffff'),
                        (QPalette.ColorRole.Button, '#ffffff'),
                        (QPalette.ColorRole.Text, '#243238'),
                        (QPalette.ColorRole.WindowText, '#243238'),
                        (QPalette.ColorRole.ButtonText, '#243238'),
                        (QPalette.ColorRole.Highlight, '#16755b'),
                        (QPalette.ColorRole.HighlightedText, '#ffffff')):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setFont(QFont('Segoe UI', 10))
    app.setStyleSheet(STYLE)


def apply_theme(app, theme):
    dark = theme == 'dark'
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: '#202224' if dark else '#f5f7f8',
        QPalette.ColorRole.Base: '#292c2f' if dark else '#ffffff',
        QPalette.ColorRole.AlternateBase: '#303438' if dark else '#f1f4f5',
        QPalette.ColorRole.Button: '#303438' if dark else '#ffffff',
        QPalette.ColorRole.Text: '#edf0f2' if dark else '#243238',
        QPalette.ColorRole.WindowText: '#edf0f2' if dark else '#243238',
        QPalette.ColorRole.ButtonText: '#edf0f2' if dark else '#243238',
        QPalette.ColorRole.Highlight: '#285f50' if dark else '#16755b',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.PlaceholderText: '#abb3ba' if dark else '#617179',
        QPalette.ColorRole.ToolTipBase: '#303438' if dark else '#ffffff',
        QPalette.ColorRole.ToolTipText: '#edf0f2' if dark else '#243238',
        QPalette.ColorRole.Light: '#616970' if dark else '#ffffff',
        QPalette.ColorRole.Mid: '#50565c' if dark else '#ccd6db',
        QPalette.ColorRole.Dark: '#141617' if dark else '#8b999f',
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#818b93' if dark else '#8b999f'))
    app.setPalette(palette)
    app.setStyleSheet(STYLE + (DARK_STYLE if dark else ''))

STYLE = '''
QWidget { color: #243238; font-family: "Segoe UI"; font-size: 14px; }
QMainWindow, QWidget#surface { background: #f5f7f8; }
QWidget#sidebar { background: #ffffff; border-right: 1px solid #dfe5e8; }
QLabel#brand { font-size: 24px; font-weight: 700; color: #182c31; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#muted { color: #617179; font-size: 13px; }
QLabel#notice { background: #edf5f1; color: #215c49; padding: 10px; border-radius: 4px; }
QPushButton { background: #ffffff; border: 1px solid #ccd6db; padding: 8px 14px; border-radius: 6px; min-height: 20px; }
QPushButton:hover { background: #edf2f3; border-color: #97aaa9; }
QToolButton { background: #ffffff; border: 1px solid #ccd6db; border-radius: 5px; padding: 6px; min-height: 26px; }
QToolButton:hover { background: #e6f2ed; border-color: #16755b; }
QMenu { background: #ffffff; border: 1px solid #ccd6db; padding: 5px; }
QMenu::item { padding: 9px 18px; }
QMenu::item:selected { background: #e6f2ed; }
QPushButton:disabled { color: #8b999f; background: #f0f3f4; border-color: #dfe5e8; }
QPushButton#primary { background: #16755b; color: white; border-color: #16755b; }
QPushButton#primary:hover { background: #116149; }
QLineEdit, QComboBox, QSpinBox { background: #ffffff; border: 1px solid #ccd6db; border-radius: 5px; padding: 8px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus { border-color: #16755b; }
QComboBox::drop-down { border: none; width: 24px; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item { padding: 13px 14px; margin: 3px 0; border-radius: 5px; }
QListWidget::item:selected { background: #e6f2ed; color: #126348; }
QTableWidget { background: #ffffff; border: 1px solid #dfe5e8; gridline-color: #edf1f3; selection-background-color: #e6f2ed; selection-color: #183d31; }
QHeaderView::section { background: #f1f4f5; color: #586a73; border: none; border-bottom: 1px solid #dfe5e8; padding: 10px; font-size: 12px; }
QProgressBar { border: none; background: #e5ecee; border-radius: 4px; height: 8px; }
QProgressBar::chunk { background: #16755b; border-radius: 4px; }
QDialog { background: #f5f7f8; }
QTextEdit, QTextBrowser { background: #ffffff; border: 1px solid #ccd6db; padding: 8px; }
QDateTimeEdit, QTimeEdit { background: #ffffff; border: 1px solid #ccd6db; border-radius: 5px; padding: 7px; min-height: 20px; }
QTabWidget::pane { border: 1px solid #dfe5e8; }
QTabBar::tab { background: #edf1f3; padding: 8px 14px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { background: #ffffff; border-bottom-color: #16755b; }
QListWidget#postPhotos::item { padding: 3px; margin: 2px; }
'''

DARK_STYLE = '''
QWidget { color: #edf0f2; }
QMainWindow, QWidget#surface, QDialog { background: #202224; }
QWidget#sidebar { background: #26292b; border-color: #43484c; }
QLabel#brand, QLabel#title { color: #edf0f2; }
QLabel#muted { color: #abb3ba; }
QLabel#notice { background: #263c34; color: #b2e4cc; }
QPushButton, QToolButton { background: #303438; border-color: #50585e; }
QPushButton:hover, QToolButton:hover { background: #3d464b; border-color: #8cbaa8; }
QPushButton:disabled { background: #282b2e; color: #818b93; border-color: #43484c; }
QPushButton#primary { background: #16755b; color: white; }
QLineEdit, QComboBox, QSpinBox { background: #292c2f; border-color: #50585e; color: #edf0f2; }
QLineEdit:focus, QComboBox:focus { border-color: #7ed1ae; }
QMenu, QComboBox QAbstractItemView { background: #292c2f; color: #edf0f2; border-color: #50585e; selection-background-color: #285f50; selection-color: white; }
QMenu::item:selected, QListWidget::item:selected { background: #285f50; color: #edf0f2; }
QTableWidget { background: #292c2f; border-color: #43484c; gridline-color: #3a4044; selection-background-color: #285f50; selection-color: #ffffff; }
QHeaderView::section { background: #303438; color: #c3cbd1; border-color: #43484c; }
QProgressBar { background: #3a4044; color: #edf0f2; }
QProgressBar::chunk { background: #27664e; }
QToolTip { background: #303438; color: #edf0f2; border: 1px solid #687279; padding: 5px; }
QTextEdit, QTextBrowser, QDateTimeEdit, QTimeEdit { background: #292c2f; border-color: #50585e; color: #edf0f2; }
QTabWidget::pane { border-color: #43484c; }
QTabBar::tab { background: #303438; color: #c3cbd1; }
QTabBar::tab:selected { background: #292c2f; color: #edf0f2; border-bottom-color: #7ed1ae; }
'''


class StatusBadgeDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # Keep searchable item text, but draw it only once in the badge widget.
        option.text = ''


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(42)
    widget.setAlternatingRowColors(False)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.setWordWrap(False)
    return widget


def fill(widget, rows):
    if getattr(widget, '_last_rows', None) == rows:
        return
    widget._last_rows = rows
    selected = {widget.item(row.row(), 0).data(Qt.ItemDataRole.UserRole) for row in widget.selectionModel().selectedRows()}
    widget.setRowCount(len(rows))
    for index, (identity, values) in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            item.setData(Qt.ItemDataRole.UserRole, identity)
            widget.setItem(index, column, item)
        if identity in selected:
            for column in range(widget.columnCount()):
                widget.item(index, column).setSelected(True)


def message_time(value):
    timestamp = value / 1000 if value > 100_000_000_000 else value
    return time.strftime('%d.%m %H:%M', time.localtime(timestamp))


def display_time(value):
    return time.strftime('%d.%m.%Y %H:%M', time.localtime(value)) if value else '—'


class ConnectDialog(QDialog):
    def __init__(self, name, saved=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Вход в MAX / ' + name)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        self.method = QComboBox()
        if saved:
            self.method.addItem('Сохранённая сессия', 'saved')
        self.method.addItem('QR-код', 'qr')
        self.method.addItem('Номер телефона', 'phone')
        layout.addWidget(self.method)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText('+7 999 123-45-67')
        self.phone.setMaxLength(32)
        self.phone.setAccessibleName('Номер телефона')
        layout.addWidget(self.phone)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        warning = QLabel('Неофициальное подключение. Возможны ограничения MAX.')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.buttons = QDialogButtonBox()
        self.submit_button = self.buttons.addButton('Подключить', QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton('Отмена', QDialogButtonBox.ButtonRole.RejectRole)
        for button in self.buttons.buttons():
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance('Получить код') + 40)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.method.currentIndexChanged.connect(self.update_method)
        self.update_method()

    def update_method(self):
        phone = self.method.currentData() == 'phone'
        self.phone.setVisible(phone)
        self.submit_button.setText('Получить код' if phone else 'Подключить')
        self.error.clear()
        if phone:
            self.phone.setFocus()

    def accept(self):
        if self.method.currentData() == 'phone':
            try:
                self.phone.setText(normalize_phone(self.phone.text()))
            except ValueError as error:
                self.error.setText(str(error))
                return
        super().accept()


class LoginInputDialog(QDialog):
    def __init__(self, kind, detail, name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(('Код подтверждения' if kind == 'code' else 'Пароль MAX') + ' / ' + name)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        label = QLabel('Код для ' + detail if kind == 'code' else 'Пароль двухэтапной проверки')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.value = QLineEdit()
        self.value.setEchoMode(QLineEdit.EchoMode.Password)
        self.value.setAccessibleName('Код подтверждения' if kind == 'code' else 'Пароль')
        self.value.setMaxLength(10 if kind == 'code' else 256)
        layout.addWidget(self.value)
        self.buttons = QDialogButtonBox()
        self.confirm = self.buttons.addButton('Подтвердить', QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton('Отменить вход', QDialogButtonBox.ButtonRole.RejectRole)
        for button in self.buttons.buttons():
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(button.text()) + 40)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        def validate():
            text = self.value.text()
            self.confirm.setEnabled(bool(text) if kind != 'code' else
                                    4 <= len(text) <= 10 and text.isascii() and text.isdigit())
        self.value.textChanged.connect(validate)
        self.value.returnPressed.connect(lambda: self.accept() if self.confirm.isEnabled() else None)
        validate()


class CollectAccountsDialog(QDialog):
    def __init__(self, store, accounts, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle('Сбор контактов')
        self.resize(780, 520)
        self.setMinimumSize(640, 400)
        layout = QVBoxLayout(self)
        template_row = QHBoxLayout()
        self.template_selector = QComboBox()
        self.template_selector.setMinimumWidth(140)
        template_row.addWidget(self.template_selector, 1)
        for title, callback in (('Загрузить', self.load_selected_template),
                                ('Сохранить', self.save_current_template),
                                ('Удалить', self.delete_selected_template)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            template_row.addWidget(button)
        layout.addLayout(template_row)
        self.reload_templates()
        actions = QHBoxLayout()
        select_all = QPushButton('Выбрать все')
        clear = QPushButton('Снять выбор')
        actions.addWidget(select_all)
        actions.addWidget(clear)
        favorite_menu = QToolButton()
        favorite_menu.setText('Избранное')
        favorite_menu.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(favorite_menu)
        menu.addAction('Добавить выбранные чаты', lambda: self.favorite_selected(True))
        menu.addAction('Убрать выбранные чаты', lambda: self.favorite_selected(False))
        favorite_menu.setMenu(menu)
        actions.addWidget(favorite_menu)
        actions.addStretch()
        layout.addLayout(actions)
        self.accounts = table(['Аккаунт', 'Исходный чат'])
        self.accounts.setRowCount(len(accounts))
        self.accounts.verticalHeader().setDefaultSectionSize(58)
        layout.addWidget(self.accounts, 1)
        self.rows = []
        for index, account in enumerate(accounts):
            check = QCheckBox(account['name'])
            check.setContentsMargins(12, 0, 8, 0)
            check.setToolTip(account['name'])
            source = QComboBox()
            source.setEditable(True)
            source.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            source.setMinimumWidth(220)
            source.addItem('Выберите чат', None)
            favorites = store.favorites(account['id'])
            chats = store.rows('SELECT * FROM chats WHERE account=? ORDER BY name', (account['id'],))
            for chat in sorted(chats, key=lambda row: row['uid'] not in favorites):
                source.addItem(f'{chat["name"]} / {chat["uid"]}', chat['uid'])
                if chat['uid'] in favorites:
                    font = QFont(source.font())
                    font.setBold(True)
                    source.setItemData(source.count() - 1, font, Qt.ItemDataRole.FontRole)
                    source.setItemData(source.count() - 1, 'Избранный чат', Qt.ItemDataRole.ToolTipRole)
            source.setCurrentIndex(-1)
            source.lineEdit().setPlaceholderText('Название или ID чата')
            key = 'collect_source_' + account['id']
            saved = store.setting(key, '')
            if saved:
                found = source.findData(int(saved))
                if found >= 0:
                    source.setCurrentIndex(found)
                else:
                    source.setEditText(saved)
            source.completer().setFilterMode(Qt.MatchFlag.MatchContains)
            source.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            source.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            source.lineEdit().setCursorPosition(0)
            self.accounts.setCellWidget(index, 0, check)
            cell = QWidget()
            row_layout = QHBoxLayout(cell)
            row_layout.setContentsMargins(4, 4, 4, 4)
            source.setMinimumWidth(100)
            row_layout.addWidget(source, 1)
            show_list = QPushButton('Список')
            show_list.setFixedWidth(80)
            show_list.clicked.connect(source.showPopup)
            row_layout.addWidget(show_list)
            self.accounts.setCellWidget(index, 1, cell)
            self.rows.append((account['id'], check, source))
        select_all.clicked.connect(lambda: self.select_all(True))
        clear.clicked.connect(lambda: self.select_all(False))
        form = QFormLayout()
        self.amount = QSpinBox()
        self.amount.setRange(1, 1000)
        self.amount.setValue(20)
        form.addRow('Контактов на аккаунт', self.amount)
        layout.addLayout(form)
        self.consent = QCheckBox('Есть согласие участников на добавление')
        layout.addWidget(self.consent)
        self.template_status = QLabel('')
        self.template_status.setWordWrap(True)
        self.template_status.setVisible(False)
        layout.addWidget(self.template_status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.start = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.start.setText('Создать и запустить')
        self.start.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        self.consent.toggled.connect(self.update_ready)
        for _, check, source in self.rows:
            check.toggled.connect(self.update_ready)
            source.currentTextChanged.connect(self.update_ready)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def select_all(self, checked):
        for _, check, _ in self.rows:
            check.setChecked(checked)

    def chosen(self):
        result = []
        for account, check, source in self.rows:
            if not check.isChecked():
                continue
            index = source.findText(source.currentText())
            chat = source.itemData(index) if index >= 0 else int(source.currentText().strip())
            if chat is None or chat == 0 or not -(2**63) <= chat < 2**63:
                raise ValueError('Выберите исходный чат для каждого аккаунта')
            result.append((account, chat))
        return result

    def update_ready(self, *args):
        try:
            ready = bool(self.chosen())
        except ValueError:
            ready = False
        self.start.setEnabled(ready and self.consent.isChecked())

    def reload_templates(self, selected=None):
        self.template_selector.clear()
        self.template_selector.addItem('Шаблоны', None)
        for name in self.store.templates():
            self.template_selector.addItem(name, name)
        if selected:
            self.template_selector.setCurrentIndex(self.template_selector.findData(selected))

    def save_current_template(self):
        try:
            sources = self.chosen()
            if not sources:
                raise ValueError('Выберите аккаунты и исходные чаты')
        except ValueError as error:
            QMessageBox.warning(self, 'Шаблон', str(error))
            return
        name, ok = QInputDialog.getText(self, 'Сохранить шаблон', 'Название')
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.store.templates() and QMessageBox.question(self, 'Шаблон', 'Заменить существующий шаблон?') != QMessageBox.StandardButton.Yes:
            return
        self.store.save_template(name, self.amount.value(), sources)
        self.reload_templates(name)

    def load_selected_template(self):
        name = self.template_selector.currentData()
        if name:
            self.apply_template(self.store.templates()[name])

    def apply_template(self, template):
        sources = template['sources']
        available = {account for account, _, _ in self.rows}
        missing = set(sources) - available
        self.consent.setChecked(False)
        for account, check, source in self.rows:
            check.setChecked(account in sources)
            if account in sources:
                index = source.findData(sources[account])
                if index >= 0:
                    source.setCurrentIndex(index)
                else:
                    source.setEditText(str(sources[account]))
        self.amount.setValue(template['amount'])
        names = {row['id']: row['name'] for row in self.store.rows('SELECT id,name FROM accounts')}
        self.template_status.setText('Недоступны аккаунты: ' + ', '.join(names.get(account, 'Удалённый аккаунт') for account in sorted(missing)) if missing else '')
        self.template_status.setVisible(bool(missing))
        self.update_ready()
        return missing

    def delete_selected_template(self):
        name = self.template_selector.currentData()
        if name and QMessageBox.question(self, 'Удалить шаблон', f'Удалить шаблон «{name}»?') == QMessageBox.StandardButton.Yes:
            self.store.execute('DELETE FROM collection_templates WHERE name=?', (name,))
            self.reload_templates()

    def favorite_selected(self, enabled):
        try:
            selected = self.chosen()
            if not selected:
                raise ValueError('Выберите аккаунты и чаты')
        except ValueError as error:
            QMessageBox.warning(self, 'Избранное', str(error))
            return
        for account, chat in selected:
            self.store.set_favorite(account, chat, enabled)
        for account, _, source in self.rows:
            current_text = source.currentText()
            current = source.currentData()
            if current_text != source.itemText(source.currentIndex()):
                current = None
            favorites = self.store.favorites(account)
            entries = [(source.itemData(i), source.itemText(i))
                       for i in range(1, source.count())]
            source.blockSignals(True)
            source.clear()
            source.addItem('Выберите чат', None)
            for uid, text in sorted(entries, key=lambda entry: (entry[0] not in favorites, entry[1].casefold())):
                source.addItem(text, uid)
                if uid in favorites:
                    font = QFont(source.font())
                    font.setBold(True)
                    source.setItemData(source.count() - 1, font, Qt.ItemDataRole.FontRole)
                    source.setItemData(source.count() - 1, 'Избранный чат', Qt.ItemDataRole.ToolTipRole)
            if current is not None:
                source.setCurrentIndex(source.findData(current))
            else:
                source.setEditText(current_text)
            source.blockSignals(False)
            source.lineEdit().setCursorPosition(0)
        self.update_ready()


class SourceMembersDialog(QDialog):
    def __init__(self, source, members, parent):
        super().__init__(parent)
        self.setWindowTitle(f'Собранные участники — {source["title"]}')
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        total = source['total'] if source['total'] > 0 else '?'
        summary = QLabel(
            f'<b>{source["title"]}</b><br>'
            f'Источник {source["chat"]} · собрано {source["taken"]} из {total} · '
            f'аккаунтов использовано: {source["accounts"]}'
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        users = table(['ID MAX', 'Аккаунт', 'Добавлен', 'Задание'])
        fill(users, [(row['uid'], [row['uid'], row['account'], display_time(row['added']), row['job']])
                     for row in members])
        layout.addWidget(users, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('Закрыть')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class BatchReportDialog(QDialog):
    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Общий отчёт')
        self.resize(940, 540)
        self.setMinimumSize(640, 400)
        layout = QVBoxLayout(self)
        total = sum(row['amount'] for row in rows)
        done = sum(row['done'] for row in rows)
        self.summary = QLabel(f'Добавлено: {done} / {total}    Аккаунтов: {len({row["account"] for row in rows})}')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.results = table(['Аккаунт', 'Источник', 'Добавлено', 'Недоступны', 'В игноре', 'Проверить', 'Состояние'])
        self.results.horizontalHeader().setMinimumSectionSize(108)
        self.results.horizontalHeader().setStretchLastSection(True)
        fill(self.results, [(row['id'], [row['account_name'], row['source'], f'{row["done"]} / {row["amount"]}',
                                       row['unavailable'], row['ignored'], row['pending'],
                                       STATUS.get(row['status'], row['status'])]) for row in rows])
        layout.addWidget(self.results, 1)
        actions = QHBoxLayout()
        export = QPushButton('Сохранить CSV')
        export.clicked.connect(self.export_report)
        actions.addWidget(export)
        actions.addStretch()
        close = QPushButton('Закрыть')
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Общий отчёт', 'batch-report.csv', 'CSV (*.csv)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([self.results.horizontalHeaderItem(c).text() for c in range(self.results.columnCount())])
                for row in range(self.results.rowCount()):
                    writer.writerow([self.results.item(row, c).text() for c in range(self.results.columnCount())])
        except OSError as error:
            QMessageBox.warning(self, 'Экспорт', str(error))


class PreviewDialog(QDialog):
    def __init__(self, preview, amount, parent):
        super().__init__(parent)
        self.setWindowTitle('Проверка задания')
        self.resize(680, 520)
        layout = QVBoxLayout(self)
        label = QLabel(f'Доступно: {len(preview["candidates"])}. Исключено существующих или свой аккаунт: {preview["excluded"]}.')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.users = table(['Выбор', 'Участник', 'ID MAX'])
        self.users.setRowCount(len(preview['candidates']))
        for row, (uid, name) in enumerate(preview['candidates']):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if row < amount else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, uid)
            self.users.setItem(row, 0, check)
            self.users.setItem(row, 1, QTableWidgetItem(name))
            self.users.setItem(row, 2, QTableWidgetItem(str(uid)))
        layout.addWidget(self.users)
        self.consent = QCheckBox('У выбранных участников есть согласие на добавление / приглашение')
        layout.addWidget(self.consent)
        self.local_label = QLineEdit()
        self.local_label.setPlaceholderText('Например: Пачка 1')
        self.local_label.setText(preview.get('label', ''))
        if preview.get('kind') == 'collect':
            layout.addWidget(QLabel('Локальная метка добавленных контактов'))
            layout.addWidget(self.local_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Создать задание')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.consent.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen(self):
        return [self.users.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.users.rowCount()) if self.users.item(row, 0).checkState() == Qt.CheckState.Checked]


class ForwardDialog(QDialog):
    def __init__(self, messages, chats, folders, parent):
        super().__init__(parent)
        self.setWindowTitle('Новая пересылка')
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Выберите одно исходное сообщение'))
        self.messages = table(['Время', 'Сообщение', 'ID'])
        self.messages.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.messages.setMaximumHeight(230)
        fill(self.messages, [(mid, [message_time(sent), text, mid])
                             for mid, text, sent in messages])
        if self.messages.rowCount():
            self.messages.selectRow(0)
        layout.addWidget(self.messages)
        self.folder = QComboBox()
        self.folder.addItem('Все чаты', None)
        for name, included in folders:
            self.folder.addItem(name, set(included))
        layout.addWidget(QLabel('Папка получателей'))
        layout.addWidget(self.folder)
        layout.addWidget(QLabel('Выберите до 20 целевых чатов'))
        search = QLineEdit()
        search.setPlaceholderText('Поиск целевого чата')
        layout.addWidget(search)
        self.targets = table(['Выбор', 'Чат', 'Тип', 'ID'])
        self.targets.setRowCount(len(chats))
        for row, chat in enumerate(chats):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, chat['uid'])
            self.targets.setItem(row, 0, check)
            self.targets.setItem(row, 1, QTableWidgetItem(chat['name']))
            self.targets.setItem(row, 2, QTableWidgetItem(chat['kind']))
            self.targets.setItem(row, 3, QTableWidgetItem(str(chat['uid'])))
        self.folder.currentIndexChanged.connect(self.filter_targets)
        search.textChanged.connect(lambda value: self.filter_targets(search=value))
        self.target_search = search
        layout.addWidget(self.targets)
        self.consent = QCheckBox('У меня есть право переслать это сообщение выбранным получателям')
        layout.addWidget(self.consent)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Создать задание')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.consent.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def filter_targets(self, index=None, search=None):
        included = self.folder.currentData()
        query = self.target_search.text().casefold() if hasattr(self, 'target_search') else ''
        for row in range(self.targets.rowCount()):
            uid = self.targets.item(row, 0).data(Qt.ItemDataRole.UserRole)
            text = ' '.join(self.targets.item(row, col).text() for col in range(1, 4)).casefold()
            hidden = (included is not None and uid not in included) or query not in text
            self.targets.setRowHidden(row, hidden)
            if hidden:
                self.targets.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    def chosen(self):
        selected = self.messages.selectionModel().selectedRows()
        message = self.messages.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole) if selected else None
        targets = [self.targets.item(row, 0).data(Qt.ItemDataRole.UserRole)
                   for row in range(self.targets.rowCount())
                   if self.targets.item(row, 0).checkState() == Qt.CheckState.Checked]
        return message, targets


class LinkFileField(QLineEdit):
    dropped = Signal(object)

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setAcceptDrops(True)
        self.setPlaceholderText('Перетащите XLSX, CSV или TXT')

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            self.dropped.emit([url.toLocalFile() for url in urls])
            event.acceptProposedAction()


class LinkImportSignals(QObject):
    loaded = Signal(object)
    failed = Signal(str)


class LinkImportWorker(QRunnable):
    def __init__(self, filenames, store, signals):
        super().__init__()
        self.filenames, self.store, self.signals = filenames, store, signals

    def run(self):
        results = []
        for filename in self.filenames:
            try:
                path = Path(filename)
                if path.stat().st_size > 30 * 1024 * 1024:
                    raise ValueError('Файл больше 30 МБ')
                original = path.read_bytes()
                imported = read_links(filename)
                if path.read_bytes() != original:
                    raise ValueError('Файл изменился во время чтения')
                results.append(dict(file=path.name, **self.store.save_join_file(filename, original, imported)))
            except Exception as error:
                results.append(dict(file=Path(filename).name,
                                    error=str(error) if isinstance(error, (OSError, ValueError)) else 'Не удалось прочитать файл'))
        self.signals.loaded.emit(results)


class ChatCatalogDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store, self.loading = store, False
        self.setWindowTitle('База чатов')
        self.resize(1000, 680)
        self.setMinimumSize(700, 540)
        layout = QVBoxLayout(self)
        tools = QHBoxLayout()
        self.file = LinkFileField()
        self.file.dropped.connect(self.load_files)
        self.browse = QPushButton('Добавить файлы')
        self.browse.clicked.connect(self.choose_files)
        tools.addWidget(self.file, 1)
        tools.addWidget(self.browse)
        layout.addLayout(tools)
        filters = QHBoxLayout()
        self.folder = QComboBox()
        for folder in store.rows('SELECT * FROM chat_folders ORDER BY id'):
            self.folder.addItem(folder['name'], folder['id'])
        self.mode = QComboBox()
        for name, value in (('Все в базе', 'active'), ('Свободные', 'free'),
                            ('Неактивные ссылки', 'inactive'), ('Объединённые ссылки', 'duplicate')):
            self.mode.addItem(name, value)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Название, ссылка или ID')
        filters.addWidget(self.folder)
        filters.addWidget(self.mode)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        self.result = QLabel('')
        self.result.setWordWrap(True)
        self.result.setObjectName('muted')
        layout.addWidget(self.result)
        self.chats = table(['Группа', 'Состояние', 'Аккаунт', 'Ссылка'])
        layout.addWidget(self.chats, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('Закрыть')
        details = buttons.addButton('Подробности', QDialogButtonBox.ButtonRole.ActionRole)
        details.clicked.connect(self.details)
        self.start = buttons.addButton('Вступить', QDialogButtonBox.ButtonRole.AcceptRole)
        self.start.setObjectName('primary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.signals = LinkImportSignals()
        self.signals.loaded.connect(self.imported)
        self.folder.currentIndexChanged.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.search_timer.start(150))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.finished.connect(self.timer.stop)
        self.finished.connect(self.search_timer.stop)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start(1500)

    def hideEvent(self, event):
        self.timer.stop()
        self.search_timer.stop()
        super().hideEvent(event)

    def refresh(self):
        folder, mode = self.folder.currentData(), self.mode.currentData()
        counts = self.store.catalog_counts(folder)
        self.stats.setText(f'В базе: {counts.get("active", 0)} · Свободно: {counts["free"]} · '
                           f'Неактивных: {counts.get("inactive", 0)} · Объединено: {counts.get("duplicate", 0)}')
        self.rows = self.store.catalog_rows(folder, 'active' if mode == 'free' else mode,
                                             self.search.text(), only_free=mode == 'free')
        labels = dict(confirmed='Вступил', skipped='Уже состоит', pending='Не подтверждено', reserved='Закреплён')
        fill(self.chats, [(r['link'], [r['title'], 'Неактивна' if r['state'] == 'inactive' else
              'Объединена' if r['state'] == 'duplicate' else labels.get(r['taken'], 'Свободен'),
              r['account_name'], r['link']]) for r in self.rows])
        self.start.setEnabled(bool(counts['free'] and not self.loading))
        self.chats.setToolTip('Показано до 1000 результатов. Поиск выполняется по всей базе.')

    def choose_files(self):
        filenames, _ = QFileDialog.getOpenFileNames(self, 'Добавить в базу', '', 'Списки групп (*.xlsx *.csv *.txt)')
        if filenames:
            self.load_files(filenames)

    def load_files(self, filenames):
        if self.loading or not filenames:
            return
        self.loading = True
        self.browse.setEnabled(False)
        self.file.setText(f'Импорт файлов: {len(filenames)}')
        self.result.setText('Импорт...')
        self.refresh()
        QThreadPool.globalInstance().start(LinkImportWorker(filenames, self.store, self.signals))

    def imported(self, results):
        self.loading = False
        self.browse.setEnabled(True)
        self.file.clear()
        good = [r for r in results if 'error' not in r]
        self.result.setText(f'Добавлено: {sum(r["added"] for r in good)} · Уже в базе: {sum(r["existing"] for r in good)} · '
                            f'Неверный формат: {sum(r["invalid"] for r in good)} · Повторы в файлах: {sum(r["duplicates"] for r in good)}')
        errors = [r['file'] + ': ' + r['error'] for r in results if 'error' in r]
        if errors:
            self.result.setText(self.result.text() + f' · Ошибок файлов: {len(errors)}')
            QMessageBox.warning(self, 'Импорт файлов', '\n'.join(errors[:10]))
        self.refresh()

    def details(self):
        selected = self.chats.selectionModel().selectedRows()
        if len(selected) != 1:
            return
        link = self.chats.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        row = next(r for r in self.rows if r['link'] == link)
        QMessageBox.information(self, 'Чат', '\n'.join([
            row['title'], row['link'], f'ID: {row["chat"] or "ещё не определён"}',
            f'Источник: {row["import_name"]}', f'Аккаунт: {row["account_name"] or "не закреплён"}', row['reason'],
        ]))


class JoinLinksDialog(QDialog):
    def __init__(self, accounts, parent=None, store=None, folder='all'):
        super().__init__(parent)
        self.store = store or getattr(parent, 'store', None)
        self.setWindowTitle('Вступление из базы чатов')
        self.resize(800, 660)
        self.setMinimumSize(640, 580)
        self.imported = None
        self.loading = False
        layout = QVBoxLayout(self)
        self.folder = QComboBox()
        if self.store:
            for row in self.store.rows('SELECT * FROM chat_folders ORDER BY id'):
                self.folder.addItem(row['name'], row['id'])
            self.folder.setCurrentIndex(max(0, self.folder.findData(folder)))
        layout.addWidget(self.folder)
        self.summary = QLabel('Нет свободных чатов')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.preview = table(['Группа', 'Ссылка', 'Источник', 'Состояние'])
        self.preview.verticalHeader().setDefaultSectionSize(32)
        layout.addWidget(self.preview, 1)
        select_row = QHBoxLayout()
        select_row.addWidget(QLabel('Аккаунты'))
        select_row.addStretch()
        for label, checked in (('Выбрать все', True), ('Снять выбор', False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, checked=checked: self.select_all(checked))
            select_row.addWidget(button)
        layout.addLayout(select_row)
        self.accounts = table(['Аккаунт'])
        self.accounts.setMinimumHeight(90)
        self.accounts.setMaximumHeight(150)
        self.accounts.horizontalHeader().hide()
        self.accounts.setRowCount(len(accounts))
        self.checks = []
        for index, account in enumerate(accounts):
            check = QCheckBox(account['name'])
            check.setChecked(len(accounts) == 1)
            check.toggled.connect(self.update_ready)
            self.accounts.setCellWidget(index, 0, check)
            self.checks.append((account['id'], check))
        layout.addWidget(self.accounts)
        form = QFormLayout()
        self.amount = QSpinBox()
        self.amount.setRange(1, 1000)
        self.amount.setValue(10)
        form.addRow('Новых групп на каждый аккаунт', self.amount)
        layout.addLayout(form)
        self.consent = QCheckBox('Подтверждаю вступления для выбранных аккаунтов')
        self.consent.toggled.connect(self.update_ready)
        layout.addWidget(self.consent)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        self.start = buttons.addButton('Начать вступление', QDialogButtonBox.ButtonRole.AcceptRole)
        self.start.setObjectName('primary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.folder.currentIndexChanged.connect(self.reload_catalog)
        self.reload_catalog()
        self.update_ready()

    def chosen(self):
        return [account for account, check in self.checks if check.isChecked()]

    def select_all(self, enabled):
        for _, check in self.checks:
            check.setChecked(enabled)

    def update_ready(self):
        if hasattr(self, 'start'):
            self.start.setEnabled(bool(not self.loading and self.imported and self.imported.entries
                                       and self.chosen() and self.consent.isChecked()))

    def reload_catalog(self):
        self.consent.setChecked(False)
        if self.store:
            entries = [dict(link=r['link'], title=r['title'], source=r['import_name']) for r in
                       self.store.catalog_rows(self.folder.currentData(), limit=-1, only_free=True)]
            self.loaded(LinkImport(entries=entries))

    def loaded(self, result):
        self.loading, self.imported = False, result
        self.summary.setText(f'Свободных ссылок: {len(result.entries)} · Предпросмотр: первые 200')
        entries = [(i, [r['title'], r['link'], r['source'], 'Свободен'])
                   for i, r in enumerate(result.entries[:200])]
        entries += [(f'invalid-{i}', ['', r['link'], r['source'], 'Пропуск'])
                    for i, r in enumerate(result.invalid[:max(0, 200 - len(entries))])]
        fill(self.preview, entries)
        self.update_ready()

    def failed(self, message):
        self.loading, self.imported = False, None
        self.summary.setText(message)
        self.update_ready()


class Window(QMainWindow):
    def __init__(self, store, demo=False):
        super().__init__()
        self.store = store
        apply_theme(QApplication.instance(), store.setting('theme', 'light'))
        self.demo = demo
        self.engine = Engine(store)
        self.engine.event.connect(self.on_engine_event)
        self.qr_dialogs = {}
        self.closing = False
        self.setWindowTitle('MAX Workspace — 1.10')
        self.resize(1180, 780)
        self.setMinimumSize(920, 640)
        outer = QWidget()
        outer.setObjectName('surface')
        self.setCentralWidget(outer)
        shell = QHBoxLayout(outer)
        shell.setContentsMargins(0, 0, 0, 0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 26, 18, 20)
        brand = QLabel('MAX\nWorkspace')
        brand.setObjectName('brand')
        side.addWidget(brand)
        side.addSpacing(25)
        self.nav = QListWidget()
        self.names = ['Аккаунты', 'Чаты', 'Источники', 'Контакты', 'Задания', 'Пересылка', 'Вступления', 'Журнал', 'Настройки']
        self.nav.addItems(self.names)
        side.addWidget(self.nav, 1)
        version = QLabel('Windows / версия 1.10')
        version.setObjectName('muted')
        side.addWidget(version)
        shell.addWidget(sidebar)
        main = QVBoxLayout()
        main.setContentsMargins(28, 24, 28, 22)
        shell.addLayout(main, 1)
        top = QHBoxLayout()
        self.title = QLabel('Аккаунты')
        self.title.setObjectName('title')
        top.addWidget(self.title)
        top.addStretch()
        self.account_selector = QComboBox()
        self.account_selector.setMinimumWidth(240)
        self.account_selector.setMaximumWidth(340)
        self.account_selector.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.account_selector)
        main.addLayout(top)
        self.notice = QLabel('Выберите или добавьте аккаунт MAX')
        self.notice.setObjectName('notice')
        self.notice.setWordWrap(True)
        main.addWidget(self.notice)
        self.pages = QStackedWidget()
        main.addWidget(self.pages, 1)
        self.accounts = self.make_page(['Аккаунт', 'ID MAX', 'Статус', 'Подробности'], [
            ('Добавить', self.add_account, QStyle.StandardPixmap.SP_FileDialogNewFolder),
            ('Подключить', self.connect_account, QStyle.StandardPixmap.SP_DialogApplyButton),
            ('Проверить статус', self.check_account_status, None),
            ('Отключить', self.disconnect_account, QStyle.StandardPixmap.SP_DialogCancelButton),
            ('Переименовать', self.rename_account, None),
            ('Удалить выбранный аккаунт', self.remove_account, QStyle.StandardPixmap.SP_TrashIcon),
        ])
        self.accounts.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.accounts.setColumnWidth(1, 100)
        self.accounts.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.accounts.setColumnWidth(2, 176)
        self.accounts.setItemDelegateForColumn(2, StatusBadgeDelegate(self.accounts))
        self.accounts.cellClicked.connect(lambda row, column: self.account_selector.setCurrentIndex(self.account_selector.findData(self.accounts.item(row, 0).data(Qt.ItemDataRole.UserRole))))
        self.chats = self.make_page(['Название чата', 'Тип', 'ID MAX'], [
            ('Обновить', self.refresh_chats, QStyle.StandardPixmap.SP_BrowserReload),
            ('В контакты', lambda: self.prepare('collect'), None),
        ])
        self.sources = self.make_page(
            ['Источник', 'ID MAX', 'Взято из группы', 'Аккаунтов', 'Последний сбор'], [
                ('Участники', self.show_source_members, QStyle.StandardPixmap.SP_FileDialogDetailedView),
                ('CSV', self.export_source, QStyle.StandardPixmap.SP_DialogSaveButton),
            ])
        self.sources.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.sources.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.contacts = self.make_page(['Контакт', 'ID MAX', 'Метка', 'Источник'], [
            ('Найти', self.lookup, QStyle.StandardPixmap.SP_FileDialogContentsView),
            ('Импорт списка', self.import_list, QStyle.StandardPixmap.SP_DialogOpenButton),
            ('Пригласить', lambda: self.prepare('invite'), None),
            ('CSV', lambda: self.export(self.contacts, 'contacts'), QStyle.StandardPixmap.SP_DialogSaveButton),
        ])
        self.jobs = self.make_page(['Задание', 'Состояние', 'Прогресс', 'Результат'], [
            ('Несколько аккаунтов', self.prepare_multi, None),
            ('Чат → контакты', lambda: self.prepare('collect'), None),
            ('Контакты → группа', lambda: self.prepare('invite'), None),
            ('Запустить', self.resume_job, QStyle.StandardPixmap.SP_MediaPlay),
            ('Пауза', lambda: self.control_job('pause'), QStyle.StandardPixmap.SP_MediaPause),
            ('Стоп', lambda: self.control_job('stop'), QStyle.StandardPixmap.SP_MediaStop),
            ('Ручная проверка', self.resolve_job, QStyle.StandardPixmap.SP_DialogResetButton),
            ('Отчёт', self.show_job_report, QStyle.StandardPixmap.SP_FileDialogInfoView),
            ('Общий отчёт', self.show_batch_report, None),
            ('CSV', self.export_job, QStyle.StandardPixmap.SP_DialogSaveButton),
            ('Удалить', self.delete_job, QStyle.StandardPixmap.SP_TrashIcon),
        ], compact=True)
        self.posts_page = PostsPage(store, self.engine, self)
        self.pages.addWidget(self.posts_page)
        self.joins = self.make_page(['Дата', 'Группа', 'Результат', 'Ссылка'], [
            ('База чатов', self.open_catalog, None),
            ('Новое задание', self.prepare_join, None),
            ('Подробности', self.join_details, None),
            ('CSV', self.export_join_history, None),
        ])
        self.join_state = QComboBox()
        self.join_state.addItem('Все результаты', '')
        for state, title in LINK_STATES.items():
            if state != 'queued':
                self.join_state.addItem(title, state)
        self.joins.parentWidget().layout().insertWidget(1, self.join_state)
        self.join_state.currentIndexChanged.connect(self.refresh_join_history)
        self.joins.property('filter').setPlaceholderText('Поиск в истории аккаунта · последние 1000 результатов')
        self.joins.property('filter').textChanged.connect(self.refresh_join_history)
        self.logs = self.make_page(['Время', 'Аккаунт', 'Сообщение'], [
            ('CSV', lambda: self.export(self.logs, 'logs'), QStyle.StandardPixmap.SP_DialogSaveButton),
        ])
        settings = QWidget()
        form = QFormLayout(settings)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        self.theme = QComboBox()
        self.theme.addItem('Светлая', 'light')
        self.theme.addItem('Тёмная', 'dark')
        self.theme.setCurrentIndex(max(0, self.theme.findData(store.setting('theme', 'light'))))
        self.theme.currentIndexChanged.connect(self.change_theme)
        form.addRow('Тема', self.theme)
        self.delay = QSpinBox()
        self.delay.setRange(1, 30)
        self.delay.setSuffix(' сек.')
        self.delay.setValue(int(float(store.setting('request_delay', '2'))))
        form.addRow('Интервал добавлений', self.delay)
        self.read_delay = QSpinBox()
        self.read_delay.setRange(1, 30)
        self.read_delay.setSuffix(' сек.')
        self.read_delay.setValue(int(float(store.setting('read_delay', '2'))))
        form.addRow('Интервал чтения', self.read_delay)
        form.addRow(self.button('Сохранить', self.save_settings))
        form.addRow(self.button('Обновить кэш источников', self.clear_source_cache))
        form.addRow(self.button('Открыть папку данных', self.open_data))
        form.addRow(self.button('Импортировать прежнюю сессию v5', self.import_legacy))
        security = QLabel('Сессии защищены Windows DPAPI после отключения. Во время работы и после аварийного завершения рабочая база сессии может оставаться незашифрованной. Не передавайте папку данных посторонним.\n\nИспользуется неофициальный API MAX. Ограничения сервиса и приватность участников сохраняются. Обновление приложения выполняется вручную.')
        security.setWordWrap(True)
        security.setObjectName('muted')
        form.addRow(security)
        self.pages.addWidget(settings)
        self.nav.currentRowChanged.connect(self.navigate)
        self.nav.setCurrentRow(0)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        main.addWidget(self.progress)
        self.footer = QLabel('Готово')
        self.footer.setObjectName('muted')
        self.footer.setWordWrap(True)
        main.addWidget(self.footer)
        self.reload_accounts()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)

    def button(self, text, callback, icon=None):
        button = QPushButton(text)
        button.ensurePolished()
        button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + 32 + (24 if icon else 0))
        if icon:
            button.setIcon(self.style().standardIcon(icon))
        button.clicked.connect(lambda checked=False: self.guarded(callback))
        return button

    def guarded(self, callback):
        try:
            if self.demo:
                return
            callback()
        except (ValueError, KeyError, OSError) as error:
            QMessageBox.warning(self, 'MAX Workspace', str(error))

    def make_page(self, headers, actions, compact=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        tools = QGridLayout()
        offset = 0
        if compact:
            create = QToolButton()
            create.setText('Создать')
            create.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(create)
            for name, callback, _ in actions[:3]:
                action = menu.addAction(name)
                action.triggered.connect(lambda checked=False, callback=callback: self.guarded(callback))
            create.setMenu(menu)
            create.setMinimumHeight(40)
            tools.addWidget(create, 0, 0)
            offset = 1
            actions = actions[3:]
        for index, (name, callback, icon) in enumerate(actions, offset):
            label = 'Удалить аккаунт' if name == 'Удалить выбранный аккаунт' else name
            tools.addWidget(self.button(label, callback), index // 3, index % 3)
        for column in range(3):
            tools.setColumnStretch(column, 1)
        layout.addLayout(tools)
        search = QLineEdit()
        search.setPlaceholderText('Поиск в таблице')
        layout.addWidget(search)
        widget = table(headers)
        widget.setProperty('filter', search)
        search.textChanged.connect(lambda text: self.filter_table(widget, text))
        layout.addWidget(widget, 1)
        self.pages.addWidget(page)
        return widget

    def filter_table(self, widget, text):
        for row in range(widget.rowCount()):
            widget.setRowHidden(row, not any(text.casefold() in widget.item(row, column).text().casefold() for column in range(widget.columnCount())))

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.title.setText(self.names[index])

    def account(self, connected=False):
        account = self.account_selector.currentData()
        if not account:
            raise ValueError('Сначала добавьте аккаунт')
        if connected and not self.engine.available(account):
            raise ValueError('Аккаунт недоступен. Проверьте его статус в разделе «Аккаунты».')
        return account

    def reload_accounts(self, select=None):
        previous = select or self.account_selector.currentData()
        self.account_selector.blockSignals(True)
        self.account_selector.clear()
        for account in self.store.rows('SELECT * FROM accounts ORDER BY rowid'):
            self.account_selector.addItem(account['name'], account['id'])
        index = self.account_selector.findData(previous)
        if index >= 0:
            self.account_selector.setCurrentIndex(index)
        self.account_selector.blockSignals(False)
        self.refresh()

    def refresh(self):
        if self.closing:
            return
        account = self.account_selector.currentData()
        accounts = self.store.rows('SELECT * FROM accounts ORDER BY rowid')
        states = {row['id']: account_display(row, self.engine.connected(row['id'])) for row in accounts}
        fill(self.accounts, [(row['id'], [row['name'], row['max_id'] or '—', states[row['id']][1], states[row['id']][3] or '—']) for row in accounts])
        dark = self.store.setting('theme', 'light') == 'dark'
        colors = ({'green': ('#194535', '#9be7bd'), 'yellow': ('#4c4118', '#ffe28a'),
                   'red': ('#542b31', '#ffb5bc'), 'gray': ('#363c40', '#c7cfd5')} if dark else
                  {'green': ('#e0f4e9', '#12613d'), 'yellow': ('#fff1bd', '#705400'),
                   'red': ('#fde5e7', '#a12437'), 'gray': ('#e9edef', '#52616a')})
        for index, row in enumerate(accounts):
            _, title, color, detail = states[row['id']]
            cell = self.accounts.cellWidget(index, 2)
            if cell is None:
                cell = QWidget()
                cell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                layout = QHBoxLayout(cell)
                layout.setContentsMargins(5, 6, 5, 6)
                badge = QLabel()
                badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(badge)
                self.accounts.setCellWidget(index, 2, cell)
            badge = cell.findChild(QLabel)
            badge.setText(title)
            background, foreground = colors[color]
            badge.setStyleSheet(f'background:{background};color:{foreground};border-radius:6px;font-size:12px;font-weight:600;padding:2px 6px;')
            tooltip = '\n'.join([title, detail, 'Обновлён: ' + display_time(row['status_changed']),
                                 'Ответ MAX: ' + display_time(row['status_checked'])])
            cell.setToolTip(tooltip)
            self.accounts.item(index, 2).setToolTip(tooltip)
            self.accounts.item(index, 3).setToolTip(tooltip)
        chat_rows = self.store.rows('SELECT * FROM chats WHERE account=? ORDER BY name', (account,))
        kind_names = {'CHAT': 'Группа', 'CHANNEL': 'Канал', 'DIALOG': 'Диалог'}
        fill(self.chats, [(row['uid'], [row['name'], kind_names.get(row['kind'], row['kind']), row['uid']]) for row in chat_rows])
        self.posts_page.refresh()
        fill(self.contacts, [(row['uid'], [row['name'] or 'Без имени', row['uid'], row.get('label', ''), row['source']]) for row in self.store.contacts(account)])
        self.fill_sources()
        jobs = self.store.rows('''SELECT j.*,a.name AS account_name,COALESCE(i.done,0) AS done
            FROM jobs j LEFT JOIN accounts a ON a.id=j.account
            LEFT JOIN (SELECT job,COUNT(*) AS done FROM items WHERE state='confirmed' GROUP BY job) i ON i.job=j.id
            WHERE j.deleted=0 ORDER BY j.created DESC''')
        job_names = {'collect': 'В контакты', 'invite': 'Приглашения', 'forward': 'Пересылка', 'join': 'Вступления', 'broadcast': 'Рассылка поста'}
        fill(self.jobs, [(row['id'], [f'{row["account_name"]}: ' + job_names.get(row['kind'], row['kind']) + ('' if row['kind'] == 'join' else f' / {row["chat"] or "вручную"}'), STATUS.get(row['status'], row['status']), f'{row["done"]} / {row["amount"]}', (row['message'] or '—').splitlines()[0]]) for row in jobs])
        self.refresh_join_history()
        names = {row['id']: row['name'] for row in accounts}
        fill(self.logs, [(row['id'], [time.strftime('%d.%m %H:%M:%S', time.localtime(row['at'])), names.get(row['account'], '—'), row['message']]) for row in self.store.rows('SELECT * FROM logs ORDER BY id DESC LIMIT 500')])
        for widget in (self.accounts, self.chats, self.sources, self.contacts, self.jobs, self.logs):
            self.filter_table(widget, widget.property('filter').text())
        busy = bool(account and self.engine.busy(account))
        self.progress.setRange(0, 0 if busy else 100)
        self.progress.setValue(0)
        if account:
            _, title, color, _ = states[account]
            self.notice.setText(title + f'  ·  Контактов: {len(self.store.contacts(account))}  ·  Аккаунтов: {len(accounts)} / 20')
            background, foreground = colors[color]
            self.notice.setStyleSheet(f'background:{background};color:{foreground};')
        else:
            self.notice.setText('Выберите или добавьте аккаунт MAX')
            self.notice.setStyleSheet('')

    def fill_sources(self):
        rows = self.store.source_rows()
        if getattr(self, '_source_snapshot', None) == rows:
            return
        self._source_snapshot = rows
        selected = set(self.selected(self.sources))
        self.sources.setRowCount(len(rows))
        for row_index, source in enumerate(rows):
            total = source['total'] if source['total'] > 0 else None
            values = [source['title'] or f'Группа {source["chat"]}', source['chat'],
                      '', source['accounts'],
                      display_time(source['last_added'])]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, source['chat'])
                item.setToolTip(str(value))
                self.sources.setItem(row_index, column, item)
            progress = QProgressBar()
            progress.setTextVisible(True)
            progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
            progress.setRange(0, max(total or source['taken'] or 1, source['taken']))
            progress.setValue(source['taken'])
            progress.setFormat(f'{source["taken"]} / {total if total is not None else "?"}')
            self.sources.setCellWidget(row_index, 2, progress)
            if source['chat'] in selected:
                self.sources.selectRow(row_index)

    def selected_source(self):
        selected = self.selected(self.sources)
        if len(selected) != 1:
            raise ValueError('Выберите один источник')
        matches = [row for row in self.store.source_rows() if row['chat'] == selected[0]]
        if not matches:
            raise ValueError('Источник не найден')
        return matches[0]

    def show_source_members(self):
        source = self.selected_source()
        SourceMembersDialog(source, self.store.source_members(source['chat']), self).exec()

    def export_source(self):
        source = self.selected_source()
        path, _ = QFileDialog.getSaveFileName(
            self, 'Экспорт источника', f'source-{source["chat"]}.csv', 'CSV (*.csv)')
        if not path:
            return
        with open(path, 'w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['ID MAX', 'Аккаунт', 'Добавлен', 'Задание'])
            for row in self.store.source_members(source['chat']):
                writer.writerow([row['uid'], row['account'], display_time(row['added']), row['job']])
        self.footer.setText(f'Реестр источника сохранён: {source["taken"]} участников')

    def selected(self, widget):
        selection = widget.selectionModel().selectedRows()
        return [widget.item(row.row(), 0).data(Qt.ItemDataRole.UserRole) for row in selection if not widget.isRowHidden(row.row())]

    def add_account(self):
        name, ok = QInputDialog.getText(self, 'Новый аккаунт', 'Название аккаунта')
        if ok and name.strip():
            self.reload_accounts(self.store.add_account(name.strip()))

    def connect_account(self):
        account = self.account()
        if self.engine.busy(account):
            raise ValueError('Дождитесь завершения текущей операции этого аккаунта')
        row = self.store.rows('SELECT connection_state FROM accounts WHERE id=?', (account,))[0]
        if self.engine.connected(account) and row['connection_state'] not in ('needs_login', 'blocked'):
            self.footer.setText('Аккаунт уже подключён')
            return
        dialog = ConnectDialog(self.account_selector.currentText(), self.engine.vault.has_session(account), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        method = dialog.method.currentData()
        phone = dialog.phone.text() if method == 'phone' else ''
        dialog.phone.clear()
        dialog.deleteLater()
        waiting = QDialog(self)
        waiting.setWindowTitle('Подключение MAX / ' + self.account_selector.currentText())
        layout = QVBoxLayout(waiting)
        layout.addWidget(QLabel('Запрос кода в MAX…' if method == 'phone' else 'Подключение к MAX…'))
        cancel = QPushButton('Отменить вход')
        cancel.clicked.connect(waiting.reject)
        layout.addWidget(cancel)
        waiting.rejected.connect(lambda: self.cancel_login(account))
        self.qr_dialogs[account] = waiting
        self.engine.submit(account, 'connect', self.engine.connect(account, method, phone))
        waiting.show()

    def cancel_login(self, account):
        if self.engine.busy(account):
            self.engine.futures[account].cancel()

    def close_login_dialog(self, account):
        dialog = self.qr_dialogs.pop(account, None)
        if dialog:
            dialog.blockSignals(True)
            if isinstance(dialog, LoginInputDialog):
                dialog.value.clear()
            dialog.close()
            dialog.deleteLater()

    def disconnect_account(self):
        account = self.account()
        self.engine.submit(account, 'disconnect', self.engine.disconnect(account))

    def check_account_status(self):
        account = self.account()
        self.engine.submit(account, 'check_status', self.engine.check_account(account))

    def rename_account(self):
        account = self.account()
        name, ok = QInputDialog.getText(self, 'Аккаунт', 'Название', text=self.account_selector.currentText())
        if ok and name.strip():
            self.store.execute('UPDATE accounts SET name=? WHERE id=?', (name.strip(), account))
            self.reload_accounts(account)

    def refresh_chats(self):
        account = self.account(True)
        self.engine.submit(account, 'refresh', self.engine.refresh(account))

    def prepare(self, kind):
        account = self.account(True)
        dialog = QDialog(self)
        dialog.setWindowTitle('Контакты из чата' if kind == 'collect' else 'Приглашение контактов в группу')
        dialog.resize(550, 220)
        layout = QFormLayout(dialog)
        combo = QComboBox()
        combo.setEditable(kind == 'collect')
        for row in self.store.rows('SELECT * FROM chats WHERE account=? ORDER BY name', (account,)):
            roles = self.store.rows('SELECT admin FROM chat_roles WHERE account=? AND chat=?', (account, row['uid']))
            if kind == 'collect' or (row['kind'] == 'CHAT' and roles and roles[0]['admin']):
                combo.addItem(f'{row["name"]} / {row["uid"]}', row['uid'])
        if combo.completer():
            combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        selected = self.selected(self.chats)
        if kind == 'collect' and selected:
            index = combo.findData(selected[0])
            combo.setCurrentIndex(index)
        amount = QSpinBox()
        amount.setRange(1, 1000)
        amount.setValue(10)
        layout.addRow('Исходный чат' if kind == 'collect' else 'Целевая группа (только группы)', combo)
        layout.addRow('Новых добавлений', amount)
        label = QComboBox()
        label.setEditable(kind == 'collect')
        label.addItem('Все метки' if kind == 'invite' else 'Без метки', '')
        for value in sorted({row.get('label', '') for row in self.store.contacts(account)} - {''}):
            label.addItem(value, value)
        layout.addRow('Фильтр по метке' if kind == 'invite' else 'Локальная метка', label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Проверить участников')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        exact = combo.findText(combo.currentText())
        if exact >= 0:
            chat = combo.itemData(exact)
        else:
            try:
                chat = int(combo.currentText().strip())
            except ValueError:
                raise ValueError('Выберите чат или введите числовой ID MAX')
        contact_selection = self.selected(self.contacts) if kind == 'invite' else []
        if kind == 'invite':
            label_value = label.currentData()
        elif label.currentIndex() >= 0 and label.currentText() == label.itemText(label.currentIndex()):
            label_value = label.currentData() or ''
        else:
            label_value = label.currentText().strip()
        self.engine.submit(account, ('preview', amount.value(), contact_selection, label_value),
                           self.engine.preview(account, kind, chat, amount.value(), label_value))

    def lookup(self):
        account = self.account(True)
        mode, ok = QInputDialog.getItem(self, 'Найти участника', 'Поиск', ['Номер телефона', 'ID MAX'], editable=False)
        if not ok:
            return
        value, ok = QInputDialog.getText(self, 'Найти участника', mode)
        if ok:
            self.engine.submit(account, 'lookup', self.engine.lookup(account, 'phone' if mode == 'Номер телефона' else 'id', value.strip()))

    def import_list(self):
        account = self.account(True)
        path, _ = QFileDialog.getOpenFileName(self, 'Список участников', '', 'Списки (*.txt *.csv *.tsv)')
        if not path:
            return
        with open(path, encoding='utf-8-sig', newline='') as file:
            if Path(path).suffix.lower() in ('.csv', '.tsv'):
                reader = csv.reader(file, delimiter='\t' if Path(path).suffix.lower() == '.tsv' else ',')
                lines = [row[0] for row in reader if row]
            else:
                lines = file.read().splitlines()
        entries = []
        for line in lines:
            value = line.strip()
            if not value:
                continue
            if value.casefold() in ('id', 'phone', 'номер', 'id max'):
                continue
            if value.startswith('+'):
                entries.append(('phone', value))
            elif value.isdigit() and 0 < int(value) < 2**63:
                entries.append(('id', value))
            else:
                raise ValueError('В первой колонке должны быть ID MAX или международные номера с +')
        if not entries or len(entries) > 1000:
            raise ValueError('В файле должно быть от 1 до 1000 участников')
        if QMessageBox.question(self, 'Поиск участников', f'Найти {len(entries)} участников в MAX? У вас есть согласие на обработку этих данных?') != QMessageBox.StandardButton.Yes:
            return
        self.engine.submit(account, ('preview', len(entries), []), self.engine.resolve_list(account, entries))

    def show_preview(self, account, preview, amount, selection, label=''):
        if selection:
            preview['candidates'] = [(uid, name) for uid, name in preview['candidates'] if uid in selection]
        dialog = PreviewDialog(preview, amount, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        if not chosen:
            raise ValueError('Не выбраны участники')
        count = min(amount, len(chosen))
        identity = self.store.new_job(account, preview['kind'], preview['chat'], count, chosen,
                                      {'label': dialog.local_label.text().strip() or label,
                                       'refill': preview['kind'] == 'collect' and bool(preview['chat'])})
        self.nav.setCurrentRow(self.names.index('Задания'))
        self.refresh()
        self.footer.setText('Задание создано. Выберите его и нажмите «Запустить».')
        for row in range(self.jobs.rowCount()):
            if self.jobs.item(row, 0).data(Qt.ItemDataRole.UserRole) == identity:
                self.jobs.selectRow(row)

    def prepare_forward(self):
        account = self.account(True)
        chats = self.store.rows('SELECT * FROM chats WHERE account=? ORDER BY name', (account,))
        if not chats:
            raise ValueError('Сначала обновите список чатов')
        selected = self.selected(self.chats)
        source = selected[0] if len(selected) == 1 else None
        if source is None:
            names = [f'{row["name"]} / {row["uid"]}' for row in chats]
            value, ok = QInputDialog.getItem(self, 'Исходное сообщение', 'Исходный чат', names, editable=False)
            if not ok:
                return
            source = chats[names.index(value)]['uid']
        self.engine.submit(account, ('history', source), self.engine.forward_setup(account, source))

    def show_forward(self, account, source, setup):
        messages = setup['messages']
        if not messages:
            raise ValueError('В исходном чате нет доступных сообщений')
        chats = self.store.rows('SELECT * FROM chats WHERE account=? AND uid<>? ORDER BY name', (account, source))
        dialog = ForwardDialog(messages, chats, setup['folders'], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        message, targets = dialog.chosen()
        if not message:
            raise ValueError('Выберите исходное сообщение')
        if not 1 <= len(targets) <= 20:
            raise ValueError('Выберите от 1 до 20 целевых чатов')
        identity = self.store.new_job(account, 'forward', source, len(targets), targets,
                                      {'message_id': message})
        self.nav.setCurrentRow(self.names.index('Задания'))
        self.refresh()
        self.footer.setText('Задание пересылки создано. Проверьте получателей и нажмите «Запустить».')
        for row in range(self.jobs.rowCount()):
            if self.jobs.item(row, 0).data(Qt.ItemDataRole.UserRole) == identity:
                self.jobs.selectRow(row)

    def resume_job(self):
        selected = self.selected(self.jobs)
        if len(selected) != 1:
            raise ValueError('Выберите одно задание')
        job = self.store.job(selected[0])
        account = job['account']
        if not self.engine.available(account):
            raise ValueError('Аккаунт задания недоступен. Проверьте его статус и подключение.')
        if job['status'] in ('complete', 'running', 'stopped'):
            raise ValueError('Это задание нельзя запустить в текущем состоянии')
        if job['status'] != 'queued' and QMessageBox.question(self, 'Возобновить', 'Перепроверить результаты и возобновить задание? Неподтверждённые операции не повторяются.') != QMessageBox.StandardButton.Yes:
            return
        self.engine.submit(account, 'job', self.engine.run_job(job['id']))

    def delete_job(self):
        selected = self.selected(self.jobs)
        if not selected:
            raise ValueError('Выберите задания')
        if any(self.store.job(uid)['status'] == 'running' for uid in selected):
            raise ValueError('Сначала остановите выбранные задания')
        if QMessageBox.question(self, 'Удалить задания',
                                'Удалить выбранные задания из списка? История добавленных ID сохранится.') != QMessageBox.StandardButton.Yes:
            return
        for uid in selected:
            self.store.delete_job(uid)
        self.refresh()

    def prepare_multi(self):
        available = [row for row in self.store.rows('SELECT * FROM accounts ORDER BY name')
                     if self.engine.available(row['id']) and not self.engine.busy(row['id'])]
        if not available:
            raise ValueError('Нет свободных подключённых аккаунтов')
        dialog = CollectAccountsDialog(self.store, available, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.chosen()
        if not selected:
            raise ValueError('Выберите аккаунты')
        group = str(time.time_ns())
        for account, chat in selected:
            if not self.engine.available(account) or self.engine.busy(account):
                raise ValueError('Один из выбранных аккаунтов занят или отключён')
        for account, chat in selected:
            self.store.set_setting('collect_source_' + account, chat)
            identity = self.store.new_job(account, 'collect', chat, dialog.amount.value(), [],
                                          {'refill': True, 'group': group})
            self.engine.submit(account, 'job', self.engine.run_job(identity))
        self.nav.setCurrentRow(self.names.index('Задания'))
        self.refresh()

    def open_catalog(self):
        dialog = ChatCatalogDialog(self.store, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.prepare_join(dialog.folder.currentData())

    def prepare_join(self, folder='all'):
        available = [row for row in self.store.rows('SELECT * FROM accounts ORDER BY name')
                     if self.engine.available(row['id']) and not self.engine.busy(row['id'])]
        if not available:
            raise ValueError('Нет свободных подключённых аккаунтов')
        dialog = JoinLinksDialog(available, self, folder=folder)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not dialog.imported or not dialog.imported.entries or not dialog.chosen() or not dialog.consent.isChecked():
            raise ValueError('Выберите папку с доступными чатами и аккаунты, затем подтвердите вступления')
        for account in dialog.chosen():
            if not self.engine.available(account) or self.engine.busy(account):
                raise ValueError('Один из выбранных аккаунтов занят или отключён')
        options = {'folder': dialog.folder.currentData(), 'group': str(time.time_ns()),
                   'invalid': len(dialog.imported.invalid), 'duplicates': dialog.imported.duplicates}
        for account in dialog.chosen():
            identity = self.store.new_join_job(account, dialog.imported.entries, dialog.amount.value(), options)
            self.engine.submit(account, 'job', self.engine.run_job(identity))
        self.nav.setCurrentRow(self.names.index('Задания'))
        self.refresh()

    def refresh_join_history(self):
        account = self.account_selector.currentData()
        rows = self.store.join_history(account, self.joins.property('filter').text(), state=self.join_state.currentData())
        fill(self.joins, [((r['job'], r['uid']), [display_time(r['updated']), r['title'],
                           LINK_STATES.get(r['state'], r['state']), r['link']]) for r in rows])
        for index in range(self.joins.rowCount()):
            self.joins.setRowHidden(index, False)

    def join_details(self):
        selected = self.selected(self.joins)
        if len(selected) != 1:
            raise ValueError('Выберите одну запись истории')
        job, uid = selected[0]
        row = next(r for r in self.store.join_rows(job) if r['uid'] == uid)
        QMessageBox.information(self, 'История вступления', '\n'.join([
            row['title'], row['link'], f'ID группы: {row["chat"] or "не определён"}',
            f'Результат: {LINK_STATES.get(row["state"], row["state"])}', row['detail'],
            f'Попыток: {row["attempts"]}', f'Дата: {display_time(row["updated"])}',
            f'Файл: {json.loads(self.store.job(job)["options"]).get("file", "")}; строка: {row["source"]}',
        ]))

    def export_join_history(self):
        account = self.account()
        filename, _ = QFileDialog.getSaveFileName(self, 'История вступлений', 'join-history.csv', 'CSV (*.csv)')
        if not filename:
            return
        rows = self.store.join_history(account, self.joins.property('filter').text(), limit=-1,
                                       state=self.join_state.currentData())
        with open(filename, 'w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['Группа', 'Ссылка', 'ID MAX', 'Результат', 'Подробности', 'Попыток', 'Дата'])
            for row in rows:
                writer.writerow([csv_value(value) for value in (
                    row['title'], row['link'], row['chat'], LINK_STATES.get(row['state'], row['state']),
                    row['detail'], row['attempts'], display_time(row['updated']))])
        self.footer.setText(f'Сохранено записей: {len(rows)}')

    def control_job(self, action):
        selected = self.selected(self.jobs)
        if len(selected) != 1:
            raise ValueError('Выберите одно задание')
        identity = selected[0]
        job = self.store.job(identity)
        if job['status'] == 'running':
            self.engine.flags[identity] = action
            self.footer.setText('Остановка / пауза после текущего запроса')
        elif action == 'stop' and job['status'] != 'complete':
            self.store.status(identity, 'stopped', 'Остановлено пользователем')
            self.refresh()

    def resolve_job(self):
        selected = self.selected(self.jobs)
        if len(selected) != 1:
            raise ValueError('Выберите одно задание')
        job = self.store.job(selected[0])
        if job['kind'] != 'invite':
            if job['kind'] == 'broadcast':
                BroadcastReport(self.store, self.engine, job['id'], self).exec()
                return
            raise ValueError('Ручная проверка доступна только для приглашений')
        if job['status'] == 'running':
            raise ValueError('Сначала поставьте задание на паузу')
        pending = [uid for uid, state in self.store.items(job['id']).items() if state == 'pending']
        if not pending:
            raise ValueError('В этом задании нет неподтверждённых приглашений')
        text = (f'В задании {len(pending)} неподтверждённых приглашений.\n\n'
                'Продолжайте только если вы вручную проверили целевую группу и этих участников там нет. '
                'После подтверждения приложение разрешит повтор и отправит их по одному.')
        if QMessageBox.question(self, 'Ручная проверка', text) != QMessageBox.StandardButton.Yes:
            return
        for uid in pending:
            self.store.item(job['id'], uid, 'retryable')
        options = json.loads(job.get('options') or '{}')
        options['force_single'] = True
        self.store.set_job_options(job['id'], options)
        self.store.status(job['id'], 'paused',
                          f'Ручная проверка подтверждена: {len(pending)} участников отсутствуют; повтор по одному разрешён')
        self.store.log(job['account'], f'Ручная проверка задания: повтор по одному разрешён для {len(pending)} участников')
        self.refresh()

    def show_job_report(self):
        selected = self.selected(self.jobs)
        if len(selected) != 1:
            raise ValueError('Выберите одно задание')
        if self.store.job(selected[0])['kind'] == 'broadcast':
            BroadcastReport(self.store, self.engine, selected[0], self).exec()
        else:
            QMessageBox.information(self, 'Отчёт задания', self.engine.job_report(selected[0]))

    def show_batch_report(self):
        selected = self.selected(self.jobs)
        if len(selected) != 1:
            raise ValueError('Выберите задание нужного запуска')
        job = self.store.job(selected[0])
        if job['kind'] == 'join':
            rows = self.store.batch_report(selected[0])
            QMessageBox.information(self, 'Общий отчёт', '\n\n'.join(
                r['account_name'] + '\n' + self.engine.job_report(r['id']) for r in rows))
        else:
            BatchReportDialog(self.store.batch_report(selected[0]), self).exec()

    def export(self, widget, name):
        path, _ = QFileDialog.getSaveFileName(self, 'Экспорт CSV', name + '.csv', 'CSV (*.csv)')
        if not path:
            return
        with open(path, 'w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([widget.horizontalHeaderItem(col).text() for col in range(widget.columnCount())])
            for row in range(widget.rowCount()):
                if not widget.isRowHidden(row):
                    writer.writerow([widget.item(row, col).text() for col in range(widget.columnCount())])
        self.footer.setText('Отчёт сохранён')

    def export_job(self):
        selected = self.selected(self.jobs)
        if len(selected) != 1:
            raise ValueError('Выберите одно задание')
        path, _ = QFileDialog.getSaveFileName(self, 'Результаты задания', 'job.csv', 'CSV (*.csv)')
        if path:
            with open(path, 'w', encoding='utf-8-sig', newline='') as file:
                writer = csv.writer(file)
                if self.store.job(selected[0])['kind'] == 'join':
                    writer.writerow(['Группа', 'Ссылка', 'ID MAX', 'Результат', 'Подробности', 'Попыток', 'Дата'])
                    for row in self.store.join_rows(selected[0]):
                        writer.writerow([csv_value(value) for value in (
                            row['title'], row['link'], row['chat'], LINK_STATES.get(row['state'], row['state']),
                            row['detail'], row['attempts'], display_time(row['updated']))])
                    return
                writer.writerow(['ID MAX', 'Состояние', 'Подробности', 'Попыток', 'Обновлено'])
                for row in self.store.item_rows(selected[0]):
                    writer.writerow([row['uid'], row['state'], row['detail'], row['attempts'],
                                     display_time(row['updated'])])

    def save_settings(self):
        self.store.set_setting('request_delay', self.delay.value())
        self.store.set_setting('read_delay', self.read_delay.value())
        self.footer.setText('Настройки сохранены')

    def change_theme(self):
        theme = self.theme.currentData()
        self.store.set_setting('theme', theme)
        apply_theme(QApplication.instance(), theme)
        self.refresh()

    def clear_source_cache(self):
        if any(self.engine.busy(account) for account in self.engine.futures):
            raise ValueError('Дождитесь завершения текущих операций')
        self.store.execute('DELETE FROM source_pages')
        self.footer.setText('Кэш очищен. Следующий сбор загрузит участников из MAX.')

    def open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory)))

    def import_legacy(self):
        account = self.account()
        if account in self.engine.clients or self.engine.busy(account):
            raise ValueError('Сначала отключите аккаунт')
        if (self.engine.vault.folder(account) / 'session.protected').exists():
            raise ValueError('У этого аккаунта уже есть сессия. Создайте новую запись для импорта.')
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите прежнюю QR-сессию', str(Path(os.environ['LOCALAPPDATA']) / 'MAX-Test' / 'sessions'), 'SQLite (*.db)')
        if path:
            self.engine.vault.import_legacy(account, path)
            self.footer.setText('Сессия скопирована и защищена. Теперь подключите аккаунт.')

    def remove_account(self):
        account = self.account()
        if account in self.engine.clients or self.engine.busy(account):
            raise ValueError('Сначала отключите аккаунт')
        if QMessageBox.question(self, 'Удаление данных', 'Удалить локальную сессию, контакты, задания и историю аккаунта? Общий реестр занятых групп сохранится. Это не отзывает сессию на сервере MAX.') != QMessageBox.StandardButton.Yes:
            return
        folder = self.engine.vault.folder(account)
        for name in ('session.protected', 'session.protected.new', 'session.db', 'session.db-wal', 'session.db-shm'):
            (folder / name).unlink(missing_ok=True)
        with self.store.db() as db:
            db.execute('DELETE FROM join_targets WHERE job IN (SELECT id FROM jobs WHERE account=?)', (account,))
            db.execute('DELETE FROM items WHERE job IN (SELECT id FROM jobs WHERE account=?)', (account,))
            db.execute('DELETE FROM harvest_claims WHERE account=?', (account,))
            for name in ('contacts', 'chats', 'jobs', 'logs'):
                db.execute(f'DELETE FROM {name} WHERE account=?', (account,))
            db.execute('DELETE FROM accounts WHERE id=?', (account,))
        self.reload_accounts()

    def on_engine_event(self, kind, data):
        if self.closing:
            return
        if kind == 'qr':
            account, url = data
            self.close_login_dialog(account)
            dialog = QDialog(self)
            dialog.setWindowTitle('Подтверждение MAX')
            layout = QVBoxLayout(dialog)
            image = qrcode.make(url).convert('RGB')
            buffer = BytesIO()
            image.save(buffer, format='PNG')
            pixmap = QPixmap()
            if not pixmap.loadFromData(buffer.getvalue(), 'PNG'):
                raise ValueError('Не удалось отобразить QR-код')
            label = QLabel()
            label.setPixmap(pixmap.scaled(360, 360, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
            layout.addWidget(QLabel('Ожидание подтверждения на телефоне'))
            cancel = self.button('Отменить', dialog.reject)
            layout.addWidget(cancel)
            dialog.rejected.connect(lambda: self.cancel_login(account))
            self.qr_dialogs[account] = dialog
            dialog.show()
        elif kind in ('code', 'password'):
            account, future, *detail = data
            self.close_login_dialog(account)
            if future.done():
                return
            rows = self.store.rows('SELECT name FROM accounts WHERE id=?', (account,))
            dialog = LoginInputDialog(kind, detail[0] if detail else '', rows[0]['name'] if rows else 'MAX', self)
            def finish(accepted):
                value = dialog.value.text() if accepted else None
                dialog.value.clear()
                try:
                    future.set_result(value)
                except concurrent.futures.InvalidStateError:
                    pass
            dialog.accepted.connect(lambda: finish(True))
            dialog.rejected.connect(lambda: finish(False))
            self.qr_dialogs[account] = dialog
            dialog.show()
            dialog.value.setFocus()
        elif kind in ('result', 'error'):
            account = data[0]
            self.close_login_dialog(account)
            if kind == 'error':
                self.footer.setText(data[1])
                QMessageBox.warning(self, 'Ответ MAX', data[1])
            else:
                _, tag, result = data
                if isinstance(tag, tuple) and tag[0] == 'preview':
                    label = tag[3] if len(tag) > 3 else ''
                    self.guarded(lambda: self.show_preview(account, result, tag[1], tag[2], label))
                elif isinstance(tag, tuple) and tag[0] == 'history':
                    self.guarded(lambda: self.show_forward(account, tag[1], result))
                elif isinstance(tag, tuple) and tag[0] == 'broadcast_groups':
                    self.footer.setText('Список чатов обновлён')
                elif isinstance(tag, tuple) and tag[0].startswith('post_import_'):
                    self.footer.setText('Пост сохранён' if tag[0] == 'post_import_commit' else 'Данные для импорта загружены')
                elif tag == 'lookup':
                    preview = {'kind': 'collect', 'chat': 0, 'candidates': [result], 'excluded': 0}
                    self.guarded(lambda: self.show_preview(account, preview, 1, []))
                else:
                    self.footer.setText(str(result).splitlines()[0] if result else 'Готово')
                    self.footer.setToolTip(str(result))
                    if tag == 'connect':
                        self.reload_accounts(account)
        if not getattr(self, '_refresh_queued', False):
            self._refresh_queued = True
            QTimer.singleShot(200, self.flush_refresh)

    def flush_refresh(self):
        self._refresh_queued = False
        self.refresh()

    def closeEvent(self, event):
        if self.closing:
            event.accept()
            return
        event.ignore()
        if any(self.engine.busy(account) for account in self.engine.futures):
            if QMessageBox.question(self, 'Закрыть приложение', 'Прервать текущие операции и сохранить результаты?') != QMessageBox.StandardButton.Yes:
                return
        self.closing = True
        self.timer.stop()
        self.footer.setText('Отключение аккаунтов и защита сессий…')
        self.setEnabled(False)
        future = asyncio.run_coroutine_threadsafe(self.engine.shutdown(), self.engine.loop)
        timer = QTimer(self)
        def finish():
            if future.done():
                self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
                timer.stop()
                self.close()
        timer.timeout.connect(finish)
        timer.start(100)


def main():
    app = QApplication(sys.argv)
    configure_app(app)
    smoke = '--self-test' in sys.argv
    temporary = tempfile.TemporaryDirectory(prefix='max-workspace-check-') if smoke else None
    directory = Path(temporary.name) if smoke else Path(os.environ['LOCALAPPDATA']) / 'MAX-Workspace'
    directory.mkdir(exist_ok=True)
    lock = QLockFile(str(directory / 'workspace.lock'))
    if not lock.tryLock(100):
        QMessageBox.information(None, 'MAX Workspace', 'Приложение уже запущено.')
        return
    store = Store(directory)
    window = Window(store, demo=smoke)
    for account in store.rows('SELECT id FROM accounts'):
        window.engine.vault.seal(account['id'])
    window.show()
    if smoke:
        QTimer.singleShot(500, window.close)
    app.exec()
    window.engine.thread.join(timeout=10)
    lock.unlock()
    if smoke:
        print('STARTUP_OK: Qt window created and closed without network requests')
        temporary.cleanup()


if __name__ == '__main__':
    main()
