# Autostart (non-systemd)

Use cron `@reboot` jobs to start the control plane and agent on headless servers.

## Install

```bash
crontab -e
```

Add:

```
@reboot /bin/bash -lc '/home/blazzee/heimdall_py/start-agent.sh' >> /home/blazzee/heimdall_py/logs/agent.cron.log 2>&1
@reboot /bin/bash -lc 'cd /home/blazzee/heimdall_py && nix develop -c ./start.sh' >> /home/blazzee/heimdall_py/logs/control.cron.log 2>&1
```

Verify:

```bash
crontab -l
```

## Notes

- These commands run under your user account at boot.
- Adjust `/home/blazzee/heimdall_py` if the repo lives elsewhere.
- If you want `.env` loaded explicitly, add `set -a; source /home/blazzee/heimdall_py/.env; set +a;` inside the `bash -lc` command.
