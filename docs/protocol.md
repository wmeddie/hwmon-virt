# Protocol and implementation

## Reading a temperature

An application reads a guest hwmon `tempN_input` attribute. The guest kernel
module asks a userspace bridge for a reading, and the bridge requests it from
the host over a persistent virtio-serial channel. The host reads its configured
sensor and returns the value. A 100 ms in-memory cache shares recent readings
between applications by default.

```text
Resources / sensors / other hwmon readers
                 │ read tempN_input
                 ▼
     k10temp_virt.ko ── 100 ms per-channel cache
                 │ request/reply through /dev/k10temp-virt
                 ▼
        guest Python service
                 │ dedicated virtio-serial port
                 ▼
        host Python service
                 │ read a configured hwmon temperature
                 ▼
          host's sensor driver
```

The services block waiting for requests when idle. Temperature collection is
triggered by guest reads, and each request uses the existing connection without
starting a subprocess. Disconnected services retry their connection every two
seconds. Each service logs one warning per outage; repeated failures are logged
at debug level, enabled with `--verbose`. Successful communication resets this
suppression and logs recovery.

The module exports 1–16 temperature channels in host source order. Values use
signed integer millidegrees Celsius. Host hardware or its driver may itself
cache readings; requests do not force a new hardware conversion. The module
provides read-only temperatures, without fan control or thermal zones.

## Message format and request handling

`protocol.h` defines a 24-byte, little-endian message shared by the module,
bridge, and host. It contains the `KTV1` magic/version, a 64-bit request ID, a
zero-based channel, a signed status, and a signed millidegree value. Requests
have zero status/value. Replies echo the ID/channel and carry zero status or
`-EIO`, `-ENODATA`, `-EPROTO`, `-ETIMEDOUT`, or `-ENOTCONN`. Other negative statuses
in the Linux errno range (−4095 through −1) become `-EIO` at both the guest bridge
and kernel boundary, preventing kernel-internal restart codes from reaching
applications. Positive statuses and values below −4095 are malformed.
Accepted temperatures range from −273150 to 1000000 m°C.

The root-only `/dev/k10temp-virt` accepts exactly one bridge opener. It supports
blocking/nonblocking reads and poll; each read yields one complete request and
each write must contain one complete response. Replies are accepted only for
the currently pending, delivered request, otherwise writes return `ESTALE`.
Each module load seeds its request counter randomly to avoid matching old
transport data after a reload. Bridge replies use a separate lock from hwmon
reads, so a waiting hwmon callback cannot deadlock its responder. A completed
reply observed under the state lock wins over an expired wait; a wait interrupted
by a signal retains its interruption result.

The Python stream layer uses `poll` to detect hangups even when a disconnected
virtio port is not writable, while draining readable data before reporting EOF.
It handles fragmented/coalesced frames and discards replies with mismatched
IDs/channels. After a torn frame or reconnect, a request may fail and subsequent
reads recover; no stale-data fallback is used. The wire format is a small
project-specific protocol, not a standard VirtIO sensor device.

## References

- [Linux hwmon API](https://docs.kernel.org/hwmon/hwmon-kernel-api.html)
- [Linux hwmon sysfs interface](https://docs.kernel.org/hwmon/sysfs-interface.html)
- [QEMU character devices](https://www.qemu.org/docs/master/system/invocation.html#character-device-options)
- [Proxmox VM configuration](https://pve.proxmox.com/pve-docs/qm.conf.5.html)
- [Proxmox qm commands](https://pve.proxmox.com/pve-docs/qm.1.html)
- [Resources 1.10.2 CPU sensor selection](https://github.com/nokyan/resources/blob/v1.10.2/src/utils/cpu.rs)
