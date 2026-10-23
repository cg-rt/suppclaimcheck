#!/usr/bin/env bash
# C0.5 part 2 on the production VM: system updates, automatic security updates, firewall (SSH only),
# and Docker Engine + Compose from Docker's own apt repository.
# Run on the VM with: sudo bash 03-docker-firewall-updates.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

step() { printf '\n=== %s ===\n' "$1"; }

step "1/5 Bring the system up to date"
apt-get update -q
apt-get full-upgrade -y -q

step "2/5 Automatic security updates (unattended-upgrades)"
apt-get install -y -q unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades
echo "unattended-upgrades: $(systemctl is-enabled unattended-upgrades), $(systemctl is-active unattended-upgrades)"

step "3/5 Firewall: allow SSH in, block every other incoming connection"
apt-get install -y -q ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status verbose

step "4/5 Docker's apt repository, with its signing key checked"
apt-get install -y -q ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
# Docker's published key fingerprint (docs.docker.com/engine/install/ubuntu). Stop if the download differs.
expected="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
actual="$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '/^fpr:/{print $10; exit}')"
if [ "$actual" != "$expected" ]; then
  echo "STOP: Docker signing key fingerprint is ${actual}, expected ${expected}."
  rm -f /etc/apt/keyrings/docker.asc
  exit 1
fi
echo "Docker signing key verified: ${actual}"
codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${codename}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update -q

step "5/5 Docker Engine and the Compose plugin"
# No buildx: this server never builds images, it only runs the ones CI publishes (D-022).
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker containerd
docker run --rm hello-world

step "Summary"
echo "Ubuntu:         $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "Docker Engine:  $(docker version --format '{{.Server.Version}}')"
echo "Compose:        $(docker compose version --short)"
echo "Docker on boot: $(systemctl is-enabled docker)"
echo "Firewall:       $(ufw status | head -1)"
echo "Auto updates:   $(systemctl is-enabled unattended-upgrades)"
if [ -f /var/run/reboot-required ]; then
  echo "NOTE: the updates need a reboot. Run: sudo reboot"
fi
