<h1 align="center">Kaligo</h1>

An updated and maintained fork of [Caligo](https://github.com/userbotindo/caligo) which is a selfbot for Telegram made with Python using the [Pyrogram](https://github.com/pyrogram/pyrogram) library. It's highly inspired by [pyrobud](https://github.com/kdrag0n/pyrobud).

## Prerequisites

Kaligo is deployed entirely with **Docker Compose**, which is available for all major operating systems. Compose builds the bot image and runs a private, local MongoDB instance alongside it, so there's nothing else to install — you don't need Python or a cloud database on the host.

It's developed and tested on Linux. Windows and macOS should work too via Docker Desktop or WSL.

## Configuration

Configure Kaligo *before* running it for the first time. There are two files to set up.

### 1. `.env`

This holds the password for the bundled MongoDB instance. Copy the example and set a strong, random password:

```bash
cp .env.example .env && nvim .env
```

```dotenv
MONGO_PASSWORD=change-me-to-a-long-random-string
```

### 2. `config.toml`

Copy the sample and fill in the fields. Every setting is documented by the comments above it.

```bash
cp sample_config.toml config.toml && nvim config.toml
```

- **`api_id` / `api_hash`** — create an API app at [my.telegram.org/apps](https://my.telegram.org/apps) and copy the values. **Treat these like a password!**
- **`db_uri`** — Kaligo no longer uses a cloud database. MongoDB runs as the `mongo` service defined in `docker-compose.yml`, reachable only over Compose's internal network by the hostname `mongo`. Point the URI at that service using the password you just set in `.env`:

  ```toml
  db_uri = "mongodb://root:<MONGO_PASSWORD>@mongo:27017"
  ```

  Replace `<MONGO_PASSWORD>` with the exact value from your `.env`.

Configuration must be complete and correct before the first start, or the bot won't come up.

## First run & authentication

Build the image and run the session generator — this signs you into Telegram:

```bash
docker compose build && docker compose run --rm kaligo python3 generate_session.py
```

The first time you run it, Pyrogram prompts you for your phone number and the login code Telegram sends you (and your 2FA password, if enabled). Once you're signed in, the session is stored in MongoDB and reused on every subsequent start, so you only need to do this once.

## Running

Start the bot in the background:

```bash
docker compose up -d
```

Follow the logs with:

```bash
docker compose logs -f
```

To stop it, run `docker compose down`.

## Support

I have contacts on my GitHub profile and you can reach me from there.
