-- Settings → Logins keeps no username or password any more: a person signs in
-- once through "Sign in" (live_signin.py) and only the browser session is
-- kept, refreshed by session_keepalive.py. A row is just the website, its
-- optional sign-in page and that session. Clears the ones already saved.
-- Safe to run more than once.

alter table site_logins alter column username drop not null;
alter table site_logins alter column password_enc drop not null;
update site_logins set username = null, password_enc = null
  where username is not null or password_enc is not null;
