# MAX Workspace 1.0

Windows desktop application built with Qt / PySide6 and the unofficial PyMax API.
This is a locally tested first release, not a guarantee of live MAX capabilities.

## Run

Open `dist/MAX-Workspace/MAX-Workspace.exe`, or install using `install-workspace.ps1`.
In Accounts, add an entry and connect through QR confirmation on your phone.
Select the active account in the top bar or click its row in Accounts.
Use Settings to import an existing prototype QR session; the original file is
copied, not changed. Existing prototype builds remain available.

## Workflows

Chats: select a source chat and choose contacts. Jobs: create collection or
invitation tasks. Review candidates and explicitly check participant consent.
Choose an amount from 1 to 1000 (a local safeguard, not a MAX service limit).
Jobs are created in a queued state; select one and explicitly start it.

Contacts supports phone / ID lookup and TXT, CSV or TSV import. In the first
column use positive MAX IDs or international phone numbers beginning with +.
CSV/TSV can have an id or phone header. Data is resolved through MAX before
the confirmation dialog; confirmed additions are stored per account.

Invitation tasks read every available target membership page before sending.
An incomplete or restricted read prevents invitations. Target must be a group;
channel invitation is not implemented. Existing target members are excluded.
The final membership scan verifies invitations without a scan per user.
Source membership is cached in memory for five minutes; target checks are fresh.

Each mutation has a persistent pending record before transmission. Confirmed
operations are never automatically repeated on resume. Uncertain operations
are reconciled with MAX; unresolved ones stop the task for manual review.
Pause and stop apply between requests, not to a request already transmitted.
No unattended retries or automatic job startup after restarting the program.

## Rate limits

MAX too-many / flood replies suspend the task and set a five-minute local guard.
This is not a claim about MAX's actual cooldown. Resume is manual only and may
still fail. Default spacing is two seconds per account, configurable 1–30 seconds.
Do not use additional accounts to evade service restrictions. Library errors,
privacy settings and missing privileges can make an operation unavailable.

## Data And Security

Data lives in `%LOCALAPPDATA%/MAX-Workspace`. Contact names, IDs, jobs and logs
are stored in local SQLite; metadata is not encrypted. Session credentials are
protected with current-Windows-user DPAPI after disconnect / normal shutdown.
PyMax needs a plaintext SQLite working session while connected; it can remain
after a crash and is sealed on the next application startup. This release does
not claim full in-memory-only credential protection. Do not share the data folder.
Deleting an account removes local session files, contacts, jobs and logs only;
revoke the session separately in MAX to invalidate server access.

One application instance is allowed. Accounts are independent; maximum 20.
One operation per account runs at a time. No automatic multi-account distribution.
Logs redact phone-like numbers, URLs and common credential fields; raw API
payloads and QR login links are not logged. CSV reports may contain personal IDs.

## Verification And Build

Run `.venv/Scripts/python.exe -m unittest test_workspace test_workspace_ui test_app test_workflows`.
`render_workspace.py` renders eight pages at 1180x780 and 920x640 using demo data
and checks button text widths. It never connects to MAX.
Live phone search, participant reads, contact mutations and invitations need
testing on user-owned accounts with consenting participants. QR login worked
in the preceding prototype, but the new account manager needs live verification.

Version 1.1 adds local contact labels, explicit blocked/unavailable participant
skips, invitations in server batches of up to 20, and forwarding of one recent
message to up to 20 selected chats. Forwarding can filter by the explicit chat
IDs stored in MAX folders. Dynamic folder filters are not expanded locally.
Custom contact labels are local metadata; PyMax does not expose the web client's
custom contact-name editor. Forwarding is confirmed one target at a time because
PyMax exposes one destination per forward request.

Version 1.2 adds a global participant ledger. Confirmed contact additions keep
the participant MAX ID, first source chat, account, job and timestamp. Candidate
previews exclude those IDs for every account and source; atomic claims also stop
two running jobs from submitting the same participant concurrently. The Sources
page shows cumulative progress such as `20 / 1756`, account count, last activity,
participant details and CSV export. Existing confirmed collection jobs are
backfilled on startup. Unavailable users are not counted; ambiguous submissions
stay reserved until they can be verified.

Version 1.3 adds persistent light/dark themes (Settings), collection templates,
per-account favorite chats and a combined report for a multi-account run.
Templates preserve account IDs, source IDs and requested quantity, never consent
or an automatic start. Loading a template displays unavailable accounts and
requires explicit selection review and launch. Favorites reorder the chat list
without excluding non-favorites from search. The combined report includes all
jobs sharing the run ID, including locally archived jobs, and exports a CSV
snapshot. Neither report viewing nor template loading sends MAX requests.

Build: `.venv/Scripts/python.exe -m PyInstaller --noconfirm --onedir --windowed
--collect-all pymax --name MAX-Workspace workspace_ui.py`.
For reproducible Windows packaging use `build-workspace.ps1`, which cleans the
build PATH to avoid unrelated ICU/CRT libraries. `MAX-Workspace.exe --self-test`
opens and closes a temporary-data window without connecting to MAX.
Installation preserves the old program directory in a sibling previous-version
folder; user data is never moved or replaced.
Updates and signing are manual; no auto-updater, installer signature or anti-ban
guarantee. Use the bundled installation script for a per-user shortcut.
