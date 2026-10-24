# edge

Cloudflare Tunnel: the only way in from the internet (D-027). The official `cloudflared` image, pinned by digest, running as a non-root user.

- **Token:** the `tunnel_token` Docker secret, read through `TUNNEL_TOKEN_FILE`. Created by the owner in the Cloudflare dashboard (C1.2).
- **Health:** native, `cloudflared tunnel ready` against its own metrics server on 127.0.0.1:20241.
- **Dev:** not started by default; `docker compose -f deploy/compose.dev.yaml --profile edge up` with a real token.
