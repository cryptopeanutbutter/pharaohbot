# PharaohBot — Windows-First Discord Earn-by-Activity Bot

PharaohBot rewards meaningful chat and active voice participation with pDAI on PulseChain. It is designed for Windows operators first, with an Ubuntu appendix for later deployment. Users earn into an internal ledger and request manual withdrawals that surface rich embeds for admins—no deposit infrastructure or custodial integration is required.

---

## 1. Install Python 3.10+ on Windows

1. Download the latest Python 3.10+ installer from [python.org](https://www.python.org/downloads/windows/).
2. Run the installer and **check “Add Python to PATH.”**
3. After installation, open PowerShell and upgrade pip:
   ```powershell
   py -m pip install -U pip
   ```

## 2. Clone the repository and install dependencies

```powershell
git clone https://github.com/your-org/pharaohbot.git
cd pharaohbot
py -m pip install -U discord.py aiosqlite requests python-dotenv
```

## 3. Configure environment variables

1. Copy `.env.example` to `.env`:
   ```powershell
   copy .env.example .env
   ```
2. Fill in the required values inside `.env`:
   - `DISCORD_BOT_TOKEN`
   - `OWNER_DISCORD_ID`
   - `ADMIN_CHANNEL_ID`
   - `GUILD_ID`
   - Optional comma-separated `ADMIN_IDS`
   - Channel IDs can be filled later or configured at runtime with slash commands.

## 4. Create the Discord Application & Bot

1. Visit the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Add a Bot user and copy its token into `.env` (`DISCORD_BOT_TOKEN`).
3. Enable privileged intents for the bot:
   - **SERVER MEMBERS INTENT**
   - **GUILD VOICE STATES INTENT**
   - **MESSAGE CONTENT INTENT** (needed for text analysis)
4. Under **OAuth2 → URL Generator** choose `bot` + `applications.commands` scopes and grant permissions such as `Read Message History`, `Send Messages`, `Use Slash Commands`, and `Manage Messages` (for temporary replies). Invite the bot to your guild.

---

## 5. Run PharaohBot on Windows

```powershell
py main.py
```

Once online:
1. Set the earning voice channel via `/setvc #your-voice-channel`.
2. Toggle eligible chat channels with `/settext #your-chat-channel`.
3. Members earn automatically when they meet the activity rules below.
4. Users can check `/balance` to review their ledger total, USD estimate, and recent withdrawal requests.

### Earning rules
- **Voice:** 0.0008 pDAI per active minute (not muted/deafened) in the configured VC, capped at 480 minutes/day.
- **Chat:** Messages ≥120 characters or ≥2 sentences, dissimilar to the last 5 qualifying messages (similarity ≤0.70), one qualifying message per channel per 60 seconds, capped at 60 messages/day.
- **Alt detection:** Discord account age ≥7 days and server join age ≥3 days are enforced before rewarding or allowing withdrawals.

### Withdrawals flow (manual but automated feel)
1. Member runs `/withdraw <amount> <0xAddress>` (minimum 1.0 pDAI, only 1 request per 24h).
2. Bot shows “Withdrawing…” and posts an embed in the admin channel with Approve/Reject buttons.
3. Admin clicks **Approve**, manually sends funds from their wallet, enters the transaction hash in the modal, and the bot marks the withdrawal as PAID, edits the embed, and DM’s the user.
4. Admin clicks **Reject**, the bot refunds the locked funds via a REFUND ledger entry and updates the embed.

Members should already have their own wallet; PharaohBot never holds or generates deposit addresses.

### Optional: keep PharaohBot running quietly
- Use Windows Task Scheduler → **Create Task** that runs `py main.py` at logon.
- Or run with `pythonw.exe main.py` to hide the console (ensure logs are captured elsewhere first).
- Stop the bot with `Ctrl+C` in the console window.

### Logs
Logs output to the console by default. Redirect to a file if desired:
```powershell
py main.py *> pharaohbot.log
```

---

## Appendix: Running on Ubuntu later

1. Install system dependencies:
   ```bash
   sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
   ```
2. Clone and install:
   ```bash
   git clone https://github.com/your-org/pharaohbot.git
   cd pharaohbot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install discord.py aiosqlite requests python-dotenv
   cp .env.example .env
   # fill in variables
   ```
3. Run manually with `python3 main.py` or create a `systemd` unit (e.g. `/etc/systemd/system/pharaohbot.service`) that launches the bot on boot.

---

## Security reminders
- Never store or expose hot private keys. All withdrawals are manual.
- Keep the admin channel private because withdrawal requests and approval embeds surface sensitive details.
- Maintain strong Discord permissions for owner/admin roles and rotate bot tokens if leaked.
- Regularly monitor logs for abuse attempts; adjust earning rates and limits with `/setrate` as needed.

Happy building! 🎉
