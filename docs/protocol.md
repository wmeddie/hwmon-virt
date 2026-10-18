# Protocol and implementation


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

