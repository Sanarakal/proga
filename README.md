# MAX Workspace

## Начать здесь на новом ноутбуке

Это существующее Windows-приложение, а не задание написать его с нуля.
Помощнику ChatGPT/Codex сначала нужно прочитать [HANDOFF.md](HANDOFF.md)
и [AGENTS.md](AGENTS.md), затем [TRANSFER.md](TRANSFER.md).
В них описаны контекст, порядок установки, восстановление сессий, проверки
и правила дальнейшей разработки. Секретов и пользовательской базы в GitHub нет.

Можно отправить помощнику такое сообщение:

> https://github.com/Sanarakal/proga
>
> Это моё приложение MAX Workspace. Прочитай README.md, HANDOFF.md, AGENTS.md
> и TRANSFER.md. Помоги развернуть существующий проект на этом ноутбуке,
> восстановить сессии из моего отдельного локального архива и подготовить
> приложение к дальнейшей разработке. Не создавай приложение заново,
> не перезаписывай имеющиеся данные и не запускай задания в MAX.
> Если архива нет на ноутбуке, попроси меня перенести его и отдельный ключ,
> но не проси отправлять их содержимое в чат.

## Overview

Windows desktop application for managing MAX accounts, collection jobs and reports.

Current release: MAX Workspace 1.10 (Qt / Windows). Import chat posts with text,
formatting, hyperlinks, photos and MP4 video into the saved-post library. Saved rich-text/photo posts,
scheduled broadcasts with delivery reports, phone and QR login, account
status badges, contact collection, invitations, reports and a shared chat catalog.

**Перенос на ноутбук и восстановление сессий: [TRANSFER.md](TRANSFER.md).**
Clone this repository and run `setup-workspace.ps1`. Restore the separate encrypted
transfer archive before launching the application for the first time.
Never upload account data, transfer archives or recovery keys to this repository.

## Run From Source

### Обновить на другом ПК

В существующей копии репозитория, после обычного закрытия MAX Workspace:

```powershell
git pull --ff-only origin main
powershell -ExecutionPolicy Bypass -File .\setup-workspace.ps1
powershell -ExecutionPolicy Bypass -File .\build-workspace.ps1
powershell -ExecutionPolicy Bypass -File .\install-workspace.ps1
```

Если Git сообщает о локальных изменениях, сохраните их перед обновлением;
не используйте принудительный сброс. На новом ПК сначала клонируйте репозиторий.
Git переносит только код: аккаунты, сохранённые посты и задания переносятся
отдельным зашифрованным архивом по `TRANSFER.md`. Существующие данные установщик
не заменяет. Не запускайте одну и ту же рассылку одновременно на двух ПК.

Requires Windows and Python 3.13. In PowerShell:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe workspace_ui.py
```

Test the current application:

```powershell
.venv\Scripts\python.exe -m unittest test_transfer_workspace test_workspace_status test_workspace_auth test_chat_catalog test_workspace_links test_workspace test_workspace_ui test_workspace_posts test_post_import test_workflows
```

Build with `build-workspace.ps1`, then install with `install-workspace.ps1`.
User data is stored outside this repository in `%LOCALAPPDATA%\MAX-Workspace`.
Build output, session files, local exports and the upstream vendor checkout are
excluded from Git. Dependencies are installed from `requirements-workspace.txt`.

## Legacy Prototype

The new Qt desktop release is described in [WORKSPACE.md](WORKSPACE.md).
Run `dist/MAX-Workspace/MAX-Workspace.exe` for the current account manager.
The prototype versions below are retained for comparison.

Windows prototype for one user-owned MAX account. Uses the unofficial PyMax API.
No automatic retries, privacy bypasses, or anti-ban guarantees.

Run `dist/MAX-Test-v5.exe`. QR login is selected by default and does not require
a phone number. Scan the displayed code using MAX on your already logged-in
phone and confirm the new session. Closing the QR window cancels login.
QR sessions are stored separately as `test-web.db`.
Alternatively, choose phone login. It requires your international phone number,
then a confirmation code and possibly a 2FA password in local dialogs.
Find a consenting participant by phone or MAX user ID, then use the separate
contact and group-invitation buttons. The group field accepts a numeric MAX ID.
Invitation does not expose previous history to the new participant.

Sessions contain credentials and are stored in `%LOCALAPPDATA%/MAX-Test/sessions`.
Do not share this folder. This prototype supports only one saved account;
to change accounts, close the application and revoke the test session in MAX
before manually removing its local session folder.

An API response is not an independent confirmation of group membership.
Check the actual contact and group in MAX after each operation.
The chat workflow adds a requested number of new contacts from a source chat,
then separately invites a requested number of account contacts to a target group.
Existing contacts and self are skipped during collection. All target membership
pages must be readable before invitations begin. Each invitation is independently
checked against group membership. A failed or unverified operation stops the job.
Stop takes effect between requests; it cannot undo a request already sent.
Only consenting participants should be processed. Counts are capped at 1000 per
test job (an application safeguard, not a MAX limit). Contact counts reflect
login data plus contacts confirmed during this application session.
Live contact and invitation operations have not been verified by the developer.
API failures now show a redacted server message, error code and operation ID.
No raw payloads are saved or displayed. Phone lookup availability depends on MAX.

Build from source with `.venv/Scripts/python.exe -m PyInstaller --noconfirm
--onefile --windowed --collect-all pymax --collect-all customtkinter --name MAX-Test-v5 app.py`.
