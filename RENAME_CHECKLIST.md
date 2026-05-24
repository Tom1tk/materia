# tgbot → materia rename checklist

Full rename of the project identity: service name, OS user, install directory, and all in-code references.

---

## Phase 1 — Code changes (Claude makes these, review before committing)

- [x] `agent.py` — `/opt/tgbot/` paths (×2) + service name in prompt string
- [x] `cron_wrapper.py` — `/opt/tgbot/` paths (×3)
- [x] `context.py` — `/opt/tgbot/MEMORY.md`
- [x] `memory.py` — `/opt/tgbot/data/memory.db` (×2)
- [x] `intent.py` — `/opt/tgbot/` paths (×2)
- [x] `bot.py` — `/opt/tgbot/` paths (×5)
- [x] `tools/builtin.py` — paths (×11) + `sudo systemctl restart tgbot` command
- [x] `manifest.json` — `restart_bot` description mentions `systemctl restart tgbot`
- [x] `README.md` — all `systemctl` commands, system-user prose, `/opt/tgbot/` path references

---

## Phase 2 — Service file update (Claude edits in place, user renames)

File: `/etc/systemd/system/tgbot.service`

- [x] `User=tgbot` → `User=materia`
- [x] `Group=tgbot` → `Group=materia`
- [x] `SyslogIdentifier=tgbot` → `SyslogIdentifier=materia`
- [x] `WorkingDirectory=/opt/tgbot` → `WorkingDirectory=/opt/materia`
- [x] `ExecStart=...` path `/opt/tgbot/` → `/opt/materia/`

---

## Phase 3 — System commands (run in order after code is committed and pushed)

> Stop the service first, then make all changes, then bring it back up.

```bash
# 1. Stop the service
sudo systemctl stop tgbot
sudo systemctl disable tgbot

# 2. Rename OS user and group
sudo usermod -l materia tgbot
sudo groupmod -n materia tgbot

# 3. Move the install directory
sudo mv /opt/tgbot /opt/materia

# 4. Fix ownership
sudo chown -R materia:materia /opt/materia

# 5. Update sudoers
#    Open with: sudo visudo
#    Change:  tgbot ALL=(ALL) NOPASSWD: ALL
#    To:      materia ALL=(ALL) NOPASSWD: ALL

# 6. Rename the service file
sudo mv /etc/systemd/system/tgbot.service /etc/systemd/system/materia.service

# 7. Migrate the crontab
#    Export, update paths, re-import under new username
sudo crontab -u tgbot -l | sed 's|/opt/tgbot/|/opt/materia/|g' | sudo tee /var/spool/cron/crontabs/materia > /dev/null
sudo chown materia:crontab /var/spool/cron/crontabs/materia
sudo chmod 600 /var/spool/cron/crontabs/materia
# Remove old crontab
sudo crontab -u tgbot -r

# 8. Reload systemd and start the renamed service
sudo systemctl daemon-reload
sudo systemctl enable materia
sudo systemctl start materia

# 9. Verify
sudo systemctl status materia
sudo journalctl -u materia -f
```

---

## Phase 4 — Verification

- [ ] `sudo systemctl status materia` shows active/running
- [ ] Telegram receives "Bot is online." startup notification
- [ ] `/status` responds correctly in Telegram
- [ ] `/restart_bot` works (triggers service restart, online notification follows)
- [ ] `sudo crontab -u materia -l` shows all expected cron entries with `/opt/materia/` paths
- [ ] `sudo crontab -u tgbot -l` returns "no crontab for tgbot" (old user cleaned up)
- [ ] `cat /etc/systemd/system/materia.service` shows correct User/Group/paths
- [ ] `sudo grep tgbot /etc/sudoers` returns nothing (old sudoers entry gone)
