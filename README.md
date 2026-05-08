# Home Assistant Admin Panels Addon

A lightweight Home Assistant add-on that puts a launcher in your sidebar for arbitrary
admin web UIs (router, NAS, IoT bridges, printers...). Each panel is reverse-proxied
through HA ingress, so you can reach HTTP-only LAN devices over HTTPS — locally, via
Nabu Casa, or through any external reverse proxy in front of HA.

```
┌──────────────────────────┐         ┌─────────────┐
│ Home Assistant (HTTPS)   │ ingress │ Admin Panels│
│  └─ /api/hassio_ingress/ ├────────▶│  launcher   │
│      <token>/proxy/N/    │         │  + proxy    │
└──────────────────────────┘         └──────┬──────┘
                                            │ httpx
                                            ▼
                                   ┌──────────────────┐
                                   │ http://router    │
                                   │ http://192.x.y.z │
                                   │ https://nas:5001 │
                                   └──────────────────┘
```

## Features

- **Sidebar entry** with a tile launcher — one tile per configured panel.
- **Live status badges** — periodic ping tells you which targets are reachable.
- **Reverse proxy** through HA ingress — works regardless of HTTP/HTTPS scheme on
  the upstream, no mixed-content issues, no port forwarding, no extra DNS.
- **Multi-tab friendly** — each tile opens the target in a new tab so you can
  keep several panels visible at once.
- **Lightweight** — Python 3.12 + FastAPI on Alpine, ~50 MB image.

## Installation

1. Add this repository to Home Assistant:
   - **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   - Paste: `https://github.com/WilliamFriconneau/ha-admin-panels`
2. Install **Admin Panels** from the store.
3. Open the **Configuration** tab and add your panels (see below).
4. Start the add-on, then click **Admin Panels** in the sidebar.

## Configuration

```yaml
panels:
  - name: Router
    url: http://router.lan
    icon: mdi:router-network
  - name: Bridge
    url: http://bridge.lan:8080
    icon: mdi:bridge
  - name: NAS
    url: https://nas.lan:5001
    icon: mdi:server
    verify_tls: false
ping_interval: 30
request_timeout: 5
```

### Options

| Key                 | Type    | Default | Description                                            |
| ------------------- | ------- | ------- | ------------------------------------------------------ |
| `panels[].name`     | string  | —       | Display name shown on the tile.                        |
| `panels[].url`      | url     | —       | Upstream base URL (scheme + host + optional port).     |
| `panels[].icon`     | string  | `mdi:web` | Material Design Icon identifier (e.g. `mdi:server`). |
| `panels[].verify_tls` | bool  | `false` | Set to `true` to enforce TLS cert verification (LAN admin UIs are usually self-signed, hence the relaxed default). |
| `ping_interval`     | int     | `30`    | Seconds between status refreshes (5–3600).             |
| `request_timeout`   | int     | `5`     | Per-ping HTTP timeout in seconds (1–60).               |

Browse icons at [pictogrammers.com/library/mdi](https://pictogrammers.com/library/mdi/).

## How the proxy works

The add-on serves a launcher page at its ingress root, plus a reverse-proxy mount at
`proxy/<index>/`. Clicking a tile opens the target in a new tab pointing at the proxy
path; the add-on streams the upstream response back through HA. For HTML/CSS responses
it rewrites absolute paths (`href`, `src`, `action`, `url(...)`) to keep them inside
the proxy scope, and injects a tiny script that intercepts `fetch` and `XMLHttpRequest`
to rewrite absolute paths in JS-driven calls.

> **Caveats**
>
> - **Reverse proxy is best-effort.** Pages relying on WebSockets, hard-coded
>   absolute URLs (`http://host/...`), CSRF tokens tied to the original origin,
>   or aggressive XHR-prototype caching (some legacy SPAs like OpenWrt LuCI,
>   Freebox AngularJS UI) may misbehave.
> - **Password managers (Bitwarden, 1Password...) won't autofill** on the
>   proxied URL because it is on `*.ui.nabu.casa` (or your HA hostname), not
>   on the original LAN IP. Workaround: add the proxy URL as an additional
>   URI on the corresponding vault entry, **or use the "Open direct" tile
>   button** when on the same LAN as the upstream device.
> - **WebSocket upgrades** are not proxied (HTTP only). The addon logs a
>   warning when this happens.
>
> Each tile has two icon buttons that appear on hover:
> - 🌐 *Open direct* — opens the upstream URL bypassing the proxy. Full SPA
>   functionality, password manager autofill works, but only when you can
>   reach the upstream IP from your browser (LAN, VPN, Tailscale).
> - ↗ *Open via proxy* — opens through the addon proxy. Works remotely via
>   Nabu Casa or any external HA URL.

## Development

```bash
git clone https://github.com/WilliamFriconneau/ha-admin-panels
cd ha-admin-panels/admin_panels

# Run locally without HA
ADMIN_PANELS_OPTIONS=$(pwd)/options.example.json \
    python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099
```

`options.example.json`:
```json
{
  "panels": [
    { "name": "Demo", "url": "http://example.com", "icon": "mdi:web" }
  ],
  "ping_interval": 30,
  "request_timeout": 5
}
```

Or build the addon image directly:
```bash
docker build \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20 \
  -t admin_panels:dev admin_panels/
```

## License

MIT — see [LICENSE](LICENSE).
