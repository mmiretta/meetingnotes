"""Gunicorn settings, driven by the same environment variables as the app."""
import os

bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '5002')}"
# One worker: transcription jobs run in background threads of this process.
workers = 1
threads = int(os.getenv("THREADS", "8"))
# Uploads of long recordings can take a while on slow links.
timeout = int(os.getenv("REQUEST_TIMEOUT", "600"))
accesslog = "-" if os.getenv("ACCESS_LOG", "1") not in ("0", "false", "no") else None
errorlog = "-"
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
# Optional direct TLS (a reverse proxy is recommended instead, see docs/DEPLOY.md).
certfile = os.getenv("SSL_CERT_FILE") or None
keyfile = os.getenv("SSL_KEY_FILE") or None
