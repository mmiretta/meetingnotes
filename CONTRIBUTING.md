# Contributing

Thanks for your interest in MeetingNotes! Bug reports, translations and pull requests are
welcome.

## Reporting issues

- Search existing issues first.
- Include the version (shown in the page footer and at `/healthz`), how you run it (Docker
  or not, GPU or CPU), the Whisper and LLM models, and the relevant lines of
  `docker compose logs app whisper`.
- **Never attach real recordings, transcripts or databases.** Reproduce with demo mode or
  a synthetic recording instead.
- Security problems: please use a private security advisory on GitHub, not a public issue.

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pybabel compile -d translations
DATA_DIR=./data DEMO_MODE=1 FLASK_DEBUG=1 flask --app app run --port 5002
```

Demo mode seeds fictional data into the empty `./data` folder and falls back to canned
results when no Whisper or LLM service is running, so you can work on the UI without models.
Delete `./data` to start over.

To run the real pipeline locally:

```bash
pip install -r requirements-whisper.txt
WHISPER_DEVICE=cpu python whisper_server.py          # listens on :9000
ollama pull llama3.2:3b                              # https://ollama.com
```

Or run the full stack with Docker:

```bash
DEMO_MODE=1 docker compose --profile whisper --profile ollama up -d --build
```

## Code style

- Python 3.11+, standard library first, few dependencies.
- Keep `app.py` framework-light: plain Flask, SQLite, server-rendered Jinja templates and a
  little vanilla JavaScript. No build step.
- Comments, log messages and identifiers in English. User-facing strings go through
  `_()` (Python and templates) so they can be translated.
- New configuration goes through environment variables, documented in `.env.example`,
  `docker-compose.yml` and the README table.
- Database changes must be additive and applied automatically in `init_auth_db()` /
  `init_user_db()`.

## Translations

The UI uses Flask-Babel. After adding or changing strings:

```bash
pybabel extract -F babel.cfg -k _l -o messages.pot .
pybabel update -i messages.pot -d translations
# edit translations/<lang>/LC_MESSAGES/messages.po
pybabel compile -d translations
```

To add a language, run `pybabel init -i messages.pot -d translations -l <code>` and add the
code to `LANGUAGES` in `app.py`. Summaries are currently generated in English and Spanish
(`summary_en` / `summary_es` columns); supporting more summary languages needs a small
schema change.

## Pull requests

- One topic per pull request, with a short description of the why.
- Check the main flows by hand in demo mode: sign in, create a meeting from the sample
  recording in `demo/`, edit a summary, switch language, manage users.
- Screenshots for the docs must come from demo mode only.
- By contributing you agree that your contribution is licensed under the MIT License.
