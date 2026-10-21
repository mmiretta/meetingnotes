# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-21

First public release.

### Added
- Record meetings in the browser or upload audio files; transcription through a
  faster-whisper HTTP service with per-meeting model and language.
- Summaries in English and Spanish written by a local LLM through Ollama, or by any
  OpenAI-compatible API (`LLM_PROVIDER=openai`).
- Background processing with a configurable concurrency limit; interrupted jobs are reset
  on restart.
- Editing of transcripts and summaries, reprocessing with other models, per-language
  summary generation.
- Tags, tag filter, full-text search over titles, descriptions, transcripts and summaries.
- Multi-user accounts with per-user storage, admin user management, password change,
  `flask create-user` command and `ADMIN_EMAIL`/`ADMIN_PASSWORD` bootstrap.
- Optional sign-in through a trusted header set by an authenticating reverse proxy.
- English and Spanish user interface.
- Demo mode with fictional users and meetings, a synthetic sample recording and offline
  fallbacks.
- Docker image, Docker Compose file with optional `whisper` and `ollama` profiles and a GPU
  override, `/healthz` endpoint.

### Security
- CSRF protection on every state-changing request.
- Markdown is rendered with raw HTML escaped.
- Redirects after sign-in are restricted to local paths.
- Session cookies are `HttpOnly` and `SameSite=Lax`, optionally `Secure`.
- Front-end assets are served locally (no third-party CDNs or fonts).
