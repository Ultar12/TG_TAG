# GitHub to Heroku configuration sync

Heroku dynos do not read GitHub repository variables, GitHub Environment variables, or `.env` files directly. The workflow at `.github/workflows/sync-heroku-config.yml` copies the required values into Heroku Config Vars after each push to `main` and can also be run manually.

## One-time GitHub setup

In the repository, open **Settings → Environments**, create or select the `production` environment, and add these values there. Store credentials as **Secrets**; non-sensitive defaults may be Variables.

| Name | Type | Purpose |
|---|---|---|
| `HEROKU_API_KEY` | Secret | Heroku API key with permission to update the app configuration |
| `HEROKU_APP_NAME` | Variable or Secret | Exact Heroku app name, such as `tg-tag-tls-e186af` |
| `BOT_TOKEN` | Secret | Telegram BotFather token |
| `ADMIN_ID` | Secret or Variable | Numeric Telegram administrator ID |
| `YOUTUBE_CLIENT_ID` | Secret | Google OAuth client ID |
| `YOUTUBE_CLIENT_SECRET` | Secret | Google OAuth client secret |
| `YOUTUBE_REFRESH_TOKEN` | Secret | YouTube authorization refresh token |
| `YOUTUBE_AUTO_UPLOAD` | Variable | `true` to enable uploads |
| `YOUTUBE_LISTENER_CHANNEL` | Variable | `@helenefischer` |
| `YOUTUBE_LISTENER_INTERVAL` | Variable | `3600` for hourly checks |

If the values are in repository-level **Settings → Secrets and variables → Actions** instead of the `production` environment, remove the `environment: production` line from the workflow or create the matching environment and put the values there.

The workflow updates Heroku’s runtime configuration; it does not commit or print secret values. A normal GitHub push then restarts the Heroku dynos with the synchronized configuration.

Do not place real credentials in `.env`, GitHub files, or source code. If a token has been exposed, revoke it and generate a replacement.
