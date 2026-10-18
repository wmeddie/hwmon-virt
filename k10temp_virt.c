// SPDX-License-Identifier: GPL-2.0-only
/* On-demand, userspace-backed temperature sensors. No hardware access here. */
#include <linux/fs.h>
#include <linux/hwmon.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/random.h>
#include <linux/uaccess.h>
#include <linux/wait.h>

#include "protocol.h"

#define MAX_CHANNELS 16

static char *sensor_name = "k10temp";
module_param(sensor_name, charp, 0444);
MODULE_PARM_DESC(sensor_name, "hwmon name (default k10temp for CPU app compatibility)");
static char *labels[MAX_CHANNELS] = { "Host CPU" };
static unsigned int num_labels = 1;
module_param_array(labels, charp, &num_labels, 0444);
MODULE_PARM_DESC(labels, "Temperature channel labels, in host export order (1-16)");
static unsigned int cache_ms = 100;
module_param(cache_ms, uint, 0444);
MODULE_PARM_DESC(cache_ms, "Per-channel cache lifetime in milliseconds (0-60000)");
static unsigned int timeout_ms = 1000;
module_param(timeout_ms, uint, 0444);
MODULE_PARM_DESC(timeout_ms, "Maximum wait for a bridge reply in milliseconds (1-10000)");

static DEFINE_MUTEX(request_lock);
static DEFINE_MUTEX(state_lock);
static DECLARE_WAIT_QUEUE_HEAD(request_wait);
static DECLARE_WAIT_QUEUE_HEAD(response_wait);

static struct {
	bool connected;
	bool pending;
	bool queued;
	bool done;
	u64 sequence;
	struct ktv_message request;
	int status;
	s32 value;
	struct {
		bool valid;
		u64 acquired_ns;
		s32 value;
	} cache[MAX_CHANNELS];
} state;

static struct device *hwmon_dev;

static void invalidate_cache(void)
{
	unsigned int i;

	for (i = 0; i < num_labels; i++)
		state.cache[i].valid = false;
}

static int sensor_read(struct device *dev, enum hwmon_sensor_types type,
		       u32 attr, int channel, long *value)
{
	u64 started = ktime_get_ns();
	long waited;
	int ret;

	if (type != hwmon_temp || attr != hwmon_temp_input)
		return -EOPNOTSUPP;
	ret = mutex_lock_interruptible(&request_lock);
	if (ret)
		return ret;
	mutex_lock(&state_lock);
	if (!state.connected) {
		ret = -ENODATA;
		goto unlock;
	}
	/* Recompute after waiting for a concurrent reader. */
	started = ktime_get_ns();
	if (state.cache[channel].valid && cache_ms &&
	    started - state.cache[channel].acquired_ns <
	    (u64)cache_ms * NSEC_PER_MSEC) {
		*value = state.cache[channel].value;
		ret = 0;
		goto unlock;
	}
	state.cache[channel].valid = false;
	state.sequence++;
	state.request = (struct ktv_message) {
		.magic = cpu_to_le32(KTV_MAGIC),
		.id_lo = cpu_to_le32(lower_32_bits(state.sequence)),
		.id_hi = cpu_to_le32(upper_32_bits(state.sequence)),
		.channel = cpu_to_le32(channel),
	};
	state.pending = true;
	state.queued = true;
	state.done = false;
	state.status = -ETIMEDOUT;
	mutex_unlock(&state_lock);
	wake_up_interruptible(&request_wait);

	waited = wait_event_interruptible_timeout(response_wait,
			READ_ONCE(state.done), msecs_to_jiffies(timeout_ms));
	mutex_lock(&state_lock);
	state.pending = false;
	state.queued = false;
	if (waited < 0)
		ret = waited;
	else if (!waited)
		ret = -ETIMEDOUT;
	else
		ret = state.status;
	if (!ret) {
		*value = state.value;
		state.cache[channel].value = state.value;
		/* Age from the request, not a possibly delayed response. */
		state.cache[channel].acquired_ns = started;
		state.cache[channel].valid = true;
	}
unlock:
	mutex_unlock(&state_lock);
	mutex_unlock(&request_lock);
	return ret;
}

static int sensor_read_string(struct device *dev, enum hwmon_sensor_types type,
			      u32 attr, int channel, const char **str)
{
	if (type != hwmon_temp || attr != hwmon_temp_label)
		return -EOPNOTSUPP;
	*str = labels[channel];
	return 0;
}

static umode_t sensor_visible(const void *data, enum hwmon_sensor_types type,
			     u32 attr, int channel)
{
	return type == hwmon_temp &&
	       (attr == hwmon_temp_input || attr == hwmon_temp_label) ? 0444 : 0;
}

static const struct hwmon_ops sensor_ops = {
	.is_visible = sensor_visible,
	.read = sensor_read,
	.read_string = sensor_read_string,
};

static u32 temperature_config[MAX_CHANNELS + 1];
static const struct hwmon_channel_info temperature_info = {
	.type = hwmon_temp,
	.config = temperature_config,
};
static const struct hwmon_channel_info * const sensor_info[] = {
	&temperature_info, NULL,
};
static const struct hwmon_chip_info sensor_chip = {
	.ops = &sensor_ops,
	.info = sensor_info,
};

static int bridge_open(struct inode *inode, struct file *file)
{
	int ret = 0;

	mutex_lock(&state_lock);
	if (state.connected) {
		ret = -EBUSY;
	} else {
		state.connected = true;
		invalidate_cache();
	}
	mutex_unlock(&state_lock);
	if (ret)
		return ret;
	return nonseekable_open(inode, file);
}

static int bridge_release(struct inode *inode, struct file *file)
{
	mutex_lock(&state_lock);
	state.connected = false;
	state.pending = false;
	state.queued = false;
	state.status = -ENOTCONN;
	state.done = true;
	invalidate_cache();
	mutex_unlock(&state_lock);
	wake_up_interruptible(&response_wait);
	wake_up_interruptible(&request_wait);
	return 0;
}

/* One complete message per read/write; there is only one bridge opener. */
static ssize_t bridge_read(struct file *file, char __user *buf, size_t count,
			   loff_t *pos)
{
	int ret;

	if (count < sizeof(struct ktv_message))
		return -EINVAL;
	for (;;) {
		mutex_lock(&state_lock);
		if (state.queued)
			break;
		mutex_unlock(&state_lock);
		if (file->f_flags & O_NONBLOCK)
			return -EAGAIN;
		ret = wait_event_interruptible(request_wait,
					       READ_ONCE(state.queued));
		if (ret)
			return ret;
	}
	if (copy_to_user(buf, &state.request, sizeof(state.request))) {
		ret = -EFAULT;
	} else {
		state.queued = false;
		ret = sizeof(state.request);
	}
	mutex_unlock(&state_lock);
	return ret;
}

static ssize_t bridge_write(struct file *file, const char __user *buf,
			    size_t count, loff_t *pos)
{
	struct ktv_message reply;
	s32 status, value;
	int ret = sizeof(reply);

	if (count != sizeof(reply))
		return -EINVAL;
	if (copy_from_user(&reply, buf, sizeof(reply)))
		return -EFAULT;
	status = (s32)le32_to_cpu(reply.status);
	value = (s32)le32_to_cpu(reply.value);
	if (le32_to_cpu(reply.magic) != KTV_MAGIC ||
	    status > 0 || status < -4095 ||
	    (!status && (value < KTV_MIN_TEMP || value > KTV_MAX_TEMP)))
		return -EINVAL;

	/* Never take the hwmon/request mutex: its reader is waiting for us. */
	mutex_lock(&state_lock);
	if (!state.pending || state.queued || state.done ||
	    reply.id_lo != state.request.id_lo ||
	    reply.id_hi != state.request.id_hi ||
	    reply.channel != state.request.channel) {
		ret = -ESTALE;
	} else {
		state.status = status;
		state.value = value;
		state.done = true;
	}
	mutex_unlock(&state_lock);
	if (ret > 0)
		wake_up_interruptible(&response_wait);
	return ret;
}

static __poll_t bridge_poll(struct file *file, poll_table *wait)
{
	__poll_t mask = EPOLLOUT | EPOLLWRNORM;

	poll_wait(file, &request_wait, wait);
	mutex_lock(&state_lock);
	if (state.queued)
		mask |= EPOLLIN | EPOLLRDNORM;
	mutex_unlock(&state_lock);
	return mask;
}

static const struct file_operations bridge_ops = {
	.owner = THIS_MODULE,
	.open = bridge_open,
	.release = bridge_release,
	.read = bridge_read,
	.write = bridge_write,
	.poll = bridge_poll,
};
static struct miscdevice bridge_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "k10temp-virt",
	.fops = &bridge_ops,
	.mode = 0600,
};

static int __init k10temp_virt_init(void)
{
	unsigned int i;
	int ret;

	static_assert(sizeof(struct ktv_message) == 24);
	if (!num_labels || cache_ms > 60000 || !timeout_ms || timeout_ms > 10000)
		return -EINVAL;
	for (i = 0; i < num_labels; i++) {
		if (!labels[i][0] || strlen(labels[i]) > 64)
			return -EINVAL;
		temperature_config[i] = HWMON_T_INPUT | HWMON_T_LABEL;
	}
	/* Avoid matching replies left in the transport across a module reload. */
	state.sequence = get_random_u64();
	ret = misc_register(&bridge_device);
	if (ret)
		return ret;
	hwmon_dev = hwmon_device_register_with_info(bridge_device.this_device,
					sensor_name, NULL, &sensor_chip, NULL);
	if (IS_ERR(hwmon_dev)) {
		ret = PTR_ERR(hwmon_dev);
		misc_deregister(&bridge_device);
		return ret;
	}
	pr_info("k10temp_virt: %u temperature channel(s), cache %u ms\n",
		num_labels, cache_ms);
	return 0;
}

static void __exit k10temp_virt_exit(void)
{
	/* An open bridge pins this module through bridge_ops.owner. */
	hwmon_device_unregister(hwmon_dev);
	misc_deregister(&bridge_device);
}

module_init(k10temp_virt_init);
module_exit(k10temp_virt_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("On-demand virtual hwmon temperatures over a userspace bridge");
MODULE_VERSION("0.1.0");
