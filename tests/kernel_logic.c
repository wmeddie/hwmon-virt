// SPDX-License-Identifier: GPL-2.0-only
/* Exercise the exact completion selector used under the module's state lock. */
#include <assert.h>
#include <stdbool.h>

#include "../hwmon_virt_internal.h"

int main(void)
{
	/* Reply accepted after the wait expired, before the reader reacquired the lock. */
	assert(hmv_wait_result(0, true, 0) == 0);
	assert(hmv_wait_result(0, true, -ENODATA) == -ENODATA);
	assert(hmv_wait_result(0, true, -ENOTCONN) == -ENOTCONN);
	assert(hmv_wait_result(0, false, 0) == -ETIMEDOUT);
	assert(hmv_wait_result(1, true, 0) == 0);
	/* A wait's own interruption result must survive even a racing completion. */
	assert(hmv_wait_result(-512, true, 0) == -512);
	assert(hmv_wait_result(-512, false, 0) == -512);
	assert(hmv_wait_result(-EINTR, true, 0) == -EINTR);
	return 0;
}
