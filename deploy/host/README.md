# Production host setup

How to build the production VM from nothing: Hyper-V on the Windows host, Ubuntu Server inside it, then key-only SSH, a firewall, automatic security updates and Docker. The owner runs every step (AGENTS.md section 3). Agents may edit these files but never run them against the VM.

| File | Runs on | Does |
|---|---|---|
| `01-create-vm.ps1` | Windows, PowerShell as administrator | Creates the Hyper-V VM with the settings from D-007 |
| `02-ssh-hardening.sh` | The VM, with `sudo` | Turns off password and root login over SSH, once key login works |
| `03-docker-firewall-updates.sh` | The VM, with `sudo` | Updates, unattended-upgrades, ufw (SSH only), Docker Engine and Compose from Docker's repo |

Every script stops at the first error and changes nothing it can't check first.

## Order

### 1. Windows host

1. Turn on Hyper-V (PowerShell as administrator), then restart Windows:
   ```powershell
   Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All
   ```
2. Download the Ubuntu Server ISO from `releases.ubuntu.com/<version>/` and check it against that folder's `SHA256SUMS`:
   ```powershell
   (Get-FileHash D:\VMs\iso\ubuntu-26.04.1-live-server-amd64.iso -Algorithm SHA256).Hash.ToLower()
   ```
3. Create the VM:
   ```powershell
   .\deploy\host\01-create-vm.ps1 -IsoPath D:\VMs\iso\ubuntu-26.04.1-live-server-amd64.iso
   ```

### 2. Install Ubuntu Server

Start it with `Start-VM scc-prod; vmconnect.exe localhost scc-prod` and choose **Try or Install Ubuntu Server**. In the installer:

- **Type:** Ubuntu Server (not minimized). **Network:** `eth0` gets a DHCP address from the Default Switch.
- **Storage:** use the entire disk with LVM, **no encryption** (an encrypted disk waits for a passphrase at boot, so the VM could not start unattended). On the summary screen, edit `ubuntu-lv` to the maximum size; the default uses only part of the disk.
- **Profile:** server name `scc-prod`. **SSH:** install OpenSSH server; keep password login for now. **Snaps:** none.
- **At the end, choose Reboot Now first, and eject the ISO only when the installer asks.** The installer runs from the ISO until the reboot; ejecting earlier makes it loop on `SQUASHFS error: Failed to read block`. If that happens: `Stop-VM scc-prod -TurnOff`, then continue below.
- Boot from the disk from now on (VM off):
  ```powershell
  Set-VMFirmware -VMName scc-prod -FirstBootDevice (Get-VMHardDiskDrive -VMName scc-prod)
  Get-VMDvdDrive -VMName scc-prod | Set-VMDvdDrive -Path $null
  ```

### 3. SSH key login from Windows

The Default Switch gives the VM the name `scc-prod.mshome.net` on the host. Replace `<user>` with the admin user created in the installer.

1. Create a key, with a passphrase: `ssh-keygen -t ed25519 -C "scc-prod admin"`.
2. On the VM console, read the server's real fingerprint: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.
3. Accept the host key in a plain connection, comparing it with step 2 first: `ssh <user>@scc-prod.mshome.net exit`. Doing this inside a pipe fails with `Host key verification failed`, because the yes/no question can't read the keyboard.
4. Install the public key:
   ```powershell
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh <user>@scc-prod.mshome.net "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```
5. Add a short name to `~/.ssh/config`:
   ```
   Host scc-prod
     HostName scc-prod.mshome.net
     User <user>
     IdentityFile ~/.ssh/id_ed25519
   ```
   If `ssh scc-prod` then says `Bad owner or permissions`, the file has a permission entry for another account. Make it yours alone:
   ```powershell
   icacls "$env:USERPROFILE\.ssh\config" /grant:r "$($env:USERNAME):F"
   icacls "$env:USERPROFILE\.ssh\config" /inheritance:r
   ```

### 4. Harden the VM and install Docker

Copy each script and run it, rather than pasting commands into the SSH session; long pastes into a Windows console can lose characters.

```powershell
scp deploy/host/02-ssh-hardening.sh deploy/host/03-docker-firewall-updates.sh scc-prod:
ssh -t scc-prod "sudo bash 02-ssh-hardening.sh"
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password scc-prod
ssh -t scc-prod "sudo bash 03-docker-firewall-updates.sh"
```

- The password-only login must fail with `Permission denied (publickey)`.
- `03` ends with a summary: the Ubuntu, Docker Engine and Compose versions, Docker enabled on boot, the firewall active and automatic updates enabled. If it says a reboot is needed: `ssh -t scc-prod "sudo reboot"`.

### 5. Check

- From Windows, only port 22 answers: `Test-NetConnection scc-prod.mshome.net -Port 22` succeeds, any other port fails.
- After a Windows restart, the VM is running without anyone signing in (the reboot drill, RB-02).

## Notes

- **Docker and ufw:** Docker writes its own firewall rules for published container ports, and these bypass ufw. The production compose file publishes no ports; traffic arrives through the outbound Cloudflare tunnel (D-027).
- **No buildx:** the VM never builds images; it runs the versions CI publishes to GHCR (D-022).
- **Restarts:** unattended-upgrades installs security updates daily but does not restart the VM. Kernel updates take effect after a restart in the weekly ops check.
