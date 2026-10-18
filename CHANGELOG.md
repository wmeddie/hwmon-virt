# Changelog

## Unreleased

Initial release candidate, version `0.1.0`:

- Pull-based host temperature forwarding over a persistent virtio-serial channel.
- Native guest hwmon device with 1–16 temperature channels and a configurable
  in-memory cache, exposed to ordinary sensor applications.
- Host sensor selection by chip/label or explicit input path.
- Guest DKMS and Proxmox host installers with dry runs and configuration preservation.
- Guest and per-VM host uninstallers with dry runs and optional configuration removal.
- Prompt disconnect errors, bounded requests, automatic reconnection, and one
  warning per outage.
- Protocol error filtering, sensor-name validation, and timeout-boundary handling.
- Userspace, installer/uninstaller, real DKMS, and disposable QEMU regression tests.
- Upgrade documentation and `--version` reporting.
