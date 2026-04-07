# systemd units (user services)

These unit files are meant to be symlinked into the user systemd directory on each machine.

## Install (per machine)

```bash
mkdir -p ~/.config/systemd/user
ln -s /path/to/heimdall_py/systemd/heimdall.service ~/.config/systemd/user/heimdall.service
ln -s /path/to/heimdall_py/systemd/heimdall-agent.service ~/.config/systemd/user/heimdall-agent.service
```

## Enable autostart (even before login)

```bash
sudo loginctl enable-linger "$USER"
```

Then enable and start the services:

```bash
systemctl --user daemon-reload
systemctl --user enable --now heimdall.service
systemctl --user enable --now heimdall-agent.service
```

## Notes

- The units load `.env` from the repo root; update `EnvironmentFile=` if your path differs.
- Both units assume the repo lives at `/home/blazzee/Development/heimdall_py`.
