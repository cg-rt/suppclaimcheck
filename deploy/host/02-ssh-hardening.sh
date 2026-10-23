#!/usr/bin/env bash
# C0.5 part 1 on the production VM: allow SSH key login only (no passwords, no direct root login).
# Run on the VM as the admin user, after key login works: sudo bash 02-ssh-hardening.sh
set -euo pipefail

user="${SUDO_USER:?run this with sudo from the admin account, not as root}"
keys="/home/${user}/.ssh/authorized_keys"
conf=/etc/ssh/sshd_config.d/10-hardening.conf

# Safety check: never turn passwords off unless a key is already installed, or we'd lock ourselves out.
if [ ! -s "$keys" ]; then
  echo "STOP: ${keys} is missing or empty, so key login isn't set up. Nothing changed."
  exit 1
fi
echo "Found $(grep -c . "$keys") key(s) for ${user}."

# 10- sorts before Ubuntu's 50-cloud-init.conf, and sshd keeps the first value it reads.
cat > "$conf" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
chmod 644 "$conf"
echo "Wrote ${conf}:"
cat "$conf"

# Check the full SSH configuration before applying it; set -e stops here on any error.
sshd -t
systemctl reload ssh
echo "SSH configuration is valid and was reloaded."

echo "Settings now in effect (all three should say no):"
sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
