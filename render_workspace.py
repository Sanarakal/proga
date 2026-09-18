import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QPushButton
from workspace_ui import Window, ForwardDialog, SourceMembersDialog, CollectAccountsDialog, BatchReportDialog, JoinLinksDialog, ChatCatalogDialog, configure_app, apply_theme
from workspace_links import LinkImport
from workspace_store import Store
from workspace_ui import ConnectDialog, LoginInputDialog
from workspace_posts_ui import PostEditor, BroadcastWizard, BroadcastReport


app = QApplication([])
configure_app(app)
with tempfile.TemporaryDirectory() as directory:
    store = Store(directory)
    account = store.add_account('Рабочий аккаунт')
    store.execute('UPDATE accounts SET max_id=? WHERE id=?', (10001, account))
    for uid, name in [(110, 'Александр'), (111, 'Елена'), (112, 'Дмитрий'), (113, 'Мария')]:
        store.contact(account, uid, name, 'Группа команды')
    for uid, name in [(-100, 'Группа команды'), (-101, 'Общий чат участников')]:
        store.execute('INSERT INTO chats VALUES(?,?,?,?)', (account, uid, name, 'CHAT'))
    job = store.new_job(account, 'collect', -100, 10, [110, 111, 112, 113])
    store.item(job, 110, 'confirmed')
    store.item(job, 111, 'confirmed')
    store.update_source(-100, 'Группа команды', 1756)
    store.confirm_harvest(-100, 110, account, job)
    store.confirm_harvest(-100, 111, account, job)
    store.status(job, 'paused', 'Подтверждено 2 / 10. Приостановлено пользователем.')
    store.log(account, 'Аккаунт подключён')
    join_entries = [dict(title='Рабочая группа', link='https://max.ru/join/demo-one', source='Лист1!6'),
                    dict(title='Обсуждения участников', link='https://max.ru/join/demo-two', source='Лист1!7')]
    store.save_join_file('Чаты.xlsx', b'demo', LinkImport(entries=join_entries + [dict(title='Свободная группа', link='https://max.ru/join/demo-three', source='8')]))
    join_job = store.new_join_job(account, join_entries, 2)
    store.item(join_job, 1, 'confirmed', 'MAX подтвердил участие')
    store.item(join_job, 2, 'unavailable', 'Ссылка истекла')
    store.status(join_job, 'complete', 'Вступил: 1 из 2')
    window = Window(store, demo=True)
    window.show()
    output = Path(__file__).parent / 'preview'
    output.mkdir(exist_ok=True)
    for theme in ('light', 'dark'):
        apply_theme(app, theme)
        login = ConnectDialog('Рабочий аккаунт', saved=True)
        login.method.setCurrentIndex(login.method.findData('phone'))
        login.show()
        app.processEvents()
        login.grab().save(str(output / f'login-{theme}.png'))
        login.close()
        for kind in ('code', 'password'):
            prompt = LoginInputDialog(kind, '+7 *** *** 4567', 'Рабочий аккаунт')
            prompt.show()
            app.processEvents()
            prompt.grab().save(str(output / f'login-{kind}-{theme}.png'))
            prompt.close()
    apply_theme(app, 'light')
    for width, height in [(1180, 780), (920, 640)]:
        window.resize(width, height)
        for page in range(len(window.names)):
            window.nav.setCurrentRow(page)
            app.processEvents()
            image = window.grab()
            image.save(str(output / f'page-{page}-{width}.png'))
            assert window.pages.width() > 600
            for button in window.pages.currentWidget().findChildren(QPushButton):
                if button.isVisible():
                    needed = button.fontMetrics().horizontalAdvance(button.text()) + 32 + (24 if not button.icon().isNull() else 0)
                    assert button.width() >= needed, (button.text(), button.width(), needed)
    window.engine.loop.call_soon_threadsafe(window.engine.loop.stop)
    window.engine.thread.join(5)
    window.timer.stop()
    window.hide()
    store.invalidate_catalog_link(join_entries[1]['link'], 'Ссылка истекла')
    catalog = ChatCatalogDialog(store)
    catalog.show()
    for width, height in ((1000, 680), (700, 540)):
        catalog.resize(width, height)
        app.processEvents()
        catalog.grab().save(str(output / f'catalog-light-{width}.png'))
    catalog.hide()
    join_dialog = JoinLinksDialog(store.rows('SELECT * FROM accounts'), store=store)
    join_dialog.select_all(True)
    join_dialog.consent.setChecked(True)
    join_dialog.show()
    for width, height in ((800, 660), (640, 580)):
        join_dialog.resize(width, height)
        app.processEvents()
        join_dialog.grab().save(str(output / f'join-light-{width}.png'))
    join_dialog.hide()
    dialog = ForwardDialog(
        [(7001, 'Проверочное сообщение с текстом', 1_700_000_000_000),
         (7002, 'Сообщение с вложением', 1_700_000_100_000)],
        [{'uid': -100, 'name': 'Группа команды', 'kind': 'Группа'},
         {'uid': -101, 'name': 'Общий чат участников', 'kind': 'Группа'}],
        [('Рабочая папка', [-100, -101])], None)
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(output / 'forward-dialog.png'))
    dialog.close()
    source = store.source_rows()[0]
    source_dialog = SourceMembersDialog(source, store.source_members(source['chat']), None)
    source_dialog.show()
    app.processEvents()
    source_dialog.grab().save(str(output / 'source-members-dialog.png'))
    source_dialog.close()
    second = store.add_account('Второй аккаунт')
    store.execute('INSERT INTO chats VALUES(?,?,?,?)', (second, -200, 'Другая группа', 'CHAT'))
    store.set_setting('collect_source_' + account, -100)
    store.set_setting('collect_source_' + second, -200)
    collect = CollectAccountsDialog(store, store.rows('SELECT * FROM accounts'))
    store.save_template('Ежедневный сбор', 20, [(account, -100), (second, -200)])
    collect.reload_templates('Ежедневный сбор')
    collect.select_all(True)
    collect.favorite_selected(True)
    collect.consent.setChecked(True)
    collect.show()
    for width in (780, 640):
        collect.resize(width, 520)
        app.processEvents()
        collect.grab().save(str(output / f'collect-{width}.png'))
    collect.close()
    store.set_job_options(job, {'group': 'demo'})
    second_job = store.new_job(second, 'collect', -200, 10, [301, 302], {'group': 'demo'})
    store.item(second_job, 301, 'confirmed')
    store.item(second_job, 302, 'pending')
    store.update_source(-200, 'Другая группа', 800)
    batch = BatchReportDialog(store.batch_report(job))
    batch.show()
    app.processEvents()
    batch.grab().save(str(output / 'batch-light.png'))
    batch.close()
    for title, state, reason in [('Основной', 'online', 'Вход в MAX подтверждён.'),
                                  ('Резервный', 'needs_login', 'MAX завершил сессию. Подключите аккаунт заново.'),
                                  ('Рабочий 2', 'limited', 'MAX ограничил запросы.'),
                                  ('Рабочий 3', 'blocked', 'MAX сообщил о блокировке аккаунта.')]:
        identity = store.add_account(title)
        store.account_status(identity, state, reason, checked=True)
        if state == 'online':
            window.engine.clients[identity] = SimpleNamespace(is_connected=True)
    window.reload_accounts()
    window.nav.setCurrentRow(0)
    window.show()
    for theme in ('light', 'dark'):
        store.set_setting('theme', theme)
        apply_theme(app, theme)
        for width, height in ((1180, 780), (920, 640)):
            window.resize(width, height)
            window.refresh()
            app.processEvents()
            window.grab().save(str(output / f'accounts-status-{theme}-{width}.png'))
    window.hide()
    window.theme.setCurrentIndex(window.theme.findData('dark'))
    window.show()
    for width, height in ((1180, 780), (920, 640)):
        window.resize(width, height)
        for page in range(len(window.names)):
            window.nav.setCurrentRow(page)
            app.processEvents()
            window.grab().save(str(output / f'dark-{page}-{width}.png'))
    window.hide()
    join_dialog.show()
    for width, height in ((800, 660), (640, 580)):
        join_dialog.resize(width, height)
        app.processEvents()
        join_dialog.grab().save(str(output / f'join-dark-{width}.png'))
    join_dialog.close()
    catalog.show()
    for width, height in ((1000, 680), (700, 540)):
        catalog.resize(width, height)
        app.processEvents()
        catalog.grab().save(str(output / f'catalog-dark-{width}.png'))
    catalog.close()
    collect.show()
    app.processEvents()
    collect.grab().save(str(output / 'collect-dark.png'))
    collect.close()
    batch.show()
    app.processEvents()
    batch.grab().save(str(output / 'batch-dark.png'))
    batch.close()
    sample = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Web/Wallpaper/Windows/img0.jpg'
    photos = [store.add_post_photo(sample)] if sample.exists() else []
    post = store.save_post(None, 'Анонс встречи', dict(
        text='Встреча участников\nВ пятницу в 19:00. Подробности на сайте.',
        elements=[{'type': 'STRONG', 'from': 0, 'length': 18},
                  {'type': 'LINK', 'from': 33, 'length': 20, 'url': 'https://example.com/event'}],
        photos=photos), True)
    broadcast = store.new_broadcast(post, account, [-100, -101], 'Вечерняя встреча', {'interval': 60, 'rounds': 2})
    store.begin_delivery(account, broadcast, 1)
    store.finish_delivery(broadcast, 1, 'confirmed', 'MAX подтвердил отправку', '123')
    for theme in ('light', 'dark'):
        apply_theme(app, theme)
        store.set_setting('theme', theme)
        window.nav.setCurrentRow(5)
        window.show()
        for width, height in ((1180, 780), (920, 640)):
            window.resize(width, height)
            for tab_index in (0, 1):
                window.posts_page.tabs.setCurrentIndex(tab_index)
                window.refresh()
                app.processEvents()
                window.grab().save(str(output / f'posts-{theme}-{width}-{tab_index}.png'))
        window.hide()
        editor = PostEditor(store, post)
        wizard = BroadcastWizard(store, window.engine, post)
        wizard.select_visible(True)
        wizard.update_review()
        report = BroadcastReport(store, window.engine, broadcast)
        for widget, name in ((editor, 'editor'), (wizard, 'wizard'), (report, 'broadcast-report')):
            widget.show()
            for width, height in ((820, 680), (640, 560)):
                widget.resize(width, height)
                steps = range(4) if widget is wizard else range(1)
                for step in steps:
                    if widget is wizard:
                        wizard.steps.setCurrentIndex(step)
                        wizard.show_step()
                    app.processEvents()
                    assert widget.width() <= width, (name, widget.width(), width)
                    for control in widget.findChildren(QPushButton):
                        if control.isVisible():
                            needed = control.fontMetrics().horizontalAdvance(control.text()) + 32 + (24 if not control.icon().isNull() else 0)
                            assert control.width() >= needed, (name, control.text(), control.width(), needed)
                    widget.grab().save(str(output / f'{name}-{theme}-{width}-{step}.png'))
            widget.reject()
print('Rendered all pages at desktop and minimum window sizes')
