# Deploying TG_TAG to Heroku

This repository supports Heroku deployment through the APT, Node.js, and Python buildpacks. The APT buildpack installs `ffmpeg` and `tesseract-ocr`, the Node.js buildpack provides the JavaScript runtime needed by current YouTube extraction, and the Python buildpack installs the packages listed in `requirements.txt`.

## Heroku Dashboard deployment

Connect the `Ultar12/TG_TAG` repository from the Heroku Dashboard **Deploy** tab. The app must use the classic Cedar buildpack system for the APT buildpack URL to apply.

For an existing Heroku app, open **Settings → Buildpacks** and add the following entries in this order:

```text
https://github.com/heroku/heroku-buildpack-apt
heroku/nodejs
heroku/python
```

The Python buildpack must be last because Python is the primary runtime. After saving the buildpacks, go to **Deploy → GitHub**, select the `main` branch, and choose **Manual deploy** or enable automatic deploys.

The repository’s `.buildpacks` and `app.json` files declare the same buildpack order for supported app-creation flows. They do not retroactively change the buildpack settings of an already-created Heroku app; those settings belong to the Heroku app itself.

## Process and dependencies

Heroku installs `requirements.txt` automatically. The `Procfile` includes both process types:

```text
web: python bot.py
worker: python bot.py
```

Use the `web` process for this Telegram bot because it runs Telegram webhooks on Heroku. The repository’s `Aptfile` installs the system packages required by the media features.

## Environment variables

Set `BOT_TOKEN`, `ADMIN_ID`, and `WEBHOOK_URL` in the Heroku Dashboard under **Settings → Config Vars**. Set `WEBHOOK_URL` to the public HTTPS base URL of the app, for example `https://your-app.herokuapp.com`, without the bot-token path. `DATABASE_URL` is strongly recommended for persistent user data; if it is absent, the bot now starts with an ephemeral SQLite database and logs a warning. The other API variables are optional and enable their corresponding features.

The YouTube handlers enable yt-dlp’s Node.js EJS runtime, and `requirements.txt` installs yt-dlp’s official default extras. YouTube may still require a valid `cookies_youtube.txt` secret file when it challenges the Heroku IP with “Sign in to confirm you’re not a bot.” Configure the cookie-file path with `YTDL_COOKIES_FILE` if you use one.

Do not commit new secret values to `.env` or this guide.

## Troubleshooting

If the logs show `Missing required environment variables`, check `BOT_TOKEN` and `ADMIN_ID` first. If the logs show `WEBHOOK_URL is required for Heroku webhook mode`, add the app’s public HTTPS URL as the `WEBHOOK_URL` Config Var. If logs report that no JavaScript runtime is available, check that `heroku/nodejs` is between the APT and Python buildpacks. If an APT binary such as `ffmpeg` is missing, check **Settings → Buildpacks** and confirm that the APT buildpack is listed first, followed by Node.js and then `heroku/python`. Keep `web.1` enabled and do not use the worker process for the webhook deployment.
