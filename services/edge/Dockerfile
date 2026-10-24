# edge: Cloudflare Tunnel (P1.01, D-027). The official image, pinned by digest; it runs as a non-root user.
# The tunnel token is a Docker secret, read through TUNNEL_TOKEN_FILE (set in compose).
# 2026.10.3 is newer than the two-week rule: taken early for a certificate-validation fix (D-040).
FROM cloudflare/cloudflared:2026.10.3@sha256:faa8e0a1475f137c910be6025f43c8e4092fdd35461b31e19fd1390657a1ff39
# Native health check: cloudflared asks its own metrics server whether the tunnel has a live connection.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3     CMD ["cloudflared", "tunnel", "--metrics", "127.0.0.1:20241", "ready"]
CMD ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241", "run"]
