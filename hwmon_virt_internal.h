/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HWMON_VIRT_INTERNAL_H
#define HWMON_VIRT_INTERNAL_H

#include <linux/errno.h>
#include <linux/types.h>

/* Called with the state lock held, after the wait has returned. */
static inline int hmv_wait_result(long waited, bool done, int status)
{
	if (waited < 0)
		return waited;
	return done ? status : -ETIMEDOUT;
}

/* Never let a peer supply kernel-internal restart or asynchronous-I/O codes. */
static inline int hmv_reply_status(int status)
{
	switch (status) {
	case 0:
	case -EIO:
	case -ENODATA:
	case -EPROTO:
	case -ETIMEDOUT:
	case -ENOTCONN:
		return status;
	default:
		return -EIO;
	}
}

#endif
