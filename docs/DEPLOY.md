# Running the hosted service (storage.cloudfronts.group)

This is the plan for the cloudfronts host (192.168.0.15, `ns3`): `cfds-server` in
multi-user mode behind nginx, with sign-in through `https://oidc.cloudfronts.group`.
Nothing here has been applied yet; each step is to be confirmed before it is run.

```
VS Code / iOS / Python ──HTTPS──> nginx (storage.cloudfronts.group:443)
                                     └──HTTP──> cfds-server 127.0.0.1:8765
                                                  ├─ /home            -> /mnt/TMP/cfds-users/<user>/  (100 GiB each)
                                                  └─ /kaggle-datasets -> /mnt/TMP/ai-rig/kaggle-datasets (read-only)
cfds-server ──HTTPS──> oidc.cloudfronts.group (signing keys, userinfo)
```

## 1. Register the three apps at the sign-in provider

`oidc.cloudfronts.group` looks like Spring Authorization Server. Each app is a
**public client**: no secret, PKCE required, grant type `authorization_code`, scopes
`openid profile email`, access tokens as JWTs (the default "self-contained" format).

| client_id | Redirect URIs |
| --- | --- |
| `cfds-vscode` | `vscode://cloudfronts.cfds/auth`, `vscode-insiders://cloudfronts.cfds/auth` |
| `cfds-ios` | `groups.cloudfronts.datascientist:/oauth2/callback` |
| `cfds-cli` | `http://127.0.0.1/callback` (Spring Authorization Server accepts any port on a loopback redirect, per RFC 8252) |

If the provider is configured through `application.yml`, the entries look like this
(repeat for each client, changing `client-id` and `redirect-uris`):

```yaml
spring:
  security:
    oauth2:
      authorizationserver:
        client:
          cfds-cli:
            registration:
              client-id: cfds-cli
              client-authentication-methods: [none]
              authorization-grant-types: [authorization_code]
              redirect-uris: [http://127.0.0.1/callback]
              scopes: [openid, profile, email]
            require-proof-key: true
            require-authorization-consent: false
            token:
              access-token-time-to-live: 8h
```

Spring Authorization Server does not issue refresh tokens to public clients, so the
access-token lifetime decides how often people sign in again (8 hours above). All
three clients handle an expired token by asking the user to sign in again.

## 2. Install the server

```bash
python3 -m venv /opt/cfds
/opt/cfds/bin/pip install "cf-datascience-fs[server] @ git+https://github.com/sweeden-ttu/cf-datascience-fs@main"
useradd --system --home-dir /var/lib/cfds --shell /usr/sbin/nologin cfds
install -d -o cfds -g cfds -m 700 /mnt/TMP/cfds-users
# cfds must be able to read the datasets, and nothing more there:
sudo -u cfds ls /mnt/TMP/ai-rig/kaggle-datasets
```

`/etc/systemd/system/cfds-server.service`:

```ini
[Unit]
Description=cfds storage server
After=network-online.target
Wants=network-online.target

[Service]
User=cfds
Group=cfds
ExecStart=/opt/cfds/bin/cfds-server \
    --users-root /mnt/TMP/cfds-users \
    --datasets-root /mnt/TMP/ai-rig/kaggle-datasets \
    --oidc-issuer https://oidc.cloudfronts.group \
    --public-url https://storage.cloudfronts.group \
    --host 127.0.0.1 --port 8765
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/mnt/TMP/cfds-users
ReadOnlyPaths=/mnt/TMP/ai-rig/kaggle-datasets

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now cfds-server
journalctl -u cfds-server -f
```

Useful options: `--default-quota 100G` (the free tier, already the default),
`--datasets-writer-role curator` (people whose token has that role may add datasets),
`--cors-origin` (defaults to vscode.dev and its CDN). One user's quota can be changed
in `/mnt/TMP/cfds-users/.cfds/users.sqlite3`:

```bash
sqlite3 /mnt/TMP/cfds-users/.cfds/users.sqlite3 \
  "UPDATE users SET quota_limit = 500*1024*1024*1024 WHERE sub = 'someone'"
```

## 3. nginx

```nginx
server {
    listen 443 ssl http2;
    server_name storage.cloudfronts.group;
    ssl_certificate     /etc/letsencrypt/live/storage.cloudfronts.group/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/storage.cloudfronts.group/privkey.pem;

    client_max_body_size 0;        # the server enforces each user's quota
    proxy_request_buffering off;   # stream uploads instead of spooling them in nginx
    proxy_buffering off;           # stream downloads
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
    }
}
```

`storage.cloudfronts.group` already resolves (it answered 404 on 2026-11-20), so only
the site and a certificate covering the name are needed.

## 4. datascientist.cloudfronts.group

The sign-up and sign-in site. `https://datascientist.cloudfronts.group/login` is to
lead to the sign-in provider, in both the nginx and the Apache configuration on ns3.
The provider's issuer stays `https://oidc.cloudfronts.group`: tokens name it in `iss`,
and its pages and cookies belong to that host. So `/login` sends the browser there
(a redirect), rather than serving the provider's pages under another name:

```nginx
server {
    listen 443 ssl http2;
    server_name datascientist.cloudfronts.group;
    ssl_certificate     /etc/letsencrypt/live/datascientist.cloudfronts.group/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/datascientist.cloudfronts.group/privkey.pem;
    location = /login { return 302 https://oidc.cloudfronts.group/login; }
}
```

```apache
<VirtualHost *:443>
    ServerName datascientist.cloudfronts.group
    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/datascientist.cloudfronts.group/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/datascientist.cloudfronts.group/privkey.pem
    Redirect 302 /login https://oidc.cloudfronts.group/login
</VirtualHost>
```

Which of the two actually answers on port 443 (and whether one proxies to the other)
is to be checked on ns3 first.

DNS for `cloudfronts.group` is at Porkbun. The new name needs an `A` record (and an
`AAAA` record if the host has public IPv6) pointing at the public address that
forwards to ns3, or a `CNAME` to a name that already does, such as
`oidc.cloudfronts.group`. With the Porkbun API (v3; key and secret from the account):

```bash
curl -s https://api.porkbun.com/api/json/v3/dns/create/cloudfronts.group \
  -H 'Content-Type: application/json' \
  -d '{"apikey":"...","secretapikey":"...","name":"datascientist","type":"CNAME",
       "content":"oidc.cloudfronts.group","ttl":"600"}'
```

## 5. Check it

```bash
curl -s https://storage.cloudfronts.group/.well-known/oauth-protected-resource
curl -si https://storage.cloudfronts.group/v1/me          # 401 with WWW-Authenticate
pip install "cf-datascience-fs @ git+https://github.com/sweeden-ttu/cf-datascience-fs@main"
cfds login https://storage.cloudfronts.group               # opens the browser
cfds whoami
python -c "import fsspec; print(fsspec.filesystem('cfdss', host='storage.cloudfronts.group').ls('/kaggle-datasets'))"
```
