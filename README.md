# TelegramBot — screenshot filing

## What this is for

Plainly: you send screenshots of your work schedule to a private Telegram bot,
and it files them in cloud storage in folders named after the date the schedule
is for. Later you can find any day's screenshots by looking under that date,
without having named or sorted anything yourself.

The bot is private. Only the Telegram user ids you list in `.env` can talk to
it; everyone else is ignored silently.

## How a session works

Filing happens in a session, because a schedule is usually several screenshots
that belong together and their order matters.

1. `/job_start 2026-12-29` — you state the date the schedule is *for*. The bot
   checks storage to see whether that date has been filed before, and tells you
   whether this will be the first batch or a later version.
2. You send the screenshots, in order. The bot queues them and replies with a
   running count. Nothing is uploaded yet.
3. `/finish_job` — the bot downloads each queued image from Telegram and uploads
   it to storage, then closes the session.

Two commands end a session without filing anything: `/job_cancel` discards it
deliberately, and doing nothing for ten minutes expires it automatically. In
both cases storage is untouched, because uploads only ever happen in step 3.
That is the main reason uploads are deferred — there is never a half-written
batch to clean up.

`/status` tells you whether a session is open and how many screenshots are
queued. `/help` repeats all of the above.

## Where files end up

```
2026/12/29/v1/01.png
2026/12/29/v1/02.png
2026/12/29/v2/01.png     <- the same date filed a second time
```

The path is date and version only. The order lives in the filename, numbered
from 01 in the order you sent the images.

Versions exist so that refiling a date never destroys the earlier batch. **The
highest version is the current one** — that is the only rule for reading the
bucket, and there is no database or metadata anywhere, deliberately.

The version number is not stored either; it is worked out at upload time by
asking storage which version folders already exist under that date. Object
storage has no real directories, so an unfiled date simply has nothing under
its prefix and becomes `v1`.

## Date format

Only `YYYY-MM-DD` is accepted, and only with zero padding: `2026-03-04`, not
`2026-3-4` and not `2026/3/4`. Anything else is rejected with a usage message.

This is strict on purpose. A date is the only thing that determines where files
land, so a mis-parsed one files a batch somewhere you will never think to look.
Year-first also removes the day/month ambiguity that `03/04/2026` has.

## The modules

| file | what lives there |
|---|---|
| `config.py` | reads `.env` into typed objects, builds the storage client |
| `dates.py` | the strict date parser and the date-to-path conversion |
| `storage.py` | works out the next version, uploads an image |
| `sessions.py` | the inactivity timer that expires abandoned sessions |
| `main.py` | the bot itself: middleware and all command handlers |

Handler order in `main.py` matters. Telegram delivers every message to one
polling loop, and the library dispatches to the **first** handler whose filters
match. So the handlers that require an open session are registered above the
ones that do not, and the "no session open" replies at the bottom only fire
when nothing above claimed the message.

## Setup

Install Python 3.14 or later and uv. Copy `.env.example` to `.env` and fill in
your credentials. Keep `.env` in the project root (next to `pyproject.toml`, not inside the
package — it must never end up in a build):

```dotenv
TELEGRAM_BOT_API_TOKEN=123456:ABC...
ALLOWED_USER_IDS=YOUR_TELEGRAM_USER_ID
R2_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=schedules
```

`ALLOWED_USER_IDS` is comma-separated. `.env` values are always strings; the
parsing into a set of integers happens in `config.py`.

To find your own user id, send the bot a message and log `message.from_user.id`
in the whitelist middleware.

Then:

```bash
uv sync
uv run telegrambot
```

The process runs until you stop it. It is a client, not a server: it repeatedly
asks Telegram for new messages, so it needs no open port, no public address and
no certificate, and works from a laptop behind NAT.

While it is stopped, Telegram queues incoming messages for about 24 hours and
delivers them when it starts again.

## Things worth knowing

**Only one copy may run at a time.** Two processes polling the same bot token
fight over the same message queue and each gets a random half. If the bot
answers every other message, look for a stale process.

**Photos and files are not the same thing.** An image sent through the photo
picker is recompressed by Telegram and loses its filename; sent as a file, it
arrives intact. Only images sent as files are accepted; photos are rejected.

**Telegram will not hand a bot anything over 20 MB.** Larger files are skipped
with a message.

**Sessions do not survive a restart.** State is held in memory, so restarting
the process drops any open session — nothing is uploaded, and you start again.

**A failed upload keeps the session open.** If the upload breaks partway, the
bot says how many landed and leaves the session open so `/finish_job` can be
retried. A retry allocates a new version; the earlier partial version remains
in storage.

## Credentials and publishing

Commit `.env.example` and `uv.lock`; keep real credentials in `.env` or inject
all six variables through your deployment environment. A `.env` file is optional
when the variables are already set. Never paste real credentials into source,
issues, logs, or screenshots. Keep the R2 bucket private and scope its credentials
to the required bucket operations. If a credential has ever been published,
rotate it; adding an ignore rule does not remove it from Git history.

## Docker

Build and run locally:

```bash
docker build -t telegrambot .
docker run --rm --init --env-file .env telegrambot
```

The multi-stage image uses Python 3.14 and installs production dependencies
from `uv.lock`. It runs as a non-root user. Only the installed environment is
copied into the runtime image; credentials are supplied at runtime. No port or
volume is required. Stop any other copy of the bot before starting this one.

## Deploy with Coolify

1. Commit and push `Dockerfile`, `src/`, `pyproject.toml`, `uv.lock`, `README.md`,
   `.dockerignore`, `.gitignore`, `.python-version`, and `.env.example`.
2. Create an application from your GitHub repository and select branch `master`
   (or whichever branch contains the application files).
3. Select the **Dockerfile** build pack, repository root `/` as the base
   directory, and `/Dockerfile` as the Dockerfile location. Keep the default
   command from the image.
4. Add all six variables from `.env.example` in Coolify's **Environment
   Variables**, with your real values. Enable runtime availability; disable
   build-time availability. Do not add credentials as Docker build arguments.
5. Leave domains and host port mappings empty. This bot only makes outbound
   HTTPS requests to Telegram and R2. Disable Coolify's HTTP health check:
   the bot has no web endpoint. If your Coolify version requires a value in
   **Ports Exposes**, a placeholder such as `3000` can satisfy that field;
   do not publish/map it or configure a domain for it.
6. Run exactly one replica. Avoid overlapping deployments: stop the existing
   bot before starting its replacement, and stop any local polling process.
   Use a restart policy such as `unless-stopped` if configuring it manually.
7. Deploy and check for `polling started` in the logs. From an allowed Telegram
   account, send `/help`, then test a screenshot filing session.

Open sessions are lost on restart. There is no persistent local data to mount;
filed images live in R2. With HTTP health checks disabled, a running container
alone does not prove that Telegram or R2 is reachable; inspect deployment logs
and perform the Telegram test above.

References: [Coolify Dockerfile builds](https://coolify.io/docs/applications/builds/dockerfile),
[Coolify health checks](https://coolify.io/docs/applications/configuration/health-checks),
and [uv Docker integration](https://docs.astral.sh/uv/guides/integration/docker/).
