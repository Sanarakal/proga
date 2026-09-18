"""Offline import UI and actual Qt video encode/decode smoke check."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QSize, QUrl
from PySide6.QtGui import QImage, QColor, QPainter
from PySide6.QtMultimedia import QMediaCaptureSession, QMediaRecorder, QMediaFormat, QVideoFrameInput, QVideoFrame
from PySide6.QtWidgets import QApplication, QPushButton
from pymax.types import Message

from workspace_ui import configure_app, apply_theme
from workspace_store import Store
from workspace_engine import Engine
from workspace_posts_ui import PostEditor, MediaPreview
from workspace_post_import import message_content
from workspace_post_import_ui import ImportPostDialog


app = QApplication([])
configure_app(app)
output = Path(__file__).parent / 'preview'
output.mkdir(exist_ok=True)


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        app.processEvents()
        time.sleep(0.01)
    raise AssertionError('Qt media operation timed out')


with tempfile.TemporaryDirectory(prefix='max-import-preview-') as directory:
    root = Path(directory)
    recorder = QMediaRecorder()
    capture = QMediaCaptureSession()
    input_frames = QVideoFrameInput()
    capture.setRecorder(recorder)
    capture.setVideoFrameInput(input_frames)
    fmt = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
    fmt.setVideoCodec(QMediaFormat.VideoCodec.MPEG4)
    recorder.setMediaFormat(fmt)
    recorder.setVideoResolution(QSize(320, 240))
    recorder.setVideoFrameRate(10)
    recorder.setOutputLocation(QUrl.fromLocalFile(str(root / 'sample.mp4')))
    errors = []
    recorder.errorOccurred.connect(lambda *args: errors.append(str(args)))
    recorder.record()
    wait_until(lambda: recorder.recorderState() == QMediaRecorder.RecorderState.RecordingState or errors)
    assert not errors, errors
    try:
        for index in range(20):
            image = QImage(320, 240, QImage.Format.Format_RGB32)
            image.fill(QColor('#287961' if index % 2 else '#b84865'))
            painter = QPainter(image)
            painter.setPen(QColor('white'))
            painter.drawText(30, 120, f'MAX Workspace / frame {index + 1}')
            painter.end()
            frame = QVideoFrame(image)
            frame.setStartTime(index * 100000)
            frame.setEndTime((index + 1) * 100000)
            wait_until(lambda: input_frames.sendVideoFrame(frame))
    finally:
        recorder.stop()
        wait_until(lambda: recorder.recorderState() == QMediaRecorder.RecorderState.StoppedState)
    assert not errors, errors
    data = (root / 'sample.mp4').read_bytes()
    store = Store(root / 'data')
    account = store.add_account('Рабочий аккаунт')
    store.execute('INSERT INTO chats VALUES(?,?,?,?)', (account, -100, 'Новости команды', 'CHANNEL'))
    video = store.prepare_import_media(data, 'VIDEO')
    photo_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Web/Wallpaper/Windows/img0.jpg'
    photo = store.prepare_import_media(photo_path.read_bytes(), 'PHOTO')
    text = 'Новости\n\n  Встреча в пятницу.\nПодробнее'
    message = Message.model_validate(dict(id=123, chatId=-100, time=1700000000000, type='USER', text=text,
        elements=[dict(type='HEADING', **{'from': 0}, length=7),
                  dict(type='LINK', **{'from': text.index('Подробнее')}, length=9, attributes={'url': 'https://example.com'})]))
    content = message_content(message)
    content['photos'] = [photo['id'], video['id']]
    post = store.save_imported_post('Пост из MAX', content, [photo, video])
    engine = Engine(store)
    try:
        for theme in ('light', 'dark'):
            apply_theme(app, theme)
            picker = ImportPostDialog(store, engine)
            picker.request_context = (account, -100)
            picker.engine_event('result', (account, ('post_import_history', id(picker)), [message,
                message.model_copy(update={'id': 122, 'time': message.time - 3600000, 'text': 'План на неделю\n\nНовые задачи и ближайшие встречи', 'elements': []}),
                message.model_copy(update={'id': 121, 'time': message.time - 86400000, 'text': 'Материалы встречи\n\nЗапись и полезные ссылки', 'elements': []})]))
            picker.history.selectRow(0)
            picker.request_context = (account, -100)
            picker.engine_event('result', (account, ('post_import_thumbnails', id(picker)),
                                (123, [('PHOTO', photo['thumbnail']), ('VIDEO', photo['thumbnail'])])))
            editor = PostEditor(store, post)
            for dialog, name in ((picker, 'import-picker'), (editor, 'imported-post')):
                dialog.show()
                for width, height in ((820, 700), (640, 580)):
                    dialog.resize(width, height)
                    app.processEvents()
                    assert dialog.width() <= width
                    for control in dialog.findChildren(QPushButton):
                        if control.isVisible():
                            required = control.fontMetrics().horizontalAdvance(control.text()) + 32 + (24 if not control.icon().isNull() else 0)
                            assert control.width() >= required, (control.text(), control.width(), required)
                    dialog.grab().save(str(output / f'{name}-{theme}-{width}.png'))
                dialog.reject()
        preview = MediaPreview(store, video['id'])
        frames = []
        preview.player.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame) if frame.isValid() else None)
        preview.show()
        preview.player.play()
        wait_until(lambda: len(frames) >= 2)
        assert frames[0].isValid()
        frames[0].toImage().save(str(output / 'import-video-frame.png'))
        preview.grab().save(str(output / 'import-video-player.png'))
        preview.reject()
        print('IMPORT_PREVIEW_OK; decoded video frames:', len(frames))
    finally:
        engine.loop.call_soon_threadsafe(engine.loop.stop)
        engine.thread.join(5)
