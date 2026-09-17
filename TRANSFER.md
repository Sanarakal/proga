# Перенос на ноутбук

В GitHub хранится код. Сессии, контакты, база чатов, задания и история находятся
в `%LOCALAPPDATA%\MAX-Workspace` и не синхронизируются через Git.
Приватный репозиторий тоже не предназначен для хранения ключей входа.

## Что перенести отдельно

Папка экспорта содержит два файла:

- `workspace.maxbackup`: зашифрованная база приложения и сохранённые сессии.
- `workspace.recovery-key`: случайный ключ расшифровки. Его содержимое никому не отправляйте.

Передайте архив на свой ноутбук, а ключ лучше передать отдельно, например на флешке.
Обладатель обоих файлов сможет получить сессии ваших аккаунтов.
Не прикрепляйте их к GitHub, задачам, сообщениям или публичным облачным ссылкам.
Без ключа восстановить архив невозможно.

Сессии на старом ПК защищены Windows DPAPI. Обычное копирование `session.protected`
на другой компьютер не работает. Утилита расшифровывает их только в памяти старого
ПК, шифрует архив и при импорте заново защищает сессии Windows уже на ноутбуке.
Незашифрованные файлы сессий при переносе не создаются.

## Подготовка ноутбука

Нужны Windows 10/11, Git и Python 3.13 с компонентом Python Launcher (`py`).
В PowerShell:

```powershell
git clone https://github.com/Sanarakal/proga.git
cd proga
powershell -ExecutionPolicy Bypass -File .\setup-workspace.ps1
```

Если репозиторий приватный, войдите в свой GitHub при запросе Git Credential Manager.
Не запускайте приложение до восстановления данных.

## Восстановление

Положите два файла переноса, например, в `Downloads\MAX-Workspace-Transfer`.
Из папки `proga` выполните:

```powershell
.venv\Scripts\python.exe transfer_workspace.py restore --archive "$env:USERPROFILE\Downloads\MAX-Workspace-Transfer\workspace.maxbackup" --key-file "$env:USERPROFILE\Downloads\MAX-Workspace-Transfer\workspace.recovery-key" --recover-orphans
.venv\Scripts\python.exe workspace_ui.py
```

Если в папке данных на ноутбуке уже есть файлы, импорт остановится и ничего не
заменит. Закройте приложение и переименуйте существующую папку данных в резервную
копию, затем повторите импорт:

```powershell
$workspaceBackup = 'MAX-Workspace.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
Rename-Item -LiteralPath "$env:LOCALAPPDATA\MAX-Workspace" -NewName $workspaceBackup
```

Параметр `--recover-orphans` создаёт записи «Восстановленный 1», «Восстановленный 2»
и далее для файлов сессий, чьи записи аккаунтов отсутствуют в исходной базе.
После входа можно переименовать их в разделе «Аккаунты». Без этого параметра
такие файлы сохраняются в папке сессий, но не появляются в списке аккаунтов.
Названия и историю удалённых записей аккаунтов из файла сессии вернуть нельзя.
При превышении 20 аккаунтов такое восстановление останавливается до записи данных.

В приложении выберите аккаунт, нажмите «Подключить» и выберите «Сохранённая сессия».
После переноса аккаунты сначала отображаются отключёнными; это не означает потерю
входа. Действующая сессия используется без QR и нового кода. Отозванные, истёкшие
или отклонённые MAX сессии потребуют нового входа; перенос не может обойти проверку MAX.
Задания не запускаются автоматически. Используйте рабочую копию на одном компьютере:
параллельная работа со снимками базы может привести к повторным действиям.

## Продолжить разработку

Основное приложение: `workspace_ui.py`, `workspace_engine.py`, `workspace_store.py`.
История функций и ограничения описаны в `WORKSPACE.md`.
Зависимости зафиксированы в `requirements-lock.txt`. Установка PyMax не зависит
от папки `vendor` на старом ПК: пакет версии 2.4.1 проверен на совпадение с ней.

Проверки текущего приложения:

```powershell
.venv\Scripts\python.exe -m unittest test_transfer_workspace test_workspace_status test_workspace_auth test_chat_catalog test_workspace_links test_workspace test_workspace_ui test_workflows -q
.venv\Scripts\python.exe render_workspace.py
```

Старый прототип `app.py` и его `test_app.py` сохранены отдельно от основного приложения.
Тесты прототипа лучше запускать отдельным процессом, поскольку он использует Tk,
а основное приложение использует Qt.

Сборка обычного Windows-приложения и создание ярлыка:

```powershell
powershell -ExecutionPolicy Bypass -File .\build-workspace.ps1
powershell -ExecutionPolicy Bypass -File .\install-workspace.ps1
```

## Новый снимок для следующего переноса

Закройте MAX Workspace на исходном компьютере и выполните из папки проекта:

```powershell
$workspaceTransfer = Join-Path $env:USERPROFILE ('Downloads\MAX-Workspace-Transfer-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
.venv\Scripts\python.exe transfer_workspace.py export --output "$workspaceTransfer"
.venv\Scripts\python.exe transfer_workspace.py verify --archive "$workspaceTransfer\workspace.maxbackup" --key-file "$workspaceTransfer\workspace.recovery-key"
```

Экспорт — снимок на момент запуска команды, а не постоянная синхронизация.
После новых действий на старом компьютере для переноса актуальной истории нужен
новый экспорт. Проверка `verify` проверяет расшифровку и целостность базы локально,
но не подключает аккаунты к MAX и не проверяет действительность сессий на сервере.
Для включения сессии старого прототипа при экспорте можно дополнительно указать
`--legacy-session "$env:LOCALAPPDATA\MAX-Test\sessions\test-web.db"`.
