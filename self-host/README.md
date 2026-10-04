# Self-hosting Bid Scout

Full setup, in order. Run everything below on the Docker server itself.

**Using OMV's Compose plugin instead of a shell?** Each `docker-compose.yml`
below becomes one OMV "Compose" project: paste the file's contents into the
project's YAML box, and paste the contents of the matching `.env` file
(after filling in secrets) into its environment-variables box — OMV writes
that as a `.env` file alongside the compose file either way, so
`env_file: .env` in these files works the same regardless of which way you
created it. Steps like `docker compose exec ...` still need a shell (SSH, or
OMV's own "Terminal" page) since there's no GUI equivalent for running a
one-off command inside a container.

## 1. Local Supabase

Follow `supabase/README.md` in this folder. End state: Postgres + the API
gateway + Storage running on `homelab-network`, reachable at
`https://supabase.westonb.com` (Studio UI + the API, both via the same
gateway container), `schema.sql` applied, the `scanned-documents` bucket
created.

## 2. Configure the app/scanner stack

```bash
cd self-host
cp .env.example .env
```

Fill in `.env`:
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — from step 1.
- `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` — see the root `README.md` for the
  tradeoffs.
- `FIRECRAWL_API_KEY` / `CREDENTIALS_KEY` — optional, see root `README.md`.
- `DISPLAY_SCHEDULE_UTC` — e.g. `07:00 UTC`. This is now the *actual*
  schedule the scanner container runs on (previously just cosmetic text,
  since the real schedule lived in `daily-scan.yml`'s cron — that workflow
  is gone now).

`SCANNER_URL`/`SCANNER_PORT` are already filled in to match the compose
file's service names — no need to touch those unless you change them.

## 3. Let GitHub build the images

Push to `main`. `.github/workflows/docker-publish.yml` builds the `app` and
`scanner` images and pushes them to
`ghcr.io/<owner>/<repo>-app`/`-scanner`. Check the repo's **Actions** tab for
the first run.

If the GHCR packages are private (default for a private repo), you'll need
a GitHub PAT with `read:packages` to pull them from the server:

```bash
echo "$GITHUB_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin
```

Run this as root (OMV's Terminal page runs as root by default) — Watchtower's
`docker-compose.yml` mounts `/root/.docker/config.json` specifically, so it
can reuse the same login.

## 4. Start the app/scanner stack

```bash
docker compose -f self-host/docker-compose.yml pull
docker compose -f self-host/docker-compose.yml up -d
```

## 5. Traefik / Cloudflare Tunnel

Already wired up in `docker-compose.yml`'s `app` service labels, matching
the same pattern as your other stacks (e.g. lubelogger): http→https
redirect, `websecure` router with the `cloudflare` certresolver, routing
`bidscout.westonb.com` → container port 8080. Nothing extra to configure
here as long as your Cloudflare Tunnel's ingress already forwards
`bidscout.westonb.com` to Traefik the same way it does for your other
`*.westonb.com` hosts.

## 6. Hostnames in use

- `bidscout.westonb.com` → the `app` container (the dashboard).
- `supabase.westonb.com` → Supabase's `api-gw` container (Studio UI + API).
- No hostname for Watchtower — it has no web UI, it's a silent poller
  (`--interval 60 --cleanup`, no HTTP API enabled).

## 7. Verify

- Open `https://bidscout.westonb.com` — you should see the dashboard.
- Create a division, add a site and a keyword.
- Click **"Run scan now"** — `docker compose logs -f scanner` should show
  `scraper.py` starting; the dashboard's Status card should move to
  "running" then "success".
- Click **"Cancel scan"** mid-run on a different division and confirm the
  scanner logs show the subprocess being killed.

## 8. Confirm auto-update works

Push a trivial commit to `main`. Once `docker-publish.yml` finishes (check
the Actions tab), wait up to 60s (Watchtower's poll interval) and run
`docker compose -f self-host/docker-compose.yml ps` on the server — the
`app`/`scanner` containers should show a recent "Up" time, meaning
Watchtower already restarted them on the new image.
