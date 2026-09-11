# LinkedIn + X Post Bot (Telegram)

Turn any article/blog URL into a polished LinkedIn post and/or X thread from Telegram.

## Highlights

- `.env`-driven configuration (no `api_key.py` required)
- Supports **OpenAI** and **Azure OpenAI**
- Optional Telegram allowlist (`TELEGRAM_ALLOWED_USER_IDS`)
- Polling mode and webhook mode
- Safer thread parsing and cleaner architecture

## Quick start

### 1) Create and activate virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2) Configure environment

```bash
cp .env.example .env
# then fill in .env values
```

### 3) Run

```bash
python main.py
```

---

## Environment variables

### Core integrations

- `TELEGRAM_TOKEN`
- `LINKEDIN_TOKEN` (required for LinkedIn posting)
- `X_API_KEY`, `X_API_SECRET_KEY`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` (required for X posting)

### LLM provider

Set one provider:

#### OpenAI
- `LLM_PROVIDER=openai`
- `OPENAI_API_KEY`
- `OPENAI_MODEL` (default `gpt-4.1`)

#### Azure OpenAI
- `LLM_PROVIDER=azure_openai`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_ENDPOINT` (e.g. `https://<resource>.openai.azure.com`)
- `AZURE_OPENAI_API_VERSION` (default `2024-06-01`)
- `AZURE_OPENAI_DEPLOYMENT` (deployment name)

### Telegram runtime mode

#### Polling mode (default)
- `TELEGRAM_MODE=polling`
- `TELEGRAM_DROP_PENDING_UPDATES=true`

#### Webhook mode
- `TELEGRAM_MODE=webhook`
- `TELEGRAM_WEBHOOK_PUBLIC_URL=https://your-domain.com`
- `TELEGRAM_WEBHOOK_PATH=webhook`
- `TELEGRAM_WEBHOOK_LISTEN=0.0.0.0`
- `TELEGRAM_WEBHOOK_PORT=8443`
- `TELEGRAM_WEBHOOK_SECRET_TOKEN=` (optional but recommended)

### Optional safety/behavior

- `TELEGRAM_ALLOWED_USER_IDS` (comma-separated Telegram user IDs)
- `SCRAPER_TIMEOUT_SECONDS` (default `10`)

---

## Development workflow

Install dev tools:

```bash
pip install -r requirements-dev.txt
```

Run checks:

```bash
pytest
ruff check .
black --check .
```

Enable pre-commit:

```bash
pre-commit install
pre-commit run --all-files
```

---

## Docker

Build:

```bash
docker build -t linkedin-bot:latest .
```

Run with env file:

```bash
docker run --rm --env-file .env linkedin-bot:latest
```

If using webhook mode, publish the configured webhook port (default `8443`):

```bash
docker run --rm --env-file .env -p 8443:8443 linkedin-bot:latest
```

---

## Project layout

```text
main.py
telegram_bot/
  bot_app.py
  config.py
  handlers.py
  scraper.py
  summarizer.py
  linkedin_client.py
  twitter_client.py
  keyboards.py
  prompts.py
tests/
```
