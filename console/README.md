# nativegate console — self-hosting

The console is a small FastAPI app that lets you upload native (C++/Fortran)
code, review the inferred contract, build it, and get a live REST + MCP
service — all as plain `ngate` CLI commands run for you.

## Quick start

```
docker compose up
```

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
