# edge: Cloudflare Tunnel (P1.01, D-027). The official image, pinned by digest; it runs as a non-root user.
# The tunnel token is a Docker secret, read through TUNNEL_TOKEN_FILE (set in compose).
FROM cloudflare/cloudflared:2026.10.0@sha256:9b49eed8f62806d5d45ddf59ecefb5710429598ea6d3fcccd2af938f621b2b07
# Native health check: cloudflared asks its own metrics server whether the tunnel has a live connection.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3     CMD ["cloudflared", "tunnel", "--metrics", "127.0.0.1:20241", "ready"]
CMD ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241", "run"]
