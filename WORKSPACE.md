# MAX Workspace 1.10

## Import Posts From MAX (1.10)

The message picker uses a multiline history list and a responsive side-by-side
preview (stacked on narrow windows). Load more scrolls to the first newly loaded
message; empty/non-advancing pages disable further loading until a refresh.
Photo/video thumbnails download only on explicit request, with bounded public
HTTPS downloads, and never create posts or retain source tokens in the database.

My posts > Import from MAX selects an already connected account, its source chat
and a message from paged history or a numeric message ID. Import is read-only on
MAX and saves a local reusable post atomically after all attachments download.
Text, whitespace, hyperlinks, formatting entities and attachment order are retained.
Photos retain original downloaded bytes; MP4 video is stored locally and uploaded
through the MAX library when sending. Photo and video preview is available by
double-clicking an attachment. Saved media is included in encrypted transfers.

Bounds: 4000 UTF-16 text units, 10 attachments, 50 MB per imported attachment,
40 megapixels per photo, 128 MB total retained media. Unsupported attachments
(including SHARE preview cards, files, polls and stickers) reject the entire
import explicitly, rather than silently losing content. Text hyperlinks work.
INLINE_KEYBOARD is automatically omitted, as requested by the user. A successful
import explicitly notifies that buttons and their embedded links were omitted;
photos and text hyperlinks are retained. Only PHOTO/VIDEO count toward the
ten-media limit. Other unsupported types remain blocked. Failed photo downloads
abort the entire import, rather than silently saving fewer photos.
Complex formatting is retained but text editing is locked when the Qt editor
cannot round-trip it safely. Titles and attachment order remain editable.

Downloads require public HTTPS addresses, check redirect/DNS targets and enforce
streaming size/time limits. Signed URLs and source attachment tokens are not saved
in posts or shown in result messages. Failed/cancelled imports leave no partial post.
Tests use mocked MAX responses, not live accounts or real sends. Offline video
preview is verified by encoding and decoding a real MP4 with Qt Multimedia.

Modules: `workspace_post_import.py`, `workspace_post_import_ui.py`.
Checks: `test_post_import.py`, `render_post_import.py` plus the existing suite.

## Saved Posts And Broadcasts (1.9)

Forwarding now opens My posts / Broadcasts. Create a post with a local title,
rich text (bold, italic, underline, HTTP/HTTPS links) and up to 10 photos.
The editor autosaves drafts and supports duplicate, search, reorder, file drop
and preview. Pasted text is plain; links and formatting are explicit structured
entities with UTF-16 offsets, not reparsed Markdown. Local safeguards: 4000 UTF-16
units, 10 MB per source photo, 40 megapixels, 128 MB of retained photo data.
These are application bounds, not claims about MAX server limits. Photos are
normalized to JPEG (EXIF orientation applied, metadata removed, max 4096 pixels),
stored as SQLite blobs and included in the existing encrypted transfer archive.
Source files can be removed without losing saved post photos.

Create broadcast opens four steps: account, chats, settings, review. Chat
selection survives search, supports explicit MAX folder membership and saved
per-account sets. All CHAT, CHANNEL and DIALOG conversations for the selected
account are shown with their type. Entering the recipient step refreshes the
list for a connected free account; offline accounts show the last cached list.
Channels may reject publishing if the account has no permission. Settings include a fixed interval,
batch pause, local start/end date, 1-100 passes with a pause between passes,
working hours (including overnight), account-wide daily attempt cap, notifications
and a policy for explicit group-write denial. At most 1000 groups and 10000
deliveries per job. Creating a job does not send; press Start to arm it, including
scheduled jobs. The app must remain open and the account connected. An armed
job occupies that account until paused, stopped or complete.

Each job stores an immutable content snapshot and durable per-group/per-pass
records with client message IDs. Editing/deleting the library post cannot change
existing jobs. Photos referenced by archived jobs are retained. All sending uses
MAX payload models and one MSG_SEND invocation; no automatic message retries.
Photo uploads run before message intent. A transactional pending record and daily
attempt entry precede each send. Confirmation stores the server message ID and
checks returned chat/client IDs when supplied. Unknown responses/timeouts stay
pending and block resume until manual confirmation or skip without resending in
the report. Explicit MAX rejections may be retried only through manual resume.
Reports offer per-recipient results and CSV export with formula-safe cells.
Optional test send is a separate one-recipient job with an explicit confirmation.
Its recipient remains selected for the main broadcast, which may send again.

Pause/stop take effect before the next send; in-flight requests cannot be undone.
Restart never auto-arms jobs. A scheduling gap over 15 seconds or backward clock
change pauses waiting jobs to avoid catch-up sends after sleep. The start time
is local to this laptop. Daily caps count attempts across broadcast jobs for
the same account, including failed/uncertain sends. No live MAX send was used
for development verification; multi-photo presentation and account permissions
must be verified by the user with the explicit test-send control.

Tests: `python -m unittest test_workspace_posts` plus the existing suite.
`render_workspace.py` additionally renders posts, editor, all wizard steps and
delivery reports in both themes at 640/820 dialog widths and 920/1180 main widths.
Core files: `workspace_posts.py` (storage/validation), `workspace_posts_ui.py`
(Qt views), `workspace_broadcast.py` (transport and runner). Existing forward
job records and their runner remain supported.

## Account Status (1.8)

Accounts show text badges: green connected, yellow login required, red restricted
or blocked, gray offline/connecting/network error. A compact reason is displayed
beside the badge; hover shows transition time and last MAX reply. Both themes are
supported. Check status sends one read-only PING to an existing connection and
never starts authentication or a job. Offline accounts must first connect.

Status and transition logs persist in SQLite. On restart, live/connecting states
become offline; confirmed logout, block and rate-limit states survive. A local
two-second transport check retires disconnected idle clients. PyMax's existing
PING responses and raw logout/error events update status without extra polling
requests. No automatic reauthentication or job restart is added.

Only explicit session/account error codes set logout/block states. Target-user
block errors on contact operations, group permissions and generic forbidden
responses are not evidence that the current account is blocked. Unknown server
codes are not guessed. Network failure never erases a known block/restriction.
A five-minute local flood pause is not the server's ban duration. Expiry or a
successful PING does not prove the limit lifted; a later successful user-requested
operation can clear it. Tasks and multi-account selection respect these states.

Run `python -m unittest test_workspace_status test_workspace_auth test_chat_catalog test_workspace_links test_workspace test_workspace_ui test_app test_workflows`.

## Phone Login (1.7)

Accounts > Connect offers QR, phone, and saved-session login. Existing sessions
are reused by PyMax before either interactive flow. The phone is normalized to
international format; Russian 8/7 prefixes with 11 digits are accepted. The
SmsAuthFlow adapter supplies the phone to the WEB runtime without changing the
transport or saved device identity. Server support and code delivery are not
guaranteed; QR remains available. There is no automatic resend or registration.

Code/password dialogs are nonmodal and isolated per account. Cancellation and
the 240-second deadline close authentication; failed clients are closed and
sessions sealed with DPAPI. Code and password are never saved in app settings or
logs. Authentication API error payloads are replaced with credential-free messages.
2FA is checked once per manual login attempt, without an automatic retry loop.
Existing MAX account identity and duplicate-account checks apply to both modes.

Run `python -m unittest test_workspace_auth test_chat_catalog test_workspace_links test_workspace test_workspace_ui test_app test_workflows`.
Auth tests mock network calls; live phone delivery requires a user login test.

## Chat Catalog (1.6)

Product workflow: Joining > Chat catalog > import one or several files >
All chats > create a joining job. Files populate a shared persistent catalog,
not separate per-account lists. The dialog supports drag-and-drop, search,
available-only filtering, per-group account/state and an inactive-link audit view.
The first folder is All chats; folder membership is stored separately so future
folders can share groups without making copies. No extra folders are exposed yet.

Imports are parsed locally on a worker. Original files and parsed snapshots are
saved in SQLite together with catalog inserts in one transaction. Reimporting
the same file or overlapping files does not duplicate links. Moving/deleting
the source file has no effect on the catalog. Stored older job lists are migrated
once on upgrade; these recovered lists do not contain the original workbook bytes.

Identity has two stages: normalized invitation link before MAX resolution, then
MAX chat ID. Known alternate links are merged in the catalog. Until resolution,
two different URLs can still represent one group; the global chat-ID claim
prevents double joining even in that case. Counts of unresolved entries represent
unique links, not a guarantee that every entry is a different live group.

Global joining reservations use BEGIN IMMEDIATE and unique link/chat keys.
Every account checks this ledger before resolution and again before sending.
Confirmed entries and ambiguous pending writes survive task/account deletion.
Unsent reservations are released on pause/error/exit or application restart.
An ambiguous write is never automatically retried or assigned to another account.
Existing recorded confirmed/pending joins are backfilled on upgrade. Memberships
made outside this app can only be recognized when MAX reports them to this app.

Inactive links are removed from the working catalog, retaining a tombstone with
the reason/time so reimport cannot revive them. Explicit link-expired/invalid
errors or not-found during LINK_INFO are sufficient evidence. A generic not-found
from CHAT_JOIN is followed by LINK_INFO; only its explicit missing/expired reply
archives the link. Empty/ambiguous responses, timeouts, flood limits, full groups
and access restrictions never remove a link. A known alternate invitation URL
can remain active if only one URL expired. Existing jobs recheck tombstones.

Acceptance checks include concurrent accounts with matching and alias URLs,
crash/pause recovery, preserved pending writes, migration from old tasks,
cross-file deduplication, restart persistence, inactive-link reimports, folder
scoping, async multi-file import, and light/dark layouts at minimum sizes.
Run `python -m unittest test_chat_catalog test_workspace_links test_workspace test_workspace_ui test_app test_workflows`.

## Previous Releases

1.5.1 treats `not.found` / `errors.not.found` from CHAT_JOIN (57) and LINK_INFO
(89) as unavailable groups and continues toward the quota. On startup, pending
join records saved by 1.5 with an explicit not-found CHAT_JOIN rejection are
reclassified as unavailable. The saved error and attempt count are preserved;
the job still requires manual resume. Timeouts and other uncertain writes are
never repaired by this migration or automatically resubmitted.

## Join Groups From Files (1.5)

The Joining page imports XLSX, CSV or TXT locally, including Excel hyperlink
targets and literal HYPERLINK formulas (formulas are never evaluated). Import
runs on a worker thread. Accepted invitation URLs are max.ru/join/... and
web.max.ru/join/...; other domains and non-group URLs are excluded. An explicit
link column takes precedence over URLs in descriptions. Duplicates are normalized.
Preview is capped at 200 entries, but the entire imported list is used.
File limits: 30 MB compressed, 100 MB expanded, 500,000 cells, 50,000 links.

Select connected accounts, a quota of new groups per account and confirm Start.
Each selected account gets the same list and its own job. Invalid links, unavailable
groups and previous memberships are skipped while searching for new groups until
the quota or end of file. Only group chats are supported, not channel subscriptions.
MAX flood/too-many replies stop the job under the existing manual-resume policy.

History is keyed by account, link and resolved chat ID. It survives restart and
job deletion, but is explicitly removed with the local account. History supports
case-insensitive search, status filters and CSV export of all matching records;
the on-screen view displays up to 1000 matching records. Local metadata is not encrypted.

Before every join write, a pending item is committed. A returned group must match
the resolved group and prove participation (current user in participants/owner/admins,
or ACTIVE status with a positive join timestamp). Missing or unknown confirmation,
including possible approval requests, remains pending and stops the job. No blind
repeat is made after a timeout or ambiguous response. Resume uses a fresh link-info
request to reconcile pending records. Recognized group/link errors are skipped;
unknown errors stop and are not mislabeled as expired links. Past confirmed groups
are not automatically rejoined even if the user subsequently left them in MAX.

Live joins are not part of development tests. Run tests with:
`python -m unittest test_workspace test_workspace_links test_workspace_ui test_app test_workflows`.

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

Version 1.4 caches collection pages for five minutes per account and source.
The remaining candidates can be reused by later jobs; invitation membership
checks never use this cache. Explicit invalid/expired marker errors restart a
scan once; rate-limit errors still suspend it. Settings offers a cache reset.
Read and mutation pacing use separate clocks, both defaulting to two seconds.
Collection success is committed atomically with the contact, ledger and job
progress. Reports persist accumulated active execution time, pacing time,
API wait time, API invocation count and cached-page count across resumes.

Build: `.venv/Scripts/python.exe -m PyInstaller --noconfirm --onedir --windowed
--collect-all pymax --name MAX-Workspace workspace_ui.py`.
For reproducible Windows packaging use `build-workspace.ps1`, which cleans the
build PATH to avoid unrelated ICU/CRT libraries. `MAX-Workspace.exe --self-test`
opens and closes a temporary-data window without connecting to MAX.
Installation preserves the old program directory in a sibling previous-version
folder; user data is never moved or replaced.
Updates and signing are manual; no auto-updater, installer signature or anti-ban
guarantee. Use the bundled installation script for a per-user shortcut.
