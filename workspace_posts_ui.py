"""Post editor, broadcast wizard and delivery report for the existing Qt shell."""
import csv
import json
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QSize, QDateTime, QTime, QObject, Signal, QRunnable, QThreadPool, QBuffer, QIODevice, QUrl
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor, QPixmap, QIcon, QTextFormat, QTextBlockFormat
from PySide6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFormLayout, QLabel, QPushButton, QLineEdit, QTextEdit, QTextBrowser, QListWidget,
    QListWidgetItem, QAbstractItemView, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox, QInputDialog, QComboBox, QCheckBox,
    QSpinBox, QDateTimeEdit, QTimeEdit, QStackedWidget, QScrollArea, QStyle)

from workspace_posts import MAX_PHOTOS, MAX_TEXT, STATES, link_url, validate_content, validate_options
from workspace_links import csv_value
from workspace_status import account_display


EDITABLE_FORMATS = {'STRONG', 'EMPHASIZED', 'UNDERLINE', 'LINK', 'STRIKETHROUGH', 'MONOSPACED', 'CODE'}
CODE_PROPERTY = int(QTextFormat.Property.UserProperty) + 1


def guarded(parent, callback):
    try:
        return callback()
    except (ValueError, OSError) as error:
        QMessageBox.warning(parent, 'MAX Workspace', str(error))


def button(parent, text, callback, icon=None):
    widget = QPushButton(text)
    widget.ensurePolished()
    if icon is not None:
        widget.setIcon(parent.style().standardIcon(icon))
    widget.setMinimumWidth(widget.fontMetrics().horizontalAdvance(text) + 32 + (24 if icon else 0))
    widget.clicked.connect(lambda: guarded(parent, callback))
    return widget


def grid_actions(parent, actions, columns=3):
    layout = QGridLayout()
    for index, (label, callback, icon) in enumerate(actions):
        layout.addWidget(button(parent, label, callback, icon), index // columns, index % columns)
    return layout


def data_table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.setWordWrap(False)
    return widget


def set_rows(table, rows):
    previous = selected_id(table)
    table.setRowCount(len(rows))
    for r, (identity, values) in enumerate(rows):
        for c, value in enumerate(values):
            cell = QTableWidgetItem(str(value))
            cell.setData(Qt.ItemDataRole.UserRole, identity)
            cell.setToolTip(str(value))
            table.setItem(r, c, cell)
        if identity == previous:
            table.selectRow(r)


def selected_id(table):
    selection = table.selectionModel().selectedRows()
    return table.item(selection[0].row(), 0).data(Qt.ItemDataRole.UserRole) if selection else None


def require_selection(table, label):
    identity = selected_id(table)
    if identity is None:
        raise ValueError(label)
    return identity


def document_content(editor, photos):
    elements = []
    block = editor.document().begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                fmt = fragment.charFormat()
                kinds = []
                if fmt.fontWeight() >= QFont.Weight.Bold:
                    kinds.append('STRONG')
                if fmt.fontItalic():
                    kinds.append('EMPHASIZED')
                if fmt.fontStrikeOut():
                    kinds.append('STRIKETHROUGH')
                if fmt.property(CODE_PROPERTY):
                    kinds.append(str(fmt.property(CODE_PROPERTY)))
                if fmt.fontUnderline() and not fmt.isAnchor():
                    kinds.append('UNDERLINE')
                if fmt.isAnchor() and fmt.anchorHref():
                    kinds.append('LINK')
                for kind in kinds:
                    item = {'type': kind, 'from': fragment.position(), 'length': fragment.length()}
                    if kind == 'LINK':
                        item['url'] = fmt.anchorHref()
                    elements.append(item)
            it += 1
        block = block.next()
    return dict(text=editor.toPlainText(), elements=elements, photos=list(photos))


def load_content(editor, content):
    editor.setPlainText(content['text'])
    for item in content['elements']:
        cursor = QTextCursor(editor.document())
        cursor.setPosition(item['from'])
        cursor.setPosition(item['from'] + item['length'], QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        if item['type'] == 'STRONG':
            fmt.setFontWeight(QFont.Weight.Bold)
        elif item['type'] == 'EMPHASIZED':
            fmt.setFontItalic(True)
        elif item['type'] == 'UNDERLINE':
            fmt.setFontUnderline(True)
        elif item['type'] == 'LINK':
            fmt.setAnchor(True)
            fmt.setAnchorHref(item['url'])
            fmt.setFontUnderline(True)
        elif item['type'] == 'STRIKETHROUGH':
            fmt.setFontStrikeOut(True)
        elif item['type'] in ('MONOSPACED', 'CODE'):
            fmt.setFontFamilies(['Consolas'])
            fmt.setProperty(CODE_PROPERTY, item['type'])
        elif item['type'] == 'HEADING':
            fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setFontPointSize(14)
        elif item['type'] == 'QUOTE':
            block = QTextBlockFormat()
            block.setLeftMargin(18)
            cursor.mergeBlockFormat(block)
        cursor.mergeCharFormat(fmt)


def photo_items(store, listing, identities):
    listing.clear()
    for identity in identities:
        row = store.rows('SELECT thumbnail,kind FROM post_media WHERE id=?', (identity,))
        if not row:
            continue
        pixmap = QPixmap()
        pixmap.loadFromData(row[0]['thumbnail'])
        video = row[0]['kind'] == 'VIDEO'
        icon = listing.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay) if video else QIcon(pixmap)
        item = QListWidgetItem(icon, ('Видео ' if video else 'Фото ') + str(listing.count() + 1))
        item.setData(Qt.ItemDataRole.UserRole, identity)
        listing.addItem(item)


def photo_list():
    listing = QListWidget()
    listing.setObjectName('postPhotos')
    listing.setViewMode(QListWidget.ViewMode.IconMode)
    listing.setIconSize(QSize(116, 76))
    listing.setGridSize(QSize(130, 108))
    listing.setFixedHeight(124)
    listing.setFlow(QListWidget.Flow.LeftToRight)
    listing.setWrapping(False)
    listing.setMovement(QListWidget.Movement.Static)
    return listing


class MediaPreview(QDialog):
    def __init__(self, store, identity, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Вложение поста')
        self.resize(760, 560)
        row = store.rows('SELECT data,kind,name FROM post_media WHERE id=?', (identity,))[0]
        layout = QVBoxLayout(self)
        if row['kind'] == 'VIDEO':
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget
            view = QVideoWidget()
            layout.addWidget(view, 1)
            self.buffer = QBuffer(self)
            self.buffer.setData(row['data'])
            self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(view)
            self.player.setSourceDevice(self.buffer, QUrl(row['name']))
            controls = QHBoxLayout()
            controls.addWidget(button(self, 'Воспроизвести', self.player.play, QStyle.StandardPixmap.SP_MediaPlay))
            controls.addWidget(button(self, 'Пауза', self.player.pause, QStyle.StandardPixmap.SP_MediaPause))
            layout.addLayout(controls)
            status = QLabel()
            status.setWordWrap(True)
            layout.addWidget(status)
            self.player.errorOccurred.connect(lambda *_: status.setText('Не удалось воспроизвести видео в предпросмотре.'))
            self.finished.connect(self.player.stop)
        else:
            image = QPixmap()
            image.loadFromData(row['data'])
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setPixmap(image.scaled(720, 470, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(label, 1)
        layout.addWidget(button(self, 'Закрыть', self.accept))


class TextEditor(QTextEdit):
    filesDropped = Signal(object)

    def insertFromMimeData(self, source):
        if source.hasUrls() and all(url.isLocalFile() for url in source.urls()):
            self.filesDropped.emit([url.toLocalFile() for url in source.urls()])
        else:
            self.insertPlainText(source.text())


class PhotoSignals(QObject):
    finished = Signal(object, object)


class PhotoImport(QRunnable):
    def __init__(self, store, paths):
        super().__init__()
        self.store, self.paths = store, paths
        self.signals = PhotoSignals()

    def run(self):
        loaded, errors = [], []
        for path in self.paths:
            try:
                loaded.append(self.store.add_post_photo(path))
            except Exception as error:
                errors.append(str(error))
        self.signals.finished.emit(loaded, errors)


class PostEditor(QDialog):
    def __init__(self, store, identity=None, parent=None):
        super().__init__(parent)
        self.store, self.identity, self.loading = store, identity, False
        self.dirty = False
        self.original_content = None
        self.original_html = None
        self.setWindowTitle('Редактор поста')
        self.resize(820, 680)
        self.setMinimumSize(640, 550)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        self.title = QLineEdit()
        self.title.setMaxLength(100)
        self.title.setPlaceholderText('Название поста')
        layout.addWidget(self.title)
        toolbar = QGridLayout()
        for text, kind in (('Жирный', 'bold'), ('Курсив', 'italic'), ('Подчеркнуть', 'underline')):
            control = button(self, text, lambda k=kind: self.format_selection(k))
            toolbar.addWidget(control, 0, ('bold', 'italic', 'underline').index(kind))
        toolbar.addWidget(button(self, 'Ссылка', self.edit_link), 1, 0)
        toolbar.addWidget(button(self, 'Убрать ссылку', self.remove_link), 1, 1)
        layout.addLayout(toolbar)
        self.tabs = QTabWidget()
        self.editor = TextEditor()
        self.editor.filesDropped.connect(lambda paths: guarded(self, lambda: self.import_photos(paths)))
        self.editor.setPlaceholderText('Текст поста')
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.tabs.addTab(self.editor, 'Текст')
        self.tabs.addTab(self.preview, 'Предпросмотр')
        layout.addWidget(self.tabs, 1)
        self.photos = photo_list()
        self.photos.itemDoubleClicked.connect(lambda item: MediaPreview(store, item.data(Qt.ItemDataRole.UserRole), self).exec())
        layout.addWidget(self.photos)
        layout.addLayout(grid_actions(self, [
            ('Добавить фото', self.choose_photos, QStyle.StandardPixmap.SP_FileDialogNewFolder),
            ('Удалить вложение', self.remove_photo, QStyle.StandardPixmap.SP_TrashIcon),
            ('Раньше', lambda: self.move_photo(-1), QStyle.StandardPixmap.SP_ArrowBack),
            ('Позже', lambda: self.move_photo(1), QStyle.StandardPixmap.SP_ArrowForward)], 4))
        bottom = QHBoxLayout()
        self.status = QLabel('Черновик')
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(button(self, 'Сохранить', self.save_ready, QStyle.StandardPixmap.SP_DialogSaveButton))
        bottom.addWidget(button(self, 'Закрыть', self.reject))
        layout.addLayout(bottom)
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.setInterval(600)
        self.autosave.timeout.connect(self.save_draft)
        self.finished.connect(self.autosave.stop)
        if identity:
            post = store.post(identity)
            self.title.setText(post['title'])
            load_content(self.editor, post['content'])
            photo_items(store, self.photos, post['content']['photos'])
            self.original_content = post['content']
            self.original_html = self.editor.toHtml()
            complex_format = any(e['type'] not in EDITABLE_FORMATS or set(e) - {'type', 'from', 'length', 'url', 'attributes'}
                                 or set(e.get('attributes', {})) - {'url'} for e in post['content']['elements'])
            if complex_format:
                self.editor.setReadOnly(True)
                for control in (toolbar.itemAt(i).widget() for i in range(toolbar.count())):
                    if control:
                        control.setEnabled(False)
                self.status.setText('Оригинальная разметка MAX · текст только для чтения')
        self.editor.textChanged.connect(self.changed)
        self.title.textChanged.connect(self.changed)
        self.tabs.currentChanged.connect(self.refresh_preview)
        self.refresh_preview()

    def content(self):
        media = [self.photos.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.photos.count())]
        if self.original_content is not None and (self.editor.isReadOnly() or self.editor.toHtml() == self.original_html):
            return {**self.original_content, 'photos': media}
        return document_content(self.editor, media)

    def changed(self):
        self.dirty = True
        self.autosave.start()
        self.status.setText('Изменения...')
        self.refresh_preview()

    def refresh_preview(self, *args):
        load_content(self.preview, self.content())

    def save_draft(self):
        try:
            self.identity = self.store.save_post(self.identity, self.title.text(), self.content())
            self.dirty = False
            count = len(self.editor.toPlainText().encode('utf-16-le')) // 2
            self.status.setText(f'Черновик сохранён · {count}/{MAX_TEXT} · вложений {self.photos.count()}/{MAX_PHOTOS}')
            return True
        except (ValueError, OSError) as error:
            self.status.setText(str(error))
            return False

    def save_ready(self):
        if self.loading:
            raise ValueError('Дождитесь загрузки фотографий.')
        self.autosave.stop()
        self.identity = self.store.save_post(self.identity, self.title.text(), self.content(), ready=True)
        self.accept()

    def reject(self):
        if self.loading:
            self.status.setText('Загрузка фотографий...')
            return
        if self.dirty:
            self.autosave.stop()
            if not self.save_draft():
                return
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()

    def format_selection(self, kind):
        cursor = self.editor.textCursor()
        current = cursor.charFormat()
        fmt = QTextCharFormat()
        if kind == 'bold':
            fmt.setFontWeight(QFont.Weight.Normal if current.fontWeight() >= QFont.Weight.Bold else QFont.Weight.Bold)
        elif kind == 'italic':
            fmt.setFontItalic(not current.fontItalic())
        else:
            fmt.setFontUnderline(not current.fontUnderline())
        self.editor.mergeCurrentCharFormat(fmt)
        self.editor.setFocus()

    def edit_link(self):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            raise ValueError('Выделите текст ссылки.')
        url, ok = QInputDialog.getText(self, 'Гиперссылка', 'Адрес', text=cursor.charFormat().anchorHref() or 'https://')
        if ok:
            fmt = QTextCharFormat()
            fmt.setAnchor(True)
            fmt.setAnchorHref(link_url(url))
            fmt.setFontUnderline(True)
            cursor.mergeCharFormat(fmt)

    def remove_link(self):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            raise ValueError('Выделите текст ссылки.')
        fmt = QTextCharFormat()
        fmt.setAnchor(False)
        fmt.setAnchorHref('')
        fmt.setFontUnderline(False)
        cursor.mergeCharFormat(fmt)

    def choose_photos(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Фотографии', '', 'Изображения (*.jpg *.jpeg *.png *.webp *.bmp)')
        if paths:
            self.import_photos(paths)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        guarded(self, lambda: self.import_photos([url.toLocalFile() for url in event.mimeData().urls()]))

    def import_photos(self, paths):
        if self.loading:
            raise ValueError('Дождитесь предыдущей загрузки.')
        if self.photos.count() + len(paths) > MAX_PHOTOS:
            raise ValueError(f'Не более {MAX_PHOTOS} фото в посте.')
        self.loading = True
        self.status.setText('Загрузка фотографий...')
        self.importer = PhotoImport(self.store, paths)
        self.importer.signals.finished.connect(self.import_finished)
        QThreadPool.globalInstance().start(self.importer)

    def import_finished(self, loaded, errors):
        self.loading = False
        identities = list(dict.fromkeys(self.content()['photos'] + loaded))
        photo_items(self.store, self.photos, identities)
        self.dirty = True
        self.save_draft()
        if errors:
            QMessageBox.warning(self, 'Фотографии', '\n'.join(errors))

    def remove_photo(self):
        row = self.photos.currentRow()
        if row >= 0:
            self.photos.takeItem(row)
            self.changed()

    def move_photo(self, step):
        row = self.photos.currentRow()
        if 0 <= row + step < self.photos.count():
            item = self.photos.takeItem(row)
            self.photos.insertItem(row + step, item)
            self.photos.setCurrentRow(row + step)
            self.changed()


def spin(low, high, value, suffix=''):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    widget.setSuffix(suffix)
    return widget


class BroadcastWizard(QDialog):
    def __init__(self, store, engine, post, parent=None):
        super().__init__(parent)
        self.store, self.engine, self.post = store, engine, post
        self.identity = None
        self.choices = set()
        self.setWindowTitle('Новая рассылка')
        self.resize(800, 690)
        self.setMinimumSize(640, 560)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        layout.addWidget(self.heading)
        self.steps = QStackedWidget()
        layout.addWidget(self.steps, 1)
        account_page = QWidget()
        form = QFormLayout(account_page)
        self.account = QComboBox()
        for row in store.rows('SELECT * FROM accounts ORDER BY rowid'):
            state = account_display(row, engine.connected(row['id']))[1]
            self.account.addItem(row['name'] + ' / ' + state, row['id'])
        form.addRow('Аккаунт отправителя', self.account)
        preview = QTextBrowser()
        preview.setOpenLinks(False)
        load_content(preview, store.post(post)['content'])
        form.addRow(preview)
        photos = photo_list()
        photo_items(store, photos, store.post(post)['content']['photos'])
        photos.itemDoubleClicked.connect(lambda item: MediaPreview(store, item.data(Qt.ItemDataRole.UserRole), self).exec())
        form.addRow(photos)
        self.steps.addWidget(account_page)

        targets_page = QWidget()
        target_layout = QVBoxLayout(targets_page)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Поиск чата')
        search_row.addWidget(self.search, 1)
        self.folder = QComboBox()
        self.folder.addItem('Все чаты', None)
        search_row.addWidget(self.folder)
        target_layout.addLayout(search_row)
        sets_row = QHBoxLayout()
        self.sets = QComboBox()
        sets_row.addWidget(self.sets, 1)
        sets_row.addWidget(button(self, 'Загрузить набор', self.load_set))
        sets_row.addWidget(button(self, 'Сохранить набор', self.save_set))
        target_layout.addLayout(sets_row)
        target_layout.addLayout(grid_actions(self, [
            ('Выбрать найденные', lambda: self.select_visible(True), None),
            ('Снять выбор', lambda: self.select_visible(False), None),
            ('Обновить чаты', self.refresh_groups, QStyle.StandardPixmap.SP_BrowserReload)]))
        self.targets = data_table(['Выбор', 'Чат', 'Тип', 'ID MAX'])
        self.targets.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.targets.setColumnWidth(0, 76)
        target_layout.addWidget(self.targets, 1)
        self.target_status = QLabel()
        self.target_status.setWordWrap(True)
        target_layout.addWidget(self.target_status)
        self.steps.addWidget(targets_page)

        settings_page = QWidget()
        settings_form = QFormLayout(settings_page)
        self.title = QLineEdit(store.post(post)['title'])
        self.title.setMaxLength(100)
        settings_form.addRow('Название рассылки', self.title)
        self.interval = spin(1, 86400, 30, ' сек.')
        settings_form.addRow('Интервал между чатами', self.interval)
        self.batch = spin(0, 1000, 0)
        self.batch.setSpecialValueText('Без дополнительной паузы')
        settings_form.addRow('Пауза после N чатов', self.batch)
        self.batch_pause = spin(1, 86400, 300, ' сек.')
        settings_form.addRow('Длительность паузы', self.batch_pause)
        self.delayed = QCheckBox('Запуск по расписанию')
        settings_form.addRow(self.delayed)
        self.start = QDateTimeEdit(QDateTime.currentDateTime().addSecs(600))
        self.start.setDisplayFormat('dd.MM.yyyy HH:mm')
        self.start.setCalendarPopup(True)
        self.start.setEnabled(False)
        self.delayed.toggled.connect(self.start.setEnabled)
        settings_form.addRow('Дата и время', self.start)
        self.rounds = spin(1, 100, 1)
        settings_form.addRow('Количество проходов', self.rounds)
        self.repeat = spin(1, 525600, 1440, ' мин.')
        settings_form.addRow('Пауза между проходами', self.repeat)
        self.has_end = QCheckBox('Завершить не позднее')
        settings_form.addRow(self.has_end)
        self.end = QDateTimeEdit(QDateTime.currentDateTime().addDays(7))
        self.end.setDisplayFormat('dd.MM.yyyy HH:mm')
        self.end.setCalendarPopup(True)
        self.end.setEnabled(False)
        self.has_end.toggled.connect(self.end.setEnabled)
        settings_form.addRow('Дата окончания', self.end)
        self.hours = QCheckBox('Только в указанные часы')
        settings_form.addRow(self.hours)
        self.hour_start, self.hour_end = QTimeEdit(QTime(9, 0)), QTimeEdit(QTime(20, 0))
        for control in (self.hour_start, self.hour_end):
            control.setDisplayFormat('HH:mm')
            control.setEnabled(False)
            self.hours.toggled.connect(control.setEnabled)
        hours_row = QHBoxLayout()
        hours_row.addWidget(self.hour_start)
        hours_row.addWidget(QLabel('до'))
        hours_row.addWidget(self.hour_end)
        settings_form.addRow('Местное время ноутбука', hours_row)
        self.daily = spin(0, 10000, 0)
        self.daily.setSpecialValueText('Без дополнительного предела')
        settings_form.addRow('Попыток в сутки на аккаунт', self.daily)
        self.skip = QCheckBox('Пропускать чаты с явным запретом отправки')
        self.skip.setChecked(True)
        settings_form.addRow(self.skip)
        self.notify = QCheckBox('Уведомлять получателей')
        self.notify.setChecked(True)
        settings_form.addRow(self.notify)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(settings_page)
        self.steps.addWidget(scroll)

        review_page = QWidget()
        review_layout = QVBoxLayout(review_page)
        self.review = QTextBrowser()
        self.review.setOpenLinks(False)
        review_layout.addWidget(self.review, 1)
        self.test_target = QComboBox()
        review_layout.addWidget(self.test_target)
        review_layout.addWidget(button(self, 'Тестовая отправка', self.test_send, QStyle.StandardPixmap.SP_MediaPlay))
        self.test_status = QLabel()
        self.test_status.setWordWrap(True)
        review_layout.addWidget(self.test_status)
        self.consent = QCheckBox('Подтверждаю пост, чаты и право на отправку')
        review_layout.addWidget(self.consent)
        self.steps.addWidget(review_page)
        bottom = QHBoxLayout()
        self.back = button(self, 'Назад', self.previous)
        self.next = button(self, 'Далее', self.advance)
        bottom.addWidget(self.back)
        bottom.addStretch()
        bottom.addWidget(button(self, 'Отмена', self.reject))
        bottom.addWidget(self.next)
        layout.addLayout(bottom)
        self.search.textChanged.connect(self.filter_targets)
        self.folder.currentIndexChanged.connect(self.filter_targets)
        self.targets.itemChanged.connect(self.target_changed)
        self.account.currentIndexChanged.connect(self.account_changed)
        self.engine.event.connect(self.engine_event)
        self.finished.connect(lambda: self.engine.event.disconnect(self.engine_event))
        self.account_changed()
        self.show_step()

    def account_changed(self, *args):
        self.choices.clear()
        self.folder.clear()
        self.folder.addItem('Все чаты', None)
        self.reload_targets()
        self.reload_sets()

    def reload_targets(self):
        self.targets.blockSignals(True)
        rows = self.store.rows("SELECT * FROM chats WHERE account=? AND kind IN ('CHAT','CHANNEL','DIALOG') ORDER BY name", (self.account.currentData(),))
        self.choices.intersection_update(row['uid'] for row in rows)
        kinds = {'CHAT': 'Группа', 'CHANNEL': 'Канал', 'DIALOG': 'Личный диалог'}
        set_rows(self.targets, [(row['uid'], ['', row['name'], kinds[row['kind']], row['uid']]) for row in rows])
        for index, row in enumerate(rows):
            item = self.targets.item(index, 0)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            item.setCheckState(Qt.CheckState.Checked if row['uid'] in self.choices else Qt.CheckState.Unchecked)
        self.targets.blockSignals(False)
        self.filter_targets()

    def filter_targets(self, *args):
        query = self.search.text().casefold()
        folder = self.folder.currentData()
        for row in range(self.targets.rowCount()):
            chat = self.targets.item(row, 0).data(Qt.ItemDataRole.UserRole)
            text = self.targets.item(row, 1).text() + ' ' + str(chat)
            self.targets.setRowHidden(row, query not in text.casefold() or (folder is not None and chat not in folder))
        self.target_status.setText(f'Выбрано чатов: {len(self.choices)} / {self.targets.rowCount()}')

    def target_changed(self, item):
        if item.column() == 0:
            chat = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                self.choices.add(chat)
            else:
                self.choices.discard(chat)
            self.filter_targets()

    def select_visible(self, checked):
        for row in range(self.targets.rowCount()):
            if not checked or not self.targets.isRowHidden(row):
                self.targets.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def reload_sets(self):
        self.sets.clear()
        self.sets.addItem('Наборы чатов', None)
        for row in self.store.rows('SELECT name,chats FROM broadcast_sets WHERE account=? ORDER BY name', (self.account.currentData(),)):
            self.sets.addItem(row['name'], json.loads(row['chats']))

    def save_set(self):
        if not self.choices:
            raise ValueError('Выберите чаты.')
        name, ok = QInputDialog.getText(self, 'Набор чатов', 'Название')
        if ok and name.strip():
            exists = self.store.rows('SELECT 1 FROM broadcast_sets WHERE account=? AND name=?', (self.account.currentData(), name.strip()))
            if exists and QMessageBox.question(self, 'Набор чатов', 'Заменить сохранённый набор?') != QMessageBox.StandardButton.Yes:
                return
            self.store.execute('INSERT INTO broadcast_sets VALUES(?,?,?) ON CONFLICT(account,name) DO UPDATE SET chats=excluded.chats',
                               (self.account.currentData(), name.strip(), json.dumps(sorted(self.choices))))
            self.reload_sets()

    def load_set(self):
        if self.sets.currentData() is not None:
            self.choices = set(self.sets.currentData())
            self.reload_targets()

    def refresh_groups(self):
        account = self.account.currentData()
        if not self.engine.available(account):
            raise ValueError('Подключите выбранный аккаунт в разделе «Аккаунты».')
        self.engine.submit(account, ('broadcast_groups', id(self)), self.fetch_groups(account))
        self.target_status.setText('Обновление чатов...')

    async def fetch_groups(self, account):
        await self.engine.refresh(account)
        response = await self.engine.request(account, self.engine.clients[account].get_folders)
        return [(folder.title or 'Без названия', list(folder.include or [])) for folder in (response.folders or [])]

    def engine_event(self, kind, data):
        if kind == 'result' and data[1] == ('broadcast_groups', id(self)) and data[0] == self.account.currentData():
            self.folder.clear()
            self.folder.addItem('Все чаты', None)
            for title, chats in data[2]:
                self.folder.addItem(title, chats)
            self.reload_targets()
        elif kind == 'error' and data[0] == self.account.currentData():
            self.reload_targets()
            self.target_status.setText(data[1])
            self.test_status.setText(data[1])
        elif kind == 'result' and data[1] == ('broadcast_test', id(self)):
            self.test_status.setText('Тест завершён. Результат сохранён в разделе «Рассылки».')

    def options(self):
        if self.delayed.isChecked() and self.start.dateTime().toSecsSinceEpoch() <= time.time():
            raise ValueError('Выберите время запуска в будущем.')
        if self.hours.isChecked() and self.hour_start.time() == self.hour_end.time():
            raise ValueError('Начало и конец рабочих часов должны отличаться.')
        return validate_options(dict(interval=self.interval.value(), batch=self.batch.value(), batch_pause=self.batch_pause.value(),
            start_at=self.start.dateTime().toSecsSinceEpoch() if self.delayed.isChecked() else 0,
            rounds=self.rounds.value(), repeat_seconds=self.repeat.value() * 60,
            end_at=self.end.dateTime().toSecsSinceEpoch() if self.has_end.isChecked() else 0,
            window_start=self.hour_start.time().hour() * 60 + self.hour_start.time().minute() if self.hours.isChecked() else 0,
            window_end=self.hour_end.time().hour() * 60 + self.hour_end.time().minute() if self.hours.isChecked() else 0,
            daily_limit=self.daily.value(), skip_denied=self.skip.isChecked(), notify=self.notify.isChecked()))

    def selected_targets(self):
        return [self.targets.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.targets.rowCount())
                if self.targets.item(row, 0).data(Qt.ItemDataRole.UserRole) in self.choices]

    def show_step(self):
        index = self.steps.currentIndex()
        self.heading.setText(('1. Аккаунт и пост', '2. Чаты', '3. Настройки рассылки', '4. Проверка')[index])
        self.back.setEnabled(index > 0)
        self.next.setText('Создать рассылку' if index == 3 else 'Далее')
        self.next.setMinimumWidth(self.next.fontMetrics().horizontalAdvance(self.next.text()) + 32)

    def previous(self):
        self.steps.setCurrentIndex(max(0, self.steps.currentIndex() - 1))
        self.consent.setChecked(False)
        self.show_step()

    def advance(self):
        index = self.steps.currentIndex()
        if not self.account.currentData():
            raise ValueError('Сначала добавьте аккаунт.')
        if index >= 1 and not self.choices:
            raise ValueError('Выберите хотя бы один чат.')
        if index == 2:
            self.update_review()
        if index == 3:
            if not self.consent.isChecked():
                raise ValueError('Подтвердите пост и получателей.')
            self.identity = self.store.new_broadcast(self.post, self.account.currentData(), self.selected_targets(), self.title.text(), self.options())
            self.accept()
        else:
            self.steps.setCurrentIndex(index + 1)
            self.show_step()
            if index == 0 and self.engine.available(self.account.currentData()) and not self.engine.busy(self.account.currentData()):
                self.refresh_groups()

    def update_review(self):
        options = self.options()
        count = len(self.choices)
        if count > 1000 or count * options['rounds'] > 10000:
            raise ValueError('Не более 1000 чатов и 10000 отправок в задании.')
        content = self.store.post(self.post)['content']
        validate_content(content)
        delay = max(options['interval'], float(self.store.setting('request_delay', '2')))
        seconds = max(0, count - 1) * delay
        if options['batch']:
            seconds += ((count - 1) // options['batch']) * max(0, options['batch_pause'] - delay)
        starts = datetime.fromtimestamp(options['start_at']).strftime('%d.%m.%Y %H:%M') if options['start_at'] else 'После нажатия «Запустить»'
        lines = [self.title.text(), self.account.currentText(), f'Чатов: {count}; проходов: {options["rounds"]}; отправок: {count * options["rounds"]}',
                 f'Интервал: {options["interval"]} сек.; один проход: от {seconds / 60:.1f} мин. + время MAX',
                 'Начало: ' + starts,
                 f'Дополнительная пауза: {options["batch_pause"]} сек. после {options["batch"]} чатов' if options['batch'] else 'Дополнительная пауза: нет',
                 f'Пауза между проходами: {options["repeat_seconds"] // 60} мин.',
                 'Окончание: ' + (datetime.fromtimestamp(options['end_at']).strftime('%d.%m.%Y %H:%M') if options['end_at'] else 'После всех проходов'),
                 f'Рабочие часы: {self.hour_start.time().toString("HH:mm")}–{self.hour_end.time().toString("HH:mm")}' if self.hours.isChecked() else 'Рабочие часы: круглосуточно',
                 'Дневной предел попыток на аккаунт: ' + (str(options['daily_limit']) if options['daily_limit'] else 'не задан'),
                 'При запрете в чате: ' + ('пропустить' if options['skip_denied'] else 'остановиться'),
                 'Уведомления: ' + ('да' if options['notify'] else 'нет'),
                 f'Вложений: {len(content["photos"])}', '', content['text'], '', 'Получатели:']
        self.test_target.clear()
        for row in range(self.targets.rowCount()):
            chat = self.targets.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if chat in self.choices:
                name = self.targets.item(row, 1).text()
                lines.append(name + ' / ' + str(chat))
                self.test_target.addItem(name, chat)
        self.review.setPlainText('\n'.join(lines))

    def test_send(self):
        account, target = self.account.currentData(), self.test_target.currentData()
        if not self.consent.isChecked():
            raise ValueError('Подтвердите право на отправку.')
        if not self.engine.available(account) or self.engine.busy(account):
            raise ValueError('Аккаунт должен быть подключён и свободен.')
        if target is None:
            raise ValueError('Выберите тестовый чат.')
        if QMessageBox.question(self, 'Тестовая отправка', f'Отправить пост в «{self.test_target.currentText()}»? Этот чат останется в основной рассылке.') != QMessageBox.StandardButton.Yes:
            return
        identity = self.store.new_broadcast(self.post, account, [target], 'Тест: ' + self.title.text(), dict(notify=self.notify.isChecked()))
        self.engine.submit(account, ('broadcast_test', id(self)), self.engine.run_job(identity))
        self.test_status.setText('Тестовая отправка выполняется...')


class BroadcastReport(QDialog):
    def __init__(self, store, engine, identity, parent=None):
        super().__init__(parent)
        self.store, self.engine, self.identity = store, engine, identity
        self.setWindowTitle('Отчёт рассылки')
        self.resize(940, 650)
        self.setMinimumSize(640, 520)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = data_table(['Чат', 'Проход', 'Результат', 'Подробности'])
        layout.addWidget(self.table, 1)
        layout.addLayout(grid_actions(self, [
            ('Подтвердить вручную', lambda: self.resolve('confirmed'), None),
            ('Пропустить без повтора', lambda: self.resolve('skipped'), None),
            ('Экспорт CSV', self.export, QStyle.StandardPixmap.SP_DialogSaveButton)]))
        layout.addWidget(button(self, 'Закрыть', self.accept))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.finished.connect(self.timer.stop)
        self.refresh()

    def refresh(self):
        from workspace_broadcast import broadcast_report
        self.summary.setText(broadcast_report(self.store, self.identity))
        rows = self.store.broadcast_rows(self.identity)
        if getattr(self, '_snapshot', None) != rows:
            self._snapshot = rows
            set_rows(self.table, [(r['seq'], [r['name'], r['round'], STATES[r['state']], r['detail']]) for r in rows])

    def resolve(self, state):
        seq = require_selection(self.table, 'Выберите отправку для проверки.')
        row = next(r for r in self.store.broadcast_rows(self.identity) if r['seq'] == seq)
        if row['state'] != 'pending':
            raise ValueError('Эта отправка не требует ручной проверки.')
        text = ('Вы проверили сообщение в MAX и подтверждаете, что оно отправлено?' if state == 'confirmed'
                else 'Пропустить эту отправку без повторного запроса в MAX?')
        if QMessageBox.question(self, 'Ручная проверка', text) == QMessageBox.StandardButton.Yes:
            self.store.resolve_delivery(self.identity, seq, state)
            self.refresh()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Отчёт', 'broadcast.csv', 'CSV (*.csv)')
        if path:
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['Чат', 'ID чата', 'Проход', 'Результат', 'ID сообщения', 'Время', 'Подробности'])
                for row in self.store.broadcast_rows(self.identity):
                    stamp = datetime.fromtimestamp(row['updated']).isoformat(timespec='seconds') if row['updated'] else ''
                    writer.writerow([csv_value(str(value)) for value in (row['name'], row['chat'], row['round'], STATES[row['state']], row['message_id'] or '', stamp, row['detail'])])


class PostsPage(QWidget):
    def __init__(self, store, engine, parent=None):
        super().__init__(parent)
        self.store, self.engine = store, engine
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        posts_page = QWidget()
        posts_layout = QVBoxLayout(posts_page)
        posts_layout.addLayout(grid_actions(self, [
            ('Создать пост', self.create_post, QStyle.StandardPixmap.SP_FileDialogNewFolder),
            ('Импорт из MAX', self.import_post, QStyle.StandardPixmap.SP_ArrowDown),
            ('Редактировать', self.edit_post, None),
            ('Создать рассылку', self.create_broadcast, QStyle.StandardPixmap.SP_ArrowForward),
            ('Дублировать', self.duplicate_post, None),
            ('Удалить пост', self.delete_post, QStyle.StandardPixmap.SP_TrashIcon)]))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Поиск поста')
        self.search.textChanged.connect(self.refresh_posts)
        posts_layout.addWidget(self.search)
        self.posts = data_table(['Пост', 'Текст', 'Состояние', 'Изменён'])
        self.posts.setIconSize(QSize(64, 44))
        self.posts.verticalHeader().setDefaultSectionSize(56)
        self.posts.cellDoubleClicked.connect(lambda *_: guarded(self, self.edit_post))
        posts_layout.addWidget(self.posts, 1)
        self.empty = QLabel('Постов пока нет')
        posts_layout.addWidget(self.empty)
        self.tabs.addTab(posts_page, 'Мои посты')
        jobs_page = QWidget()
        jobs_layout = QVBoxLayout(jobs_page)
        jobs_layout.addLayout(grid_actions(self, [
            ('Запустить', self.start_job, QStyle.StandardPixmap.SP_MediaPlay),
            ('Пауза', lambda: self.control('pause'), QStyle.StandardPixmap.SP_MediaPause),
            ('Остановить', lambda: self.control('stop'), QStyle.StandardPixmap.SP_MediaStop),
            ('Отчёт', self.report, QStyle.StandardPixmap.SP_FileDialogInfoView),
            ('Просмотр поста', self.view_post, None),
            ('Удалить задание', self.delete_job, QStyle.StandardPixmap.SP_TrashIcon)]))
        self.jobs = data_table(['Рассылка / аккаунт', 'Состояние', 'Отправлено', 'Следующая отправка'])
        jobs_layout.addWidget(self.jobs, 1)
        self.job_detail = QLabel()
        self.job_detail.setWordWrap(True)
        jobs_layout.addWidget(self.job_detail)
        self.jobs.itemSelectionChanged.connect(self.update_detail)
        self.tabs.addTab(jobs_page, 'Рассылки')
        self.refresh()

    def refresh_posts(self, *args):
        query = self.search.text().casefold()
        posts = self.store.rows('SELECT * FROM posts ORDER BY updated DESC')
        snapshot = (query, posts)
        if getattr(self, '_posts_snapshot', None) == snapshot:
            return
        self._posts_snapshot = snapshot
        rows, photos = [], []
        for post in posts:
            content = json.loads(post['content'])
            if query not in (post['title'] + ' ' + content['text']).casefold():
                continue
            rows.append((post['id'], [post['title'], content['text'].replace('\n', ' ') or f"Вложений: {len(content['photos'])}",
                                     'Готов' if post['ready'] else 'Черновик', datetime.fromtimestamp(post['updated']).strftime('%d.%m %H:%M')]))
            photos.append(content['photos'][0] if content['photos'] else None)
        set_rows(self.posts, rows)
        for index, identity in enumerate(photos):
            if identity:
                media = self.store.rows('SELECT thumbnail,kind FROM post_media WHERE id=?', (identity,))
                if media:
                    pixmap = QPixmap()
                    pixmap.loadFromData(media[0]['thumbnail'])
                    icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay) if media[0]['kind'] == 'VIDEO' else QIcon(pixmap)
                    self.posts.item(index, 0).setIcon(icon)
        self.empty.setVisible(not rows)

    def refresh(self):
        from workspace_ui import STATUS
        self.refresh_posts()
        rows = self.store.rows('''SELECT j.*,b.title,b.next_at,a.name AS account_name,
            (SELECT COUNT(*) FROM items i WHERE i.job=j.id AND i.state='confirmed') AS done
            FROM jobs j JOIN broadcasts b ON b.job=j.id LEFT JOIN accounts a ON a.id=j.account
            WHERE j.deleted=0 ORDER BY j.created DESC''')
        if getattr(self, '_jobs_snapshot', None) != rows:
            self._jobs_snapshot = rows
            set_rows(self.jobs, [(r['id'], [r['title'] + ' / ' + (r['account_name'] or 'Аккаунт удалён'), STATUS.get(r['status'], r['status']),
                                          f"{r['done']} / {r['amount']}", datetime.fromtimestamp(r['next_at']).strftime('%d.%m %H:%M:%S') if r['next_at'] and r['status'] == 'running' else '']) for r in rows])
        self.update_detail()

    def update_detail(self):
        identity = selected_id(self.jobs)
        self.job_detail.setText((self.store.job(identity)['message'] or 'Готово к запуску').splitlines()[0] if identity else '')

    def create_post(self):
        PostEditor(self.store, parent=self).exec()
        self.refresh()

    def import_post(self):
        from workspace_post_import_ui import ImportPostDialog
        dialog = ImportPostDialog(self.store, self.engine, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            for row in range(self.posts.rowCount()):
                if self.posts.item(row, 0).data(Qt.ItemDataRole.UserRole) == dialog.identity:
                    self.posts.selectRow(row)
            PostEditor(self.store, dialog.identity, self).exec()
        self.refresh()

    def edit_post(self):
        identity = require_selection(self.posts, 'Выберите пост.')
        PostEditor(self.store, identity, self).exec()
        self.refresh()

    def duplicate_post(self):
        post = self.store.post(require_selection(self.posts, 'Выберите пост.'))
        identity = self.store.save_post(None, post['title'] + ' (копия)', post['content'], ready=post['ready'])
        self.refresh()
        for row in range(self.posts.rowCount()):
            if self.posts.item(row, 0).data(Qt.ItemDataRole.UserRole) == identity:
                self.posts.selectRow(row)

    def delete_post(self):
        identity = require_selection(self.posts, 'Выберите пост.')
        if QMessageBox.question(self, 'Удалить пост', 'Удалить пост из библиотеки? Копии в рассылках сохранятся.') == QMessageBox.StandardButton.Yes:
            self.store.delete_post(identity)
            self.refresh()

    def create_broadcast(self):
        identity = require_selection(self.posts, 'Выберите пост.')
        validate_content(self.store.post(identity)['content'])
        dialog = BroadcastWizard(self.store, self.engine, identity, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.tabs.setCurrentIndex(1)
            self.refresh()
            for row in range(self.jobs.rowCount()):
                if self.jobs.item(row, 0).data(Qt.ItemDataRole.UserRole) == dialog.identity:
                    self.jobs.selectRow(row)

    def start_job(self):
        identity = require_selection(self.jobs, 'Выберите рассылку.')
        job = self.store.job(identity)
        if job['status'] in ('running', 'complete', 'stopped'):
            raise ValueError('Рассылка уже запущена или завершена.')
        if not self.engine.available(job['account']):
            raise ValueError('Подключите аккаунт рассылки.')
        if QMessageBox.question(self, 'Запуск рассылки', 'Запустить выбранную рассылку с сохранёнными настройками?') == QMessageBox.StandardButton.Yes:
            self.engine.submit(job['account'], 'job', self.engine.run_job(identity))

    def control(self, action):
        identity = require_selection(self.jobs, 'Выберите рассылку.')
        job = self.store.job(identity)
        if job['status'] == 'running':
            self.engine.flags[identity] = action
        elif job['status'] not in ('complete', 'stopped'):
            self.store.status(identity, 'stopped' if action == 'stop' else 'paused', 'Остановлено пользователем' if action == 'stop' else 'На паузе')
        self.refresh()

    def report(self):
        identity = require_selection(self.jobs, 'Выберите рассылку.')
        BroadcastReport(self.store, self.engine, identity, self).exec()

    def view_post(self):
        identity = require_selection(self.jobs, 'Выберите рассылку.')
        post = self.store.broadcast(identity)
        dialog = QDialog(self)
        dialog.setWindowTitle(post['title'])
        dialog.resize(640, 550)
        layout = QVBoxLayout(dialog)
        text = QTextBrowser()
        text.setOpenLinks(False)
        load_content(text, post['content'])
        layout.addWidget(text, 1)
        photos = photo_list()
        photo_items(self.store, photos, post['content']['photos'])
        photos.itemDoubleClicked.connect(lambda item: MediaPreview(self.store, item.data(Qt.ItemDataRole.UserRole), dialog).exec())
        layout.addWidget(photos)
        layout.addWidget(button(dialog, 'Закрыть', dialog.accept))
        dialog.exec()

    def delete_job(self):
        identity = require_selection(self.jobs, 'Выберите рассылку.')
        if QMessageBox.question(self, 'Удалить рассылку', 'Убрать задание из списка? История отправок сохранится.') == QMessageBox.StandardButton.Yes:
            self.store.delete_job(identity)
            self.refresh()
