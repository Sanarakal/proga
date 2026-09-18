"""Account-scoped message picker; imports only after an explicit selection."""
import time

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit, QTextBrowser, QSplitter, QStyle, QWidget, QListWidget, QListWidgetItem, QAbstractItemView, QMessageBox

from workspace_post_import import message_history, import_message, message_content, check_message, attachment_kind, message_thumbnails
from workspace_posts_ui import button, data_table, set_rows, selected_id, load_content
from workspace_status import account_display


class ImportPostDialog(QDialog):
    def __init__(self, store, engine, parent=None):
        super().__init__(parent)
        self.store, self.engine = store, engine
        self.identity = None
        self.messages = {}
        self.future = None
        self.request_context = None
        self.before = None
        self.exhausted = False
        self.busy = False
        self.loading_older = False
        self.setWindowTitle('Импорт поста из MAX')
        self.resize(880, 720)
        self.setMinimumSize(640, 580)
        layout = QVBoxLayout(self)
        self.account = QComboBox()
        for row in store.rows('SELECT * FROM accounts ORDER BY rowid'):
            self.account.addItem(row['name'] + ' / ' + account_display(row, engine.connected(row['id']))[1], row['id'])
        layout.addWidget(self.account)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Поиск исходного чата')
        filters.addWidget(self.search, 1)
        self.refresh_button = button(self, 'Обновить чаты', self.refresh_chats, QStyle.StandardPixmap.SP_BrowserReload)
        filters.addWidget(self.refresh_button)
        layout.addLayout(filters)
        self.chats = QComboBox()
        layout.addWidget(self.chats)
        controls = QHBoxLayout()
        self.load_button = button(self, 'Загрузить сообщения', self.load_history)
        self.older_button = button(self, 'Загрузить ещё', lambda: self.load_history(older=True))
        controls.addWidget(self.load_button)
        controls.addWidget(self.older_button)
        self.message_id = QLineEdit()
        self.message_id.setPlaceholderText('ID сообщения (необязательно)')
        controls.addWidget(self.message_id, 1)
        layout.addLayout(controls)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.history = data_table(['Сообщения'])
        self.history.horizontalHeader().hide()
        self.history.setWordWrap(True)
        self.history.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.history.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.history.setMinimumWidth(230)
        self.splitter.addWidget(self.history)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_title = QLabel('Предпросмотр')
        detail_layout.addWidget(self.detail_title)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        detail_layout.addWidget(self.preview, 1)
        self.media = QListWidget()
        self.media.setViewMode(QListWidget.ViewMode.IconMode)
        self.media.setIconSize(QSize(96, 56))
        self.media.setGridSize(QSize(140, 92))
        self.media.setSpacing(4)
        self.media.setStyleSheet('QListWidget::item { padding: 4px; }')
        self.media.setMovement(QListWidget.Movement.Static)
        self.media.setFixedHeight(108)
        self.media.hide()
        detail_layout.addWidget(self.media)
        self.preview_button = button(self, 'Показать миниатюры', self.load_thumbnails)
        self.preview_button.hide()
        detail_layout.addWidget(self.preview_button)
        self.splitter.addWidget(detail)
        self.splitter.setSizes([300, 500])
        layout.addWidget(self.splitter, 1)
        self.title = QLineEdit()
        self.title.setMaxLength(100)
        self.title.setPlaceholderText('Название сохраняемого поста')
        layout.addWidget(self.title)
        self.status = QLabel('Выберите аккаунт и исходный чат')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bottom = QHBoxLayout()
        self.import_button = button(self, 'Сохранить пост', self.import_selected, QStyle.StandardPixmap.SP_DialogSaveButton)
        bottom.addWidget(self.import_button)
        bottom.addWidget(button(self, 'Закрыть', self.reject))
        layout.addLayout(bottom)
        self.account.currentIndexChanged.connect(self.account_changed)
        self.search.textChanged.connect(self.filter_chats)
        self.chats.currentIndexChanged.connect(self.clear_history)
        self.history.itemSelectionChanged.connect(self.selection_changed)
        self.engine.event.connect(self.engine_event)
        self.finished.connect(self.cleanup)
        self.account_changed()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        orientation = Qt.Orientation.Horizontal if self.width() >= 760 else Qt.Orientation.Vertical
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([180, 260] if self.width() < 760 else [300, 500])

    def account_changed(self, *args):
        self.chat_rows = self.store.rows('SELECT * FROM chats WHERE account=? ORDER BY name', (self.account.currentData(),))
        self.filter_chats()
        self.clear_history()

    def filter_chats(self, *args):
        previous = self.chats.currentData()
        self.chats.blockSignals(True)
        self.chats.clear()
        query = self.search.text().casefold()
        kinds = {'CHAT': 'Группа', 'CHANNEL': 'Канал', 'DIALOG': 'Личный диалог'}
        for row in self.chat_rows:
            label = row['name'] + ' / ' + kinds.get(row['kind'], row['kind']) + ' / ' + str(row['uid'])
            if query in label.casefold():
                self.chats.addItem(label, row['uid'])
        index = self.chats.findData(previous)
        if index >= 0:
            self.chats.setCurrentIndex(index)
        self.chats.blockSignals(False)
        if previous != self.chats.currentData():
            self.clear_history()

    def clear_history(self, *args):
        self.messages.clear()
        self.before = None
        self.exhausted = False
        self.history.setRowCount(0)
        self.preview.clear()
        self.message_id.clear()
        self.media.clear()
        self.media.hide()
        self.preview_button.hide()
        self.detail_title.setText('Предпросмотр')
        self.update_paging()

    def update_paging(self):
        self.older_button.setEnabled(not self.busy and self.before is not None and not self.exhausted)
        self.older_button.setText('История загружена' if self.exhausted else 'Загрузить ещё')

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.account, self.search, self.chats, self.refresh_button,
                       self.load_button, self.older_button, self.message_id, self.import_button, self.history, self.preview_button):
            widget.setEnabled(not busy)
        self.update_paging()

    def submit(self, kind, operation, require_chat=True):
        account, chat = self.account.currentData(), self.chats.currentData()
        if not account or not self.engine.available(account):
            operation.close()
            raise ValueError('Подключите выбранный аккаунт в разделе «Аккаунты».')
        if require_chat and chat is None:
            operation.close()
            raise ValueError('Выберите исходный чат.')
        self.request_context = (account, chat)
        self.future = self.engine.submit(account, (kind, id(self)), operation)
        self.set_busy(True)

    def refresh_chats(self):
        self.submit('post_import_chats', self.engine.refresh(self.account.currentData()), require_chat=False)
        self.status.setText('Загрузка чатов...')

    def load_history(self, older=False):
        if older and self.before is None:
            raise ValueError('Сначала загрузите последние сообщения.')
        if not older:
            self.clear_history()
        self.loading_older = older
        self.submit('post_import_history', message_history(self.engine, self.account.currentData(), self.chats.currentData(), self.before if older else None))
        self.status.setText('Загрузка сообщений...')

    def selection_changed(self):
        identity = selected_id(self.history)
        if identity not in self.messages:
            return
        message = self.messages[identity]
        self.message_id.setText(str(identity))
        self.detail_title.setText(time.strftime('%d.%m.%Y · %H:%M', time.localtime(message.time / 1000)))
        self.media.clear()
        self.media.hide()
        self.preview_button.setVisible(any(attachment_kind(a) in ('PHOTO', 'VIDEO') for a in message.attaches or []))
        try:
            content = message_content(message)
            load_content(self.preview, content)
            check_message(message)
            kinds = [attachment_kind(a) for a in message.attaches or []]
            self.status.setText(f'Фото: {kinds.count("PHOTO")} · видео: {kinds.count("VIDEO")}')
            if 'INLINE_KEYBOARD' in kinds:
                self.status.setText(self.status.text() + ' · Кнопки MAX не перенесутся')
        except ValueError as error:
            self.preview.setPlainText(message.text or '')
            self.status.setText(str(error))

    def load_thumbnails(self):
        identity = selected_id(self.history)
        if identity not in self.messages:
            raise ValueError('Выберите сообщение.')
        self.submit('post_import_thumbnails', message_thumbnails(self.messages[identity]))
        self.status.setText('Загрузка миниатюр...')

    def import_selected(self):
        try:
            identity = int(self.message_id.text().strip())
        except ValueError:
            raise ValueError('Выберите сообщение или укажите его числовой ID.') from None
        if identity <= 0:
            raise ValueError('Некорректный ID сообщения.')
        if identity in self.messages:
            check_message(self.messages[identity])
        title = self.title.text().strip() or 'Пост из MAX · ' + time.strftime('%d.%m %H:%M')
        self.submit('post_import_commit', import_message(self.engine, self.account.currentData(), self.chats.currentData(), identity, title))
        self.status.setText('Загрузка вложений и сохранение поста...')

    def engine_event(self, kind, data):
        if kind not in ('result', 'error') or not self.request_context or data[0] != self.request_context[0]:
            return
        if kind == 'error':
            self.set_busy(False)
            self.status.setText(data[1])
            self.request_context = None
            return
        tag, result = data[1], data[2]
        if tag not in [(name, id(self)) for name in ('post_import_history', 'post_import_chats', 'post_import_commit', 'post_import_thumbnails')]:
            return
        self.set_busy(False)
        self.request_context = None
        if tag[0] == 'post_import_chats':
            self.account_changed()
            self.status.setText('Список чатов обновлён')
        elif tag[0] == 'post_import_history':
            previous_ids = set(self.messages)
            for message in result:
                self.messages[message.id] = message
            new_ids = set(self.messages) - previous_ids
            cursor = min((message.time for message in result), default=None)
            # Stop on an empty or non-advancing server page, not a repeated request.
            self.exhausted = not result or (self.loading_older and (not new_ids or cursor > self.before))
            if cursor is not None and not self.exhausted:
                self.before = cursor - 1
            ordered = sorted(self.messages.values(), key=lambda message: (message.time, message.id), reverse=True)
            rows = []
            for message in ordered:
                kinds = [attachment_kind(a) for a in message.attaches or []]
                meta = time.strftime('%d.%m.%Y  %H:%M', time.localtime(message.time / 1000))
                if kinds:
                    meta += '  ·  ' + ', '.join(f'{kind}: {kinds.count(kind)}' for kind in dict.fromkeys(kinds))
                rows.append((message.id, [meta + '\n\n' + ((message.text or '').strip()[:240] or 'Сообщение с вложениями')]))
            self.history.blockSignals(True)
            set_rows(self.history, rows)
            for index in range(len(rows)):
                self.history.setRowHeight(index, 112)
            self.history.blockSignals(False)
            target = next((i for i, message in enumerate(ordered) if message.id in new_ids), None)
            if target is not None:
                self.history.selectRow(target)
                self.history.scrollToItem(self.history.item(target, 0), QAbstractItemView.ScrollHint.PositionAtTop)
                self.selection_changed()
            self.update_paging()
            self.status.setText(f'Загружено: {len(self.messages)} · новых: {len(new_ids)}' if new_ids else 'Более ранних сообщений MAX не вернул')
        elif tag[0] == 'post_import_thumbnails':
            identity, thumbnails = result
            if identity != selected_id(self.history):
                return
            self.media.clear()
            for index, (media_kind, data) in enumerate(thumbnails, 1):
                label = ('Видео' if media_kind == 'VIDEO' else 'Фото') + f' {index}'
                pixmap = QPixmap()
                if data:
                    pixmap.loadFromData(data)
                icon = QIcon(pixmap) if not pixmap.isNull() else self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
                item = QListWidgetItem(icon, label if data else label + ' · недоступно')
                item.setSizeHint(QSize(132, 88))
                self.media.addItem(item)
            self.media.setVisible(bool(thumbnails))
            self.preview_button.setVisible(not thumbnails or not all(data for _, data in thumbnails))
            self.status.setText('Миниатюры загружены' if thumbnails and all(data for _, data in thumbnails) else 'Некоторые миниатюры недоступны')
        else:
            self.identity = result.identity
            if result.keyboard_omitted:
                QMessageBox.information(self, 'Пост сохранён без кнопок',
                    'Текст, его гиперссылки и все фото/видео сохранены.\n'
                    'Кнопки MAX и ссылки внутри кнопок перенести нельзя: они пропущены.')
            self.accept()

    def cleanup(self):
        self.engine.event.disconnect(self.engine_event)
        if self.future and not self.future.done():
            self.future.cancel()
