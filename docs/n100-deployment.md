# N100 deployment reliability

The installer actively runs health checks while waiting up to 900 seconds. A startup retry limit of 40 could exhaust early and restart Prisma during its first migration. Startup retries now have no count limit; the installer deadline and normal liveness checks remain in effect.

The same runtime settings were exercised on an existing N100 with 53 containers. All containers recovered automatically after a physical reboot; the completed read-only check was at boot uptime 558.4 seconds. This is an observation, not a fixed startup-time guarantee.

[Physical-host evidence](https://github.com/WOOWTECH/Woow_ubuntu_version_control/blob/195dcda9357100a7821407e1c8469a349acc9831/distribution/docs/reboot-acceptance.md).
