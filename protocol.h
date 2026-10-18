/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef K10TEMP_VIRT_PROTOCOL_H
#define K10TEMP_VIRT_PROTOCOL_H

#include <linux/types.h>

#define KTV_MAGIC 0x3156544b /* ASCII KTV1 */
#define KTV_MIN_TEMP (-273150)
#define KTV_MAX_TEMP 1000000

/* Exactly 24 bytes, little endian, identical on 32/64-bit systems.
 * A request has zero status/value. A reply echoes the ID and channel;
 * status is 0 or a negative Linux errno; value is signed millidegrees C.
 */
struct ktv_message {
	__le32 magic;
	__le32 id_lo;
	__le32 id_hi;
	__le32 channel;
	__le32 status;
	__le32 value;
};

#endif
