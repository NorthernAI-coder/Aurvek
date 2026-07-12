# Hardened Linux nginx blocklist reload

Production keeps the web process unprivileged. Aurvek writes a staging file at
`/opt/aurvek-data/nginx_blocklist.conf` with
`NGINX_BLOCKLIST_RELOAD_MODE=external`; it never invokes nginx or sudo.

`aurvek-nginx-blocklist-sync.path` watches the staging file's parent directory
so atomic file replacement is detected reliably. A two-minute timer provides a
retry/acknowledgement fallback if an event is missed or a reload fails. The
root-owned oneshot helper rejects symlinks, oversized input, non-IP directives,
private IPs, and malformed lines. It writes a canonical root-owned include at
`/etc/nginx/aurvek/blocklist.map`, validates the complete nginx configuration,
and only then reloads nginx. A failed validation restores the last known-good
include.

The active HTTP-context map must contain this include inside the map block:

```nginx
geo $is_blocked_ip {
    default 0;
    include /etc/nginx/aurvek/blocklist.map;
}
```

Each protected server block must also enforce the result:

```nginx
if ($is_blocked_ip) {
    return 444;
}
```

Install the Python helper as
`/usr/local/libexec/aurvek-nginx-blocklist-sync` owned by `root:root` with mode
`0755`. Before starting either service, create `/opt/aurvek-data` as
`aurvek:aurvek` mode `0755` and `/etc/nginx/aurvek` as `root:root` mode
`0755`; the systemd sandbox requires both paths to exist. Create the initial
staging file as `aurvek:aurvek` mode `0664` and the sanitized
`/etc/nginx/aurvek/blocklist.map` as `root:root` mode `0644`.

Install the service, path, and timer units in `/etc/systemd/system/`, run
`systemctl daemon-reload`, then enable and start both the path and timer units.
Run the service once manually to promote the initial reconciled list.

Install `aurvek-nginx-blocklist.override.conf` as
`/etc/systemd/system/aurvek.service.d/nginx-blocklist.conf`. This keeps the
Linux-only staging path and external reload mode outside the application
checkout and its `.env` file.

Do not execute the helper from `/opt/aurvek`: that tree is writable by the
application account and must never be a root execution source.
