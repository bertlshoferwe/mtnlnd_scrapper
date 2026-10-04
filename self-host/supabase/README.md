# Local Supabase

`docker-compose.yml` here is Supabase's official self-hosted stack
(pulled from `supabase/supabase`, Envoy-gateway release — the API gateway
service is `api-gw`, aliased internally as both `envoy` and `kong`), with
two edits already applied so it drops straight into this homelab:

- Joins the shared `homelab-network` (bottom `networks:` block) instead of
  getting its own auto-created network, same as every other stack here.
- The gateway (`api-gw`) has Traefik labels routing `supabase.westonb.com`
  to it — same http→https-redirect + `cloudflare` certresolver pattern as
  `bidscout.westonb.com`. Since `api-gw` proxies `/` to Studio (behind the
  `DASHBOARD_USERNAME`/`PASSWORD` login) and `/rest/v1`, `/storage/v1`, etc.
  to the actual APIs, this one hostname gets you both.

Bid Scout itself only needs Postgres + the gateway + PostgREST + Storage —
no Auth/Realtime/Edge Functions/Analytics — but it's fine to leave the full
default stack running rather than trimming it down.

If Supabase releases a newer version of this stack later and you want to
pick it up, re-fetch `docker/docker-compose.yml` from
`github.com/supabase/supabase` and re-apply the same two edits (search this
file's git history for the exact diff if needed).

## 1. Secrets

```bash
cd self-host/supabase
cp .env.example .env
sh generate-keys.sh --update-env
```

This fills in `JWT_SECRET`, `ANON_KEY`, `SERVICE_ROLE_KEY`,
`SECRET_KEY_BASE`, `VAULT_ENC_KEY`, `PG_META_CRYPTO_KEY`, the Logflare/S3/
MinIO tokens, `POSTGRES_PASSWORD`, and `DASHBOARD_PASSWORD` — everything
`.env.example` ships with an insecure placeholder for. `DASHBOARD_USERNAME`
defaults to `supabase`; change it in `.env` if you want something else.

(If you're pasting into OMV's Compose plugin instead of running this on a
shell, run `sh generate-keys.sh` — without `--update-env` — on any machine
with `openssl` to just print the values, then paste them into OMV's
environment-variables box by hand.)

`SUPABASE_PUBLIC_URL`, `API_EXTERNAL_URL`, and `SITE_URL` are already set to
`https://supabase.westonb.com` in `.env.example` — nothing to change there.

Leave `SUPABASE_PUBLISHABLE_KEY`/`SUPABASE_SECRET_KEY`/`JWT_KEYS`/
`JWT_JWKS` blank — those are for the newer asymmetric-key auth scheme, which
this app doesn't use; the legacy `ANON_KEY`/`SERVICE_ROLE_KEY` above are all
it needs.

## 2. Start it

```bash
docker compose pull
docker compose up -d
```

Add `supabase.westonb.com` to Cloudflare Tunnel's ingress the same way you
did for `bidscout.westonb.com`.

## 3. Apply the schema

```bash
docker compose exec -T db psql -U postgres < ../../schema.sql
```

## 4. Create the Storage bucket

Open `https://supabase.westonb.com`, log in with `DASHBOARD_USERNAME`/
`DASHBOARD_PASSWORD`, then **Storage → New bucket** → name it
`scanned-documents` → leave **Public bucket** OFF.

## 5. Note the API URL for the app

The app/scanner containers are on the same `homelab-network`, so they reach
this stack by service name: `http://kong:8000` (the `kong` alias on
`api-gw`, port 8000 is `api-gw`'s `KONG_HTTP_PORT`/`API_GW_HTTP_PORT`
default). Put that in `self-host/.env` as `SUPABASE_URL`, and the
`SERVICE_ROLE_KEY` from step 1 as `SUPABASE_SERVICE_KEY`.
