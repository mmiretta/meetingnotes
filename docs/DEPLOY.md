# Deploying MeetingNotes

This guide covers a production-style deployment with Docker Compose: the app, the
Whisper speech-to-text service and Ollama, optional GPU acceleration, a TLS reverse
proxy, backups and upgrades.

## 1. Services

| Service | Compose profile | Image | Port (internal) | Volume |
|---|---|---|---|---|
| `app` | *(always)* | built from `Dockerfile` | 5002 | `meetingnotes-data` → `/data` |
| `whisper` | `whisper` | built from `Dockerfile.whisper` | 9000 | `whisper-models` → `/models` |
| `ollama` | `ollama` | `ollama/ollama` | 11434 | `ollama-models` → `/root/.ollama` |

Only the app publishes a port, and by default only on `127.0.0.1`. Whisper and Ollama are
reachable from the app over the Compose network and have no authentication of their own:
do not publish their ports.

If you already run Whisper or Ollama, skip the profiles and set `WHISPER_URL` /
`OLLAMA_URL`. Services on the Docker host itself are reachable as `host.docker.internal`
(the compose file maps it to the host gateway). Remember that a service listening only on
the host's `127.0.0.1` is **not** reachable from a container; bind it to the Docker bridge
address or to `0.0.0.0` behind a firewall.

## 2. First start

```bash
git clone https://github.com/mmiretta/meetingnotes.git
cd meetingnotes
cp .env.example .env
```

Edit `.env`:

```dotenv
SECRET_KEY=<output of: openssl rand -hex 32>
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=<at least 8 characters>
SESSION_COOKIE_SECURE=1          # once it is served over HTTPS
SUMMARY_MODEL=llama3.2:3b
WHISPER_DEFAULT_MODEL=small
```

Start everything and pull a summary model:

```bash
docker compose --profile whisper --profile ollama up -d --build
docker compose exec ollama ollama pull llama3.2:3b
docker compose logs -f app
```

The first transcription downloads the Whisper model into the `whisper-models` volume, so
it takes longer. After the first start you can remove `ADMIN_PASSWORD` from `.env`; the
account stays in the database.

Check that it is healthy:

```bash
curl -s http://127.0.0.1:5002/healthz      # {"status":"ok","version":"..."}
docker compose ps                          # app should be "healthy"
```

## 3. GPU acceleration (NVIDIA)

Install the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
then check that containers can see the GPU:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Start with the GPU override, which rebuilds the Whisper image on a CUDA 12 + cuDNN 9
runtime and reserves the GPU for Whisper and Ollama:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml \
    --profile whisper --profile ollama up -d --build
docker compose logs whisper | grep loading      # device=cuda
```

Notes:

- `WHISPER_COMPUTE_TYPE=int8_float16` is a good default on GPUs. Use `float16` on recent
  cards for a little more accuracy, and `int8` on older GPUs (Pascal and earlier) that lack
  fast FP16.
- Whisper and the LLM share GPU memory. With 8 GB, `small`/`medium` plus a 3–4B model fit
  comfortably; `large-v3` plus a 7–8B model needs about 12 GB or more. Keep
  `JOB_CONCURRENCY=1` on a single small GPU.
- Ollama unloads idle models after five minutes; set `OLLAMA_KEEP_ALIVE` on the `ollama`
  service to change that.
- Without a GPU everything still works on the CPU, only slower: prefer `base` or `small`
  and a 3B summary model.

## 4. Reverse proxy and TLS

The app does not terminate TLS itself in the recommended setup. Keep it on
`127.0.0.1:5002` and put a reverse proxy in front. Browsers only allow microphone
recording on HTTPS pages (or `localhost`), so TLS is required for recording from other
machines.

Set `SESSION_COOKIE_SECURE=1` once the site is served over HTTPS. Allow large request
bodies (recordings of long meetings) and long timeouts.

### Caddy

Caddy obtains and renews certificates automatically:

```caddy
meetings.example.com {
    request_body {
        max_size 500MB
    }
    reverse_proxy 127.0.0.1:5002 {
        transport http {
            read_timeout 10m
            write_timeout 10m
        }
    }
}
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name meetings.example.com;

    ssl_certificate     /etc/letsencrypt/live/meetings.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/meetings.example.com/privkey.pem;

    client_max_body_size 500m;
    proxy_read_timeout   600s;
    proxy_send_timeout   600s;
    proxy_request_buffering off;

    location / {
        proxy_pass         http://127.0.0.1:5002;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name meetings.example.com;
    return 301 https://$host$request_uri;
}
```

### Single sign-on (optional)

If the proxy authenticates users (oauth2-proxy, Authelia, Authentik...) and passes the
user's e-mail in a header, MeetingNotes can sign those users in automatically:

```dotenv
TRUSTED_AUTH_HEADER=X-Auth-Request-Email
TRUSTED_PROXY_CIDRS=172.16.0.0/12      # the address(es) the proxy connects from
```

Only users that already exist in MeetingNotes are signed in this way; create them first.
Make sure the app is reachable **only** through that proxy and that the proxy strips the
header from incoming client requests, otherwise anyone could impersonate a user.

When the proxy runs in another container, connect it to the app's Compose network (or
publish the app port on the Docker bridge address with `BIND_ADDRESS`), and set
`TRUSTED_PROXY_CIDRS` to that network.

### Direct TLS (not recommended)

gunicorn can serve HTTPS itself. Mount a certificate and key into the container and set
`SSL_CERT_FILE` / `SSL_KEY_FILE`. The container health check uses plain HTTP and will
report the container as unhealthy in this mode.

## 5. Backups

Everything lives in the `meetingnotes-data` volume (`/data` in the container):

```
/data/users.db                 accounts
/data/.secret_key              session key (if SECRET_KEY is not set)
/data/<user-id>/meetings.db    meetings, transcripts, summaries, tags
/data/<user-id>/meetings/...   audio files
```

SQLite files should be copied consistently. A simple nightly backup:

```bash
#!/bin/sh
set -e
STAMP=$(date +%F)
DEST=/srv/backups/meetingnotes
mkdir -p "$DEST"
# consistent copies of the databases
docker compose exec -T app python - <<'EOF'
import pathlib, sqlite3
for db in pathlib.Path("/data").rglob("*.db"):
    if db.name.endswith(".backup.db"):
        continue
    src = sqlite3.connect(db)
    dst = sqlite3.connect(db.with_suffix(".backup.db"))
    src.backup(dst)
    dst.close(); src.close()
EOF
# archive the volume (databases + audio)
docker run --rm -v meetingnotes_meetingnotes-data:/data:ro -v "$DEST":/backup alpine \
    tar czf "/backup/meetingnotes-$STAMP.tar.gz" -C /data .
docker compose exec -T app sh -c 'find /data -name "*.backup.db" -delete'
find "$DEST" -name 'meetingnotes-*.tar.gz' -mtime +30 -delete
```

The volume name is `<project>_meetingnotes-data`; check it with `docker volume ls`.
Backups contain recordings and transcripts: encrypt them (for example with `age` or
`restic`) if they leave the machine.

To restore, stop the app, extract the archive into an empty volume, use the `*.backup.db`
files in place of the live databases if you took the copies above, and start it again:

```bash
docker compose stop app
docker run --rm -v meetingnotes_meetingnotes-data:/data -v /srv/backups/meetingnotes:/backup alpine \
    sh -c 'rm -rf /data/* /data/.secret_key && tar xzf /backup/meetingnotes-2026-01-31.tar.gz -C /data \
           && find /data -name "*.backup.db" | while read f; do mv "$f" "${f%.backup.db}.db"; done \
           && chown -R 1000:1000 /data'
docker compose start app
```

## 6. Upgrades and migrations

```bash
git pull
docker compose --profile whisper --profile ollama up -d --build
```

Take a backup first. Database migrations are automatic: on start (and on every sign-in)
the app adds any missing tables and columns to `users.db` and to each user's
`meetings.db`. Migrations only add; they never drop data. Check `CHANGELOG.md` for notes
that need manual action.

To pin a version, check out a release tag and set `MEETINGNOTES_VERSION` so the images are
tagged accordingly:

```bash
git checkout v1.0.0
MEETINGNOTES_VERSION=1.0.0 docker compose up -d --build
```

Rolling back to an older version works as long as no migration added something the old
version cannot handle; restoring the backup taken before the upgrade always works.

## 7. Operations

- Logs: `docker compose logs -f app whisper`.
- Health: `GET /healthz` (no authentication) returns the status and version.
- Jobs run in background threads of the app process. If the app restarts mid-job, the
  meeting goes back to "Pending" and can be restarted from its page.
- Deleting a user keeps their files in `/data/<id>/`; remove that folder to purge them.
- Bind mounts instead of named volumes: the container runs as UID 1000, so the host
  directory must be writable by that UID (`chown -R 1000:1000 ./data`).
