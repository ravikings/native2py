# nativegate console — self-hosting

The console is a small FastAPI app that lets you upload native (C++/Fortran)
code, review the inferred contract, build it, and get a live REST + MCP
service — all as plain `ngate` CLI commands run for you.

## Quick start

```
./console/start.sh
```

(equivalent to `docker compose up --build` from the repo root, with a couple
of sanity checks — Docker installed, daemon reachable — first)

Then open `http://localhost:8000`. By default `NGATE_AUTH=none`, so there is
no login — everything is owned by a single local user, suitable for
self-hosting on your own machine or a private box.

The console shells out to `docker` to run builds and deploy services, so the
container mounts your host's Docker socket (`/var/run/docker.sock`). Build
and project data live in the `console-data` named volume.

**`docker-compose.yml` binds the console to `127.0.0.1:8000` only, on
purpose.** `NGATE_AUTH=none` has no login, and this container can reach the
Docker socket — together, exposing that combination beyond localhost means
anyone who can reach the port can ask the console to run arbitrary `docker`
commands as root. Don't change the port mapping to `8000:8000` (or put this
behind a reverse proxy reachable from outside the host) without switching to
`NGATE_AUTH=github` first.

Port 8000 already taken? Pass `--port` (or set `CONSOLE_PORT`):

```
./console/start.sh --port 8010
# or: CONSOLE_PORT=8010 docker compose up --build
```

The container always listens on 8000 internally — only the host-side mapping
changes, so open `http://localhost:8010` instead.

## Orchestration

The console itself is single-instance, but the service containers it deploys
(one per project, `ngate-svc-<slug>`) are kept alive by a small orchestration
layer (`console/orchestrator.py`), not just left to fend for themselves:

- **Startup reconciliation** — every project the database thinks is
  `running` is checked against Docker on boot; if the container is gone
  (host reboot, someone ran `docker rm`), it's started back up from the
  last built image, or marked `stopped` if that image no longer exists.
- **Background health monitoring** — a loop polls each running project's
  `/healthz` every `NGATE_HEALTH_INTERVAL` seconds (default `30`). A
  project that fails 3 checks in a row gets auto-restarted; if the restart
  itself fails, it's marked `crashed` and left alone rather than
  restart-looped forever.
- **Fleet status** — `GET /api/services` returns live status (DB status,
  actual container state, port, health-check failure count) for every
  deployed project, useful for a quick "is anything down?" check without
  opening each project page.

This is still one Docker daemon on one host (the same socket mount described
above) — there's no multi-host scheduling here, just making sure what's
supposed to be running actually is.

## GitHub OAuth (`NGATE_AUTH=github`)

To let multiple people use one instance, switch to GitHub login:

1. Register a new OAuth app at
   [github.com/settings/developers](https://github.com/settings/developers).
2. Set the **Authorization callback URL** to
   `http://<your-host>:8000/auth/github/callback` (use your real domain if
   deploying publicly).
3. Set these environment variables (see the commented-out block in
   `docker-compose.yml`):
   - `NGATE_AUTH=github`
   - `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — from the OAuth app.
   - `NGATE_SECRET_KEY` — any long random string, used to sign session
     cookies.

Projects are scoped to the logged-in GitHub user id. The OAuth scope is
`read:user` only — the console never requests access to private repositories.

## Ports and data

- App: `8000`
- Data: `console-data` volume, mounted at `console/data` in the container
  (override the path with `NGATE_CONSOLE_DB`).
