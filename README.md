# PharaohBot — Windows-First Discord Earn-by-Activity Bot

PharaohBot rewards meaningful chat and active voice participation with pDAI on PulseChain. It was designed for Windows operators first, with an Ubuntu appendix for later deployment.

⚠️ **Security note:** The HD fallback deposit backend is **TEST ONLY** and unsafe for real funds. Always prefer the custodial CryptoAPIs/Venly flow for real value.

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
   - Set earning channel IDs later via slash commands if you prefer.

## 4. Create the Discord Application & Bot

1. Visit the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Add a Bot user and copy its token into `.env` (`DISCORD_BOT_TOKEN`).
3. Enable privileged intents for the bot:
   - **SERVER MEMBERS INTENT**
   - **GUILD VOICE STATES INTENT**
   - **MESSAGE CONTENT INTENT** (needed for text analysis)
4. Under **OAuth2 → URL Generator** choose `bot` + `applications.commands` scopes and grant permissions: `Manage Webhooks`, `Read Message History`, `Send Messages`, `Use Slash Commands`, `Add Reactions`, `Manage Messages` (optional but helps for temp replies). Invite the bot to your guild.

## 5. Pick your deposit backend

### A. Custodial (CryptoAPIs / Venly) — Recommended on Windows

1. Create a CryptoAPIs account and provision an EVM wallet that can derive addresses.
2. Note the wallet ID and API key. Populate `.env`:
   - `CRYPTOAPIS_API_KEY`
   - `CRYPTOAPIS_WALLET_ID`
   - `CRYPTOAPIS_WEBHOOK_HMAC_SECRET` (any strong string you choose)
3. Start the bot once to expose the local webhook server at `http://localhost:3001/webhook/deposit` (port configurable via `WEBHOOK_PORT`).
4. Install [ngrok for Windows](https://ngrok.com/download) and expose the webhook:
   ```powershell
   ngrok http 3001
   ```
   Copy the generated **HTTPS** URL.
5. In CryptoAPIs, configure a webhook for deposit callbacks pointing to `https://<ngrok-domain>/webhook/deposit` and include your HMAC secret header (`X-Hub-Signature`).
6. Test by requesting a `/deposit` address in Discord, sending a tiny amount of pDAI on PulseChain to that address, and waiting for the webhook to confirm (default 12 confirmations). The bot credits the ledger and DM’s the user.

### B. HD fallback (TEST ONLY — unsafe for production)

1. Set `HD_MNEMONIC` to a **test-only** mnemonic (never reuse real seed phrases) and optionally tweak `HD_DERIVATION_PATH`.
2. In Discord, run `/deposit` to obtain the deterministic address derived from `m/44'/60'/0'/0/<discord_id_index>`.
3. Send test tokens to that address (ideally on a test environment) and invoke `/proof <txHash>` once the transaction is mined. The bot verifies confirmations on PulseChain BlockScout and credits the pDAI amount.

---

## 6. Run PharaohBot on Windows

```powershell
py main.py
```

Once online:
1. Set the earning voice channel via `/setvc #your-voice-channel`.
2. Toggle eligible chat channels with `/settext #your-chat-channel`.
3. Have members join the configured voice channel (unmuted) or send qualifying messages to begin earning.
4. Users can monitor their balances with `/balance`.

### Testing earnings
- Voice: stay active in the configured VC; the bot awards 0.0008 pDAI per minute up to 480 minutes/day.
- Chat: messages ≥120 characters or ≥2 sentences, dissimilar to the last 5 messages, 60 second per-channel cooldown, and 60-qualifying-message daily cap.

### Withdrawals flow
1. Member runs `/withdraw <amount> <0xAddress>` (minimum 1.0 pDAI, 1 request/24h).
2. Bot shows “Withdrawing…” and posts an embed in the admin channel with Approve/Reject buttons.
3. Admin Approve → modal appears to paste the transaction hash after manually sending funds. Upon submission the bot marks the withdrawal PAID, posts tx hash, and DM’s the user.
4. Admin Reject → funds are returned to the user’s ledger via a REFUND entry.

### Alt-detection & limits enforced
- Discord account must be ≥7 days old; server join age ≥3 days.
- Voice earnings: 8h/day max, only when not self-muted/deafened.
- Chat earnings: similarity filter ≥0.70 against last 5 qualifying messages, 60-second per-channel cooldown, 60-message daily cap.
- Withdrawals limited to 1 per 24h.
- Clear replies are sent when a rule blocks earnings or withdrawals.

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
3. Run manually with `python3 main.py` or create a `systemd` unit (e.g. `/etc/systemd/system/pharaohbot.service`) that launches the bot on boot. Remember to configure your webhook exposure (ngrok or reverse proxy) similarly to the Windows section.

---

## Security reminders
- Never store or expose hot private keys. All withdrawals are manual.
- HD fallback mnemonic is for testing only—do not deposit real value.
- Keep your ngrok URL private; rotate if compromised.
- Require 12 confirmations (configurable) before crediting deposits.
- Keep the admin channel private since withdrawal requests are posted there.

Happy building! 🎉
