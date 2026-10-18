# Contributing to hwmon-virt

Bug reports and focused pull requests are welcome. Include the host and guest
distributions, guest kernel, Python version, project version or commit, source
selector, expected behavior, and the smallest reproduction you can provide.
Relevant service logs and compiler errors help; omit unrelated host details.

Run `make test` for userspace and installer changes. For kernel changes, also
build with `make` and run the disposable QEMU tests. Changes to staged module
files or build rules should pass the isolated DKMS test. See
[Building and testing](docs/testing.md) for prerequisites and commands.

Keep temperature reads on demand, keep the host's source allowlist explicit,
and preserve the existing wire format and installed names unless a change
includes a documented migration.

Describe the problem, the resulting behavior, and the checks you ran in pull
requests. Add regression coverage for bugs where it can distinguish the old
behavior from the fix. Documentation-only changes do not need new tests.

The project uses GPL-2.0-only, with the existing Linux syscall exception on
`protocol.h`. Keep the SPDX identifier when editing a file and use the same
license for contributions to it. See [COPYING](COPYING).

## Releases

Keep `VERSION` in `hwmon_virt.py`, `PACKAGE_VERSION` in `dkms.conf`, and
`MODULE_VERSION` in `hwmon_virt.c` consistent; the test suite checks them.
Update versioned manual-install/removal examples and the changelog at the same
time. Each published release gets a new version and a matching `vX.Y.Z` tag.
Leave release notes under **Unreleased** until the release is actually published.

Before tagging a release, check CI, record any compatibility limitations, and
exercise the documented upgrade path. A local build result does not stand in
for untested kernel or distribution combinations.
