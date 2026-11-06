# edge: Cloudflare Tunnel (P1.01, D-027). The official image, pinned by digest; it runs as a non-root user.
# The tunnel token is a Docker secret, read through TUNNEL_TOKEN_FILE (set in compose).
# 2026.10.7 is newer than the two-week rule: taken early because 2026.10.3 failed the release scan (D-056).
FROM cloudflare/cloudflared:2026.10.7@sha256:4955524aeaa379e154023d5a027c250f7b35041a2e7e950cf13f144bc9b9c5ba
# Native health check: cloudflared asks its own metrics server whether the tunnel has a live connection.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3     CMD ["cloudflared", "tunnel", "--metrics", "127.0.0.1:20241", "ready"]
CMD ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241", "run"]
