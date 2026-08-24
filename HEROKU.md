# Deploying TG_TAG to Heroku

This repository supports Heroku deployment through the Python buildpack and the APT buildpack. The APT buildpack installs `ffmpeg` and `tesseract-ocr`, while the Python buildpack installs the packages listed in `requirements.txt`.

## Commands

For a native Heroku buildpack deployment, Heroku installs dependencies automatically from `requirements.txt`. There is no separate build command required. The process command is declared in `Procfile`:

```text
worker: python bot.py
```

The bot runs in polling mode on Heroku because Heroku does not provide Render’s `RENDER_EXTERNAL_URL`. Start one worker dyno after deployment:

```bash
heroku ps:scale worker=1 --app YOUR_APP_NAME
```

## Deploy

Create the app with the Heroku CLI, then push the repository:

```bash
heroku create YOUR_APP_NAME
heroku buildpacks:clear --app YOUR_APP_NAME
heroku buildpacks:add --index 1 https://github.com/heroku/heroku-buildpack-apt --app YOUR_APP_NAME
heroku buildpacks:add heroku/python --app YOUR_APP_NAME
git push heroku main
heroku ps:scale worker=1 --app YOUR_APP_NAME
heroku logs --tail --app YOUR_APP_NAME
```

Set the required config vars in the Heroku Dashboard or CLI: `BOT_TOKEN`, `DATABASE_URL`, and `ADMIN_ID`. The remaining API variables are optional and enable their corresponding bot features. Do not commit changes to `.env` or paste secret values into this guide.

For an app created from `app.json`, Heroku can use the declared buildpacks, config-variable prompts, and worker formation. Existing Heroku apps still need their buildpacks and dyno formation updated with the CLI because changes to `app.json` do not retroactively change an existing app.
