# Noyra setup modes

`noyra setup` configures an already installed Ubuntu release. It keeps the Noyra
listener on `127.0.0.1:8765`; a proxy or tunnel is the only public entry point.
Run the command as root on the host that owns `/etc/noyra` and the systemd
units. Use `--dry-run` first to validate inputs and show planned changes without
writing files, restarting services, or enabling units.

## Prerequisites

- Ubuntu 24.04 (or a supported Ubuntu release), an installed Noyra release, and
  a working `noyra` console script.
- `/etc/noyra/noyra.env` exists and has `NOYRA_HOST=127.0.0.1` and
  `NOYRA_PORT=8765`.
- The service account can read its protected credential files. Keep the env file
  and generated proxy configuration root-owned.
- For public HTTPS, both hostnames resolve to this server before setup. Open
  ports 80 and 443 to the proxy and keep port 8765 private.

Credentials belong in protected files or hidden prompts. Do not put operator,
provider, or Cloudflare tokens in shell arguments, ordinary environment values,
chat, or source control. A Cloudflare token file must be private to root (mode
`0600`); the setup command never prints its contents.

## Local mode

Local mode leaves the service private and writes no public proxy configuration:

```bash
sudo noyra setup --mode local --dry-run
sudo noyra setup --mode local
curl --fail http://127.0.0.1:8765/health
```

Use an SSH tunnel for remote administration or a private browser session:

```bash
ssh -N -L 8765:127.0.0.1:8765 operator@server.example.com
```

Open `http://127.0.0.1:8765` on the client. The SSH session is the access
boundary; do not bind Noyra to a public interface to avoid the tunnel.

## Public HTTPS mode

Public mode writes the generated Caddy configuration, updates the known public
and admin origin settings, and reloads Caddy. DNS A/AAAA records for both names
must point to this host and Caddy must be able to obtain certificates from the
public ACME service:

```bash
sudo noyra setup --mode public \
  --public-domain archive.example.com \
  --admin-domain admin.example.com \
  --dry-run
sudo noyra setup --mode public \
  --public-domain archive.example.com \
  --admin-domain admin.example.com
```

The generated proxy forwards both hostnames to `127.0.0.1:8765`, sets the
trusted proxy and secure-cookie values, and preserves the admin authentication
boundary. If an existing Caddyfile would be replaced, pass `--replace` only
after saving a reviewable backup. Nginx remains supported through
`deploy/nginx/noyra.conf.example`; adapt that template manually when Nginx is
the selected proxy.

## Cloudflare Tunnel mode

Create the Tunnel and its public hostnames in the Cloudflare dashboard first.
Add `archive.example.com` and `admin.example.com` as Public Hostnames, route
both to `http://127.0.0.1:8765`, and ensure the zone DNS is active. Copy the
Tunnel token to a root-owned file, or let the command request it with a hidden
prompt:

```bash
sudo install -d -o root -g root -m 0700 /etc/noyra/credentials
sudo install -o root -g root -m 0600 /dev/null \
  /etc/noyra/credentials/cloudflare-tunnel-token
sudoedit /etc/noyra/credentials/cloudflare-tunnel-token

sudo noyra setup --mode cloudflare \
  --public-domain archive.example.com \
  --admin-domain admin.example.com \
  --tunnel-token-file /etc/noyra/credentials/cloudflare-tunnel-token \
  --dry-run
sudo noyra setup --mode cloudflare \
  --public-domain archive.example.com \
  --admin-domain admin.example.com \
  --tunnel-token-file /etc/noyra/credentials/cloudflare-tunnel-token
```

The setup writes `cloudflared-noyra.service` using the protected
`cloudflare-tunnel-token` credential, enables the connector, and checks local
and external readiness. The token is never copied into `noyra.env` or included
in command output. A hidden prompt is equivalent when `--tunnel-token-file` is
omitted; use a file for repeatable, audited operations.

## Failure and rollback

Setup validates DNS, paths, listener settings, and required binaries before
mutation. If a post-change health check fails, it restores the previous env,
proxy, credential, and systemd state. Inspect `systemctl status noyra` and
`journalctl -u noyra -u cloudflared-noyra` before retrying. Keep the generated
backup under `/var/backups/noyra` and do not delete it until the deployment is
verified.

For a failed release, select the previous code release and verify readiness:

```bash
sudo ./scripts/install-ubuntu.sh --rollback
sudo systemctl status noyra
curl --fail http://127.0.0.1:8765/health/ready
```

Code rollback does not reverse database migrations. If the new release changed
the schema, stop the service and restore the pre-update encrypted backup before
starting the previous release. When the network path is unavailable, use the
SSH tunnel above to reach the loopback health endpoint and admin surface while
you repair DNS, Caddy, or the Cloudflare connector.
