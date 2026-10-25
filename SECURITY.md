# Security policy

PlayNow is a small, self-hosted private video library. It uses a shared viewer
password, a separate administrator password, and revocable links to individual
videos. It is intended for people who trust their administrator and server
operator. It is not a multi-tenant hosting service or a DRM system.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/olcovecyy/playnow/security/advisories/new)
for this repository. If that option is unavailable, arrange a private channel
with a maintainer before sending a sensitive reproduction. Do not put active share links,
passwords, environment files, cookies, databases, or private media in a public
issue. Include the affected commit or release, deployment topology, impact, and
a minimal reproduction using disposable data.

The latest default-branch code is the maintenance target. Backports and response
times are not guaranteed. Automated tests are included; they do not constitute
an independent penetration test or a guarantee that a deployment is secure.

## Access boundaries

- Password hashes use Werkzeug scrypt. The service refuses to start without
  both password hashes and a sufficiently long signing secret.
- Authorization is checked on the server for the catalog, original videos,
  compatible copies, posters, and every management operation. Uploading,
  editing, renaming, deleting, and managing share links require administrator
  access. CSRF tokens and origin checks protect mutations, including login and
  link redemption.
- A share link is a bearer secret: anyone who receives or forwards it can view
  its one video until the link is revoked. Only a token hash is stored in the
  database. The URL places the token in its fragment, and the frontend removes
  it from the address before redeeming it. Scoped sessions are revalidated
  against the share and video on subsequent requests.
- Revoking a share also invalidates its scoped sessions. Expired sessions must
  authenticate again. Revocation does not erase a downloaded copy or stop bytes
  already being delivered by a previously authorized response.
- Full viewer and administrator sessions remain full sessions when opening a
  valid share link. Changing a password hash alone does **not** invalidate
  existing full sessions. To revoke them, stop the web service, back up the
  database, and delete the intended rows from the `sessions` table before
  restarting. Rotating the signing secret invalidates existing cookies too.

## Deployment requirements

Use HTTPS for remote access. Set `PLAYNOW_PUBLIC_URL` to the exact public origin
and `PLAYNOW_COOKIE_SECURE=1` behind TLS. Keep the application port private to the
trusted reverse proxy. Leave `PLAYNOW_TRUSTED_PROXY_COUNT=0` unless the configured
number of trusted, controlled proxy hops actually exists; the edge proxy must
replace client-supplied forwarding headers.

Keep environment files, SQLite, posters, locks, partial uploads, and media out of
the web server's static root and out of Git. Do not create an unauthenticated
Nginx alias for the media directories. Use a dedicated writable media mount;
the configured storage marker prevents an absent mount from silently selecting
another location. The server account and anyone with filesystem or Docker
access can read or change videos and application state, regardless of browser
passwords. Restrict that access and keep verified backups.

Only trusted administrators should upload files. FFmpeg processes attacker-
controlled formats if an administrator accepts an untrusted video. Keep Python,
Gunicorn, Flask, FFmpeg, the container base image, and the host patched. Rebuild
and retest when updating a pinned image digest or dependency version. Run the
worker with resource limits and without network access where supported.

The service includes login throttling and a disk reserve. Add deployment-level
request and connection limits appropriate to the server; these controls are
not protection against all denial-of-service attacks. The sampled upload
fingerprint is a resume aid, not a full-file integrity signature.
