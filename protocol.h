/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef HWMON_VIRT_PROTOCOL_H
#define HWMON_VIRT_PROTOCOL_H

#include <linux/types.h>

#define HMV_MAGIC 0x31564d48 /* ASCII HMV1 */
#define HMV_MIN_TEMP (-273150)
#define HMV_MAX_TEMP 1000000

/* Exactly 24 bytes, little endian, identical on 32/64-bit systems.
 * A request has zero status/value. A reply echoes the ID and channel;
 * status is 0, -EIO, -ENODATA, -EPROTO, -ETIMEDOUT, or -ENOTCONN;
 * other negative Linux errnos are mapped to -EIO by the receiver.
 * value is signed millidegrees C.
 */
struct hmv_message {
	__le32 magic;
	__le32 id_lo;
	__le32 id_hi;
	__le32 channel;
	__le32 status;
	__le32 value;
};

#endif
