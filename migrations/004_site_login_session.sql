-- A site's own login keeps its browser session too, like site_logins'
-- session_enc: the cookies after the last good sign-in (by the scan worker,
-- or by a person through "Sign in yourself"), Fernet-encrypted with
-- CREDENTIALS_KEY. The next sign-in restores them and skips the form — and
-- with it checks like Bidtracer's Cloudflare one. Safe to run more than once.

alter table sites add column if not exists login_session_enc text;
