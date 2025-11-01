import asyncio
import datetime as dt
import difflib
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp
import aiosqlite
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import requests

# ---------------------------------------------
# Environment & configuration helpers
# ---------------------------------------------

load_dotenv()


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_list(name: str) -> List[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            logging.warning("Failed to parse id '%s' from %s", part, name)
    return out


@dataclass
class BotConfig:
    token: str
    owner_id: int
    admin_ids: List[int]
    admin_channel_id: int
    guild_id: int
    vc_rate: float
    vc_afk_timeout: int
    vc_max_minutes: int
    chat_rate: float
    chat_min_chars: int
    chat_cooldown_seconds: int
    chat_daily_cap: int
    chat_similarity_max: float
    withdraw_min: float
    withdraw_per_24h: int
    price_asset_id: str
    price_cache_seconds: int
    monitored_vc_id: Optional[int]
    earning_text_channel_ids: List[int]
    database_path: str
    cryptoapis_key: Optional[str]
    cryptoapis_wallet_id: Optional[str]
    cryptoapis_hmac_secret: Optional[str]
    blockscout_api_url: str
    confirmations_required: int
    webhook_port: int
    hd_mnemonic: Optional[str]
    hd_derivation_path: str

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.getenv("DISCORD_BOT_TOKEN")
        owner_id = int(os.getenv("OWNER_DISCORD_ID", "0"))
        admin_ids = env_list("ADMIN_IDS")
        admin_channel_id = int(os.getenv("ADMIN_CHANNEL_ID", "0"))
        guild_id = int(os.getenv("GUILD_ID", "0"))

        return cls(
            token=token or "",
            owner_id=owner_id,
            admin_ids=admin_ids,
            admin_channel_id=admin_channel_id,
            guild_id=guild_id,
            vc_rate=env_float("EARNING_VC_PDAI_PER_MIN", 0.0008),
            vc_afk_timeout=env_int("EARNING_VC_AFK_TIMEOUT_SECONDS", 30),
            vc_max_minutes=env_int("EARNING_VC_MAX_MINUTES_PER_DAY", 480),
            chat_rate=env_float("EARNING_CHAT_PDAI_PER_MSG", 0.002),
            chat_min_chars=env_int("EARNING_CHAT_MIN_CHARS", 120),
            chat_cooldown_seconds=env_int("EARNING_CHAT_MINUTE_COOLDOWN_SECONDS", 60),
            chat_daily_cap=env_int("EARNING_CHAT_DAILY_MSG_CAP", 60),
            chat_similarity_max=env_float("EARNING_CHAT_SIMILARITY_MAX", 0.70),
            withdraw_min=env_float("WITHDRAW_MIN_PDAI", 1.0),
            withdraw_per_24h=env_int("WITHDRAW_REQUESTS_PER_24H", 1),
            price_asset_id=os.getenv("COINGECKO_PDAI_ID", "dai-on-pulsechain"),
            price_cache_seconds=env_int("PRICE_CACHE_SECONDS", 60),
            monitored_vc_id=int(os.getenv("MONITORED_VC_ID", "0")) or None,
            earning_text_channel_ids=env_list("EARNING_TEXT_CHANNEL_IDS"),
            database_path=os.getenv("DATABASE_PATH", "./ledger.sqlite3"),
            cryptoapis_key=os.getenv("CRYPTOAPIS_API_KEY") or None,
            cryptoapis_wallet_id=os.getenv("CRYPTOAPIS_WALLET_ID") or None,
            cryptoapis_hmac_secret=os.getenv("CRYPTOAPIS_WEBHOOK_HMAC_SECRET") or None,
            blockscout_api_url=os.getenv("BLOCKSCOUT_API_URL", "https://api.scan.pulsechain.com/api"),
            confirmations_required=env_int("CONFIRMATIONS_REQUIRED", 12),
            webhook_port=env_int("WEBHOOK_PORT", 3001),
            hd_mnemonic=os.getenv("HD_MNEMONIC") or None,
            hd_derivation_path=os.getenv("HD_DERIVATION_PATH", "m/44'/60'/0'/0/"),
        )
# ---------------------------------------------
# SQLite helpers
# ---------------------------------------------


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def init(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self.create_schema()

    async def create_schema(self) -> None:
        assert self.conn
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                discord_id TEXT PRIMARY KEY,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ledger(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                token TEXT,
                amount REAL,
                type TEXT,
                ref TEXT,
                note TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS withdrawals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                amount REAL,
                token TEXT,
                to_address TEXT,
                status TEXT,
                tx_hash TEXT,
                admin_msg_id TEXT,
                created_at TEXT,
                processed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS deposit_addresses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                address TEXT,
                backend TEXT,
                status TEXT,
                created_at TEXT,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS config(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS chat_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                channel_id TEXT,
                content TEXT,
                created_at TEXT
            );
            """
        )
        await self.conn.commit()

    async def execute(self, query: str, params: Tuple = ()) -> None:
        assert self.conn
        async with self.lock:
            await self.conn.execute(query, params)
            await self.conn.commit()

    async def fetchone(self, query: str, params: Tuple = ()) -> Optional[aiosqlite.Row]:
        assert self.conn
        async with self.lock:
            cursor = await self.conn.execute(query, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def fetchall(self, query: str, params: Tuple = ()) -> List[aiosqlite.Row]:
        assert self.conn
        async with self.lock:
            cursor = await self.conn.execute(query, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return rows

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None


# ---------------------------------------------
# Price cache
# ---------------------------------------------


class PriceCache:
    def __init__(self, asset_id: str, ttl: int):
        self.asset_id = asset_id
        self.ttl = ttl
        self.cached_at: float = 0.0
        self.cached_value: Optional[float] = None

    async def get_price(self) -> Optional[float]:
        now = time.monotonic()
        if self.cached_value is not None and now - self.cached_at < self.ttl:
            return self.cached_value
        try:
            response = await asyncio.to_thread(
                requests.get,
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": self.asset_id, "vs_currencies": "usd"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            price = data.get(self.asset_id, {}).get("usd")
            if price is not None:
                self.cached_value = float(price)
                self.cached_at = now
                return self.cached_value
        except Exception as exc:  # pragma: no cover - logging fallback
            logging.warning("Failed to fetch price: %s", exc)
        return None
# ---------------------------------------------
# HD wallet fallback utilities (TEST ONLY)
# ---------------------------------------------

# secp256k1 parameters
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_A = 0
_B = 7
_Gx = 55066263022277343669578718895168534326250603453777594175500187360389116729240
_Gy = 32670510020758816978083085130507043184471273380659243275938904335757337482424
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _modinv(a: int, n: int) -> int:
    return pow(a, -1, n)


def _point_add(p1: Tuple[int, int], p2: Tuple[int, int]) -> Tuple[int, int]:
    if p1 == (None, None):
        return p2
    if p2 == (None, None):
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return (None, None)
    if x1 == x2 and y1 == y2:
        return _point_double(p1)
    m = ((y2 - y1) * _modinv((x2 - x1) % _P, _P)) % _P
    x3 = (m * m - x1 - x2) % _P
    y3 = (m * (x1 - x3) - y1) % _P
    return x3, y3


def _point_double(p: Tuple[int, int]) -> Tuple[int, int]:
    x, y = p
    if y == 0:
        return (None, None)
    m = ((3 * x * x + _A) * _modinv(2 * y, _P)) % _P
    x3 = (m * m - 2 * x) % _P
    y3 = (m * (x - x3) - y) % _P
    return x3, y3


def _scalar_mult(k: int, point: Tuple[int, int]) -> Tuple[int, int]:
    if k % _N == 0 or point == (None, None):
        return (None, None)
    result = (None, None)
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_double(addend)
        k >>= 1
    return result


def _pbkdf2_hmac_sha512(password: str, salt: str, iterations: int, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", password.encode(), salt.encode(), iterations, dklen)


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    return _pbkdf2_hmac_sha512(mnemonic, "mnemonic" + passphrase, 2048, 64)


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


def private_to_public(privkey: bytes) -> bytes:
    pk_int = int.from_bytes(privkey, "big")
    point = _scalar_mult(pk_int, (_Gx, _Gy))
    if point == (None, None):
        raise ValueError("Invalid point")
    x, y = point
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _ckd_priv(parent_key: bytes, parent_chain_code: bytes, index: int) -> Tuple[bytes, bytes]:
    hardened = index >= 0x80000000
    if hardened:
        data = b"\x00" + parent_key + index.to_bytes(4, "big")
    else:
        pub = private_to_public(parent_key)
        data = pub + index.to_bytes(4, "big")
    i = _hmac_sha512(parent_chain_code, data)
    il, ir = i[:32], i[32:]
    ki = (int.from_bytes(il, "big") + int.from_bytes(parent_key, "big")) % _N
    if ki == 0:
        raise ValueError("Invalid derived key")
    return ki.to_bytes(32, "big"), ir


def keccak256(data: bytes) -> bytes:
    # Lightweight pure-Python Keccak-256 implementation (sponge construction)
    # This implementation is intentionally straightforward and only used for
    # deriving fallback addresses. Avoid for performance-critical paths.
    b = 1600 - 512 * 2
    w = 64
    l = 6
    nr = 12 + 2 * l

    def _rot(x, n):
        return ((x << n) | (x >> (w - n))) & ((1 << w) - 1)

    def _keccak_f(state):
        RC = [
            0x0000000000000001,
            0x0000000000008082,
            0x800000000000808A,
            0x8000000080008000,
            0x000000000000808B,
            0x0000000080000001,
            0x8000000080008081,
            0x8000000000008009,
            0x000000000000008A,
            0x0000000000000088,
            0x0000000080008009,
            0x000000008000000A,
            0x000000008000808B,
            0x800000000000008B,
            0x8000000000008089,
            0x8000000000008003,
            0x8000000000008002,
            0x8000000000000080,
            0x000000000000800A,
            0x800000008000000A,
            0x8000000080008081,
            0x8000000000008080,
            0x0000000080000001,
            0x8000000080008008,
        ]
        r = [
            [0, 36, 3, 41, 18],
            [1, 44, 10, 45, 2],
            [62, 6, 43, 15, 61],
            [28, 55, 25, 21, 56],
            [27, 20, 39, 8, 14],
        ]
        for ir in range(nr):
            c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rot(c[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    state[x + 5 * y] ^= d[x]
            b_tmp = [0] * 25
            for x in range(5):
                for y in range(5):
                    b_tmp[y + 5 * ((2 * x + 3 * y) % 5)] = _rot(state[x + 5 * y], r[x][y])
            for x in range(5):
                for y in range(5):
                    state[x + 5 * y] = b_tmp[x + 5 * y] ^ ((~b_tmp[((x + 1) % 5) + 5 * y]) & b_tmp[((x + 2) % 5) + 5 * y])
            state[0] ^= RC[ir]

    rate = b
    block_size = 0
    state = [0] * 25
    buf = bytearray()

    for byte in data:
        buf.append(byte)
        block_size += 8
        if block_size == rate:
            for i in range(rate // 64):
                state[i] ^= int.from_bytes(buf[i * 8:(i + 1) * 8], "little")
            _keccak_f(state)
            buf = bytearray()
            block_size = 0

    buf.append(0x01)
    while (len(buf) * 8) % rate != rate - 8:
        buf.append(0x00)
    buf.append(0x80)
    for i in range(rate // 64):
        chunk = buf[i * 8:(i + 1) * 8]
        if len(chunk) < 8:
            chunk = chunk + b"\x00" * (8 - len(chunk))
        state[i] ^= int.from_bytes(chunk, "little")
    _keccak_f(state)
    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 64):
            out.extend(state[i].to_bytes(8, "little"))
        if len(out) >= 32:
            break
        _keccak_f(state)
    return bytes(out[:32])


def eth_checksum_address(address: str) -> str:
    address = address.lower().replace("0x", "")
    hash_hex = keccak256(address.encode()).hex()
    checksum = "0x" + "".join(
        c.upper() if int(hash_hex[i], 16) >= 8 else c
        for i, c in enumerate(address)
    )
    return checksum


def derive_eth_address(mnemonic: str, path: str) -> str:
    seed = mnemonic_to_seed(mnemonic)
    key = seed[:32]
    chain = seed[32:]
    segments = path.strip().split('/')
    if segments[0] != 'm':
        raise ValueError("Invalid path")
    for seg in segments[1:]:
        hardened = seg.endswith("'")
        if hardened:
            index = int(seg[:-1]) + 0x80000000
        else:
            index = int(seg)
        key, chain = _ckd_priv(key, chain, index)
    pub = private_to_public(key)
    addr = keccak256(pub[1:])[12:]
    return eth_checksum_address("0x" + addr.hex())
# ---------------------------------------------
# Economy helpers
# ---------------------------------------------


def utcnow() -> dt.datetime:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)


def start_of_day(ts: Optional[dt.datetime] = None) -> dt.datetime:
    ts = ts or utcnow()
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


class Economy:
    def __init__(self, db: Database, config: BotConfig):
        self.db = db
        self.config = config

    async def ensure_user(self, discord_id: int) -> None:
        existing = await self.db.fetchone("SELECT 1 FROM users WHERE discord_id = ?", (str(discord_id),))
        if not existing:
            await self.db.execute(
                "INSERT INTO users(discord_id, created_at) VALUES (?, ?)",
                (str(discord_id), utcnow().isoformat()),
            )

    async def ledger_balance(self, discord_id: int, token: str = "pDAI") -> float:
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS balance FROM ledger WHERE discord_id = ? AND token = ?",
            (str(discord_id), token),
        )
        return float(row["balance"] if row else 0.0)

    async def add_ledger_entry(
        self,
        discord_id: int,
        amount: float,
        entry_type: str,
        note: str = "",
        token: str = "pDAI",
        ref: Optional[str] = None,
    ) -> None:
        await self.ensure_user(discord_id)
        await self.db.execute(
            "INSERT INTO ledger(discord_id, token, amount, type, ref, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(discord_id), token, amount, entry_type, ref, note, utcnow().isoformat()),
        )

    async def todays_voice_minutes(self, discord_id: int) -> int:
        rows = await self.db.fetchall(
            "SELECT note, created_at FROM ledger WHERE discord_id = ? AND type = ?",
            (str(discord_id), "VC_EARN"),
        )
        total = 0
        today = start_of_day()
        for row in rows:
            created = dt.datetime.fromisoformat(row["created_at"])
            if created >= today:
                try:
                    minutes = int(float(row["note"].split(" ")[0]))
                except Exception:
                    minutes = int(self.config.vc_rate and float(row["amount"]) / self.config.vc_rate)
                total += minutes
        return total

    async def todays_chat_count(self, discord_id: int) -> int:
        today = start_of_day()
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM chat_messages WHERE discord_id = ? AND created_at >= ?",
            (str(discord_id), today.isoformat()),
        )
        return int(row["c"] if row else 0)

    async def record_chat_message(self, discord_id: int, channel_id: int, content: str) -> None:
        await self.db.execute(
            "INSERT INTO chat_messages(discord_id, channel_id, content, created_at) VALUES (?, ?, ?, ?)",
            (str(discord_id), str(channel_id), content, utcnow().isoformat()),
        )

    async def last_messages(self, discord_id: int, limit: int = 5) -> List[str]:
        rows = await self.db.fetchall(
            "SELECT content FROM chat_messages WHERE discord_id = ? ORDER BY id DESC LIMIT ?",
            (str(discord_id), limit),
        )
        return [row["content"] for row in rows]

    async def pending_withdrawals(self, discord_id: int) -> List[aiosqlite.Row]:
        return await self.db.fetchall(
            "SELECT * FROM withdrawals WHERE discord_id = ? AND status IN ('REQUESTED','APPROVED') ORDER BY id DESC",
            (str(discord_id),),
        )

    async def last_withdrawal_time(self, discord_id: int) -> Optional[dt.datetime]:
        row = await self.db.fetchone(
            "SELECT created_at FROM withdrawals WHERE discord_id = ? ORDER BY id DESC LIMIT 1",
            (str(discord_id),),
        )
        if row:
            return dt.datetime.fromisoformat(row["created_at"])
        return None
# ---------------------------------------------
# Discord bot implementation
# ---------------------------------------------


class PharaohBot(commands.Bot):
    def __init__(self, config: BotConfig, db: Database, price_cache: PriceCache):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.db = db
        self.economy = Economy(db, config)
        self.price_cache = price_cache
        self.tree = app_commands.CommandTree(self)
        self.voice_presence: Dict[int, dt.datetime] = {}
        self.chat_cooldowns: Dict[Tuple[int, int], dt.datetime] = {}
        self.web_app_runner: Optional[web.AppRunner] = None

    async def setup_hook(self) -> None:
        await self.db.init()
        await self.sync_config()
        voice_tick_loop.start(self)
        await self.start_webhook()
        guild = discord.Object(id=self.config.guild_id) if self.config.guild_id else None
        await self.tree.sync(guild=guild)
        logging.info("Slash commands synced")

    async def start_webhook(self) -> None:
        if not self.config.cryptoapis_key:
            return
        app = web.Application()

        async def handle(request: web.Request) -> web.Response:
            raw_body = await request.read()
            signature = request.headers.get("X-Hub-Signature")
            secret = self.config.cryptoapis_hmac_secret or ""
            if secret:
                computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
                if not signature or not hmac.compare_digest(signature, computed):
                    logging.warning("Invalid webhook signature")
                    return web.Response(status=401, text="invalid signature")
            try:
                payload = json.loads(raw_body.decode())
            except json.JSONDecodeError:
                return web.Response(status=400, text="invalid json")
            await self.handle_deposit_webhook(payload)
            return web.Response(text="ok")

        app.router.add_post("/webhook/deposit", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.webhook_port)
        await site.start()
        self.web_app_runner = runner
        logging.info("Webhook listening on port %s", self.config.webhook_port)

    async def close(self) -> None:
        voice_tick_loop.cancel()
        if self.web_app_runner:
            await self.web_app_runner.cleanup()
        await self.db.close()
        await super().close()

    async def sync_config(self) -> None:
        for key, value in [
            ("EARNING_VC_PDAI_PER_MIN", str(self.config.vc_rate)),
            ("EARNING_CHAT_PDAI_PER_MSG", str(self.config.chat_rate)),
            ("MONITORED_VC_ID", str(self.config.monitored_vc_id or "")),
        ]:
            await self.db.execute(
                "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.config.owner_id or user_id in self.config.admin_ids

    async def handle_deposit_webhook(self, payload: dict) -> None:
        try:
            address = payload["data"]["item"]["depositAddress"]
            amount = float(payload["data"]["item"].get("amount", {}).get("amount", "0"))
            tx_hash = payload["data"]["item"].get("transactionHash")
            confirmations = int(payload["data"]["item"].get("currentConfirmations", 0))
        except Exception as exc:
            logging.error("Malformed webhook payload: %s", exc)
            return
        if confirmations < self.config.confirmations_required:
            logging.info("Webhook for %s pending confirmations", address)
            return
        row = await self.db.fetchone(
            "SELECT discord_id, backend, status FROM deposit_addresses WHERE address = ?",
            (address,),
        )
        if not row:
            logging.warning("Webhook for unknown address %s", address)
            return
        if row["status"] == "USED":
            logging.info("Address %s already used", address)
            return
        discord_id = int(row["discord_id"])
        await self.db.execute(
            "UPDATE deposit_addresses SET status='USED', used_at=? WHERE address=?",
            (utcnow().isoformat(), address),
        )
        await self.economy.add_ledger_entry(
            discord_id,
            amount,
            "DEPOSIT",
            note=f"Deposit via {row['backend']} tx {tx_hash}",
            ref=tx_hash,
        )
        logging.info("Credited %.6f pDAI to %s", amount, discord_id)
        user = self.get_user(discord_id)
        if user:
            try:
                await user.send(f"Deposit of {amount:.4f} pDAI confirmed (tx: {tx_hash}).")
            except discord.HTTPException:
                pass

    async def fetch_blockscout_json(self, params: Dict[str, str]) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.config.blockscout_api_url, params=params, timeout=15) as resp:
                resp.raise_for_status()
                return await resp.json()
# ---------------------------------------------
# Background voice earning loop
# ---------------------------------------------


@tasks.loop(minutes=1)
async def voice_tick_loop(bot: PharaohBot) -> None:
    await bot.wait_until_ready()
    if not bot.config.monitored_vc_id:
        return
    guild = bot.get_guild(bot.config.guild_id)
    if not guild:
        return
    channel = guild.get_channel(bot.config.monitored_vc_id)
    if not isinstance(channel, discord.VoiceChannel):
        return
    now = utcnow()
    active_ids = set()
    for member in channel.members:
        if member.bot:
            continue
        if member.voice is None:
            continue
        if member.voice.self_mute or member.voice.self_deaf or member.voice.mute or member.voice.deaf:
            continue
        active_ids.add(member.id)
        account_age = utcnow() - member.created_at.replace(tzinfo=dt.timezone.utc)
        join_age = utcnow() - (member.joined_at.replace(tzinfo=dt.timezone.utc) if member.joined_at else utcnow())
        if account_age < dt.timedelta(days=7) or join_age < dt.timedelta(days=3):
            continue
        first_seen = bot.voice_presence.get(member.id)
        if not first_seen:
            bot.voice_presence[member.id] = now
            continue
        if (now - first_seen).total_seconds() < bot.config.vc_afk_timeout:
            continue
        already_today = await bot.economy.todays_voice_minutes(member.id)
        if already_today >= bot.config.vc_max_minutes:
            continue
        remaining = bot.config.vc_max_minutes - already_today
        minutes = min(1, remaining)
        amount = minutes * bot.config.vc_rate
        await bot.economy.add_ledger_entry(
            member.id,
            amount,
            "VC_EARN",
            note=f"{minutes} minutes in VC",
        )
        bot.voice_presence[member.id] = now
        try:
            await member.send(f"Earned {amount:.6f} pDAI for voice activity.")
        except discord.HTTPException:
            pass
    # Clean up presence entries for members no longer active
    for tracked_id in list(bot.voice_presence.keys()):
        if tracked_id not in active_ids:
            bot.voice_presence.pop(tracked_id, None)
# ---------------------------------------------
# Slash command helpers
# ---------------------------------------------


def require_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        bot: PharaohBot = interaction.client  # type: ignore
        if interaction.user.id != bot.config.owner_id:
            await interaction.response.send_message("Only the owner can use this command.", ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


def require_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        bot: PharaohBot = interaction.client  # type: ignore
        if not bot.is_admin(interaction.user.id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


async def ensure_interaction_response(interaction: discord.Interaction) -> None:
    if interaction.response.is_done():
        return
    await interaction.response.defer(ephemeral=True)
# ---------------------------------------------
# Command registrations
# ---------------------------------------------


@PharaohBot.tree.command(name="balance", description="Show your pDAI balance and activity stats.")
async def balance_command(interaction: discord.Interaction) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    await ensure_interaction_response(interaction)
    user_id = interaction.user.id
    await bot.economy.ensure_user(user_id)
    balance = await bot.economy.ledger_balance(user_id)
    price = await bot.price_cache.get_price()
    vc_minutes = await bot.economy.todays_voice_minutes(user_id)
    chat_count = await bot.economy.todays_chat_count(user_id)
    pending = await bot.economy.pending_withdrawals(user_id)
    usd_value = balance * price if price is not None else None
    embed = discord.Embed(title="Your Balance", color=discord.Color.blurple())
    embed.add_field(name="pDAI", value=f"{balance:.4f}", inline=True)
    if usd_value is not None:
        embed.add_field(name="≈ USD", value=f"${usd_value:.2f}", inline=True)
    else:
        embed.add_field(name="≈ USD", value="Price unavailable", inline=True)
    embed.add_field(name="Today's voice minutes", value=str(vc_minutes), inline=False)
    embed.add_field(name="Today's qualifying messages", value=str(chat_count), inline=False)
    if pending:
        desc = "\n".join(
            f"#{row['id']} {row['status']} {row['amount']} pDAI → {row['to_address']}" for row in pending
        )
        embed.add_field(name="Pending withdrawals", value=desc, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@PharaohBot.tree.command(name="deposit", description="Get your deposit address.")
async def deposit_command(interaction: discord.Interaction) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    await ensure_interaction_response(interaction)
    user_id = interaction.user.id
    await bot.economy.ensure_user(user_id)
    backend = "custodial" if bot.config.cryptoapis_key else "hd"
    address = await get_or_create_deposit_address(bot, user_id, backend)
    if not address:
        await interaction.followup.send("Failed to create deposit address. Contact an admin.", ephemeral=True)
        return
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?data={address}&size=200x200"
    warning = ""
    if backend == "hd":
        warning = "\n\n⚠️ TEST ONLY: HD fallback addresses are not safe for real funds."
    embed = discord.Embed(title="Deposit Address", description=f"`{address}`{warning}")
    embed.set_image(url=qr_url)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def get_or_create_deposit_address(bot: PharaohBot, user_id: int, backend: str) -> Optional[str]:
    row = await bot.db.fetchone(
        "SELECT address FROM deposit_addresses WHERE discord_id = ? AND status = 'ACTIVE'",
        (str(user_id),),
    )
    if row:
        return row["address"]
    if backend == "custodial":
        address = await create_custodial_address(bot, user_id)
        backend_label = "CRYPTOAPIS"
    else:
        address = await derive_hd_address(bot, user_id)
        backend_label = "HD_TEST"
    if not address:
        return None
    await bot.db.execute(
        "INSERT INTO deposit_addresses(discord_id, address, backend, status, created_at) VALUES (?, ?, ?, 'ACTIVE', ?)",
        (str(user_id), address, backend_label, utcnow().isoformat()),
    )
    return address


async def create_custodial_address(bot: PharaohBot, user_id: int) -> Optional[str]:
    if not bot.config.cryptoapis_key or not bot.config.cryptoapis_wallet_id:
        return None
    payload = {
        "context": "discord-pharaoh",
        "data": {"item": {"label": f"discord-{user_id}"}},
    }
    url = f"https://rest.cryptoapis.io/v2/wallet-as-a-service/wallets/{bot.config.cryptoapis_wallet_id}/addresses"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": bot.config.cryptoapis_key,
    }
    try:
        response = await asyncio.to_thread(
            requests.post,
            url,
            headers=headers,
            data=json.dumps(payload),
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("item", {}).get("address")
    except Exception as exc:
        logging.error("Failed to create custodial address: %s", exc)
        return None


async def derive_hd_address(bot: PharaohBot, user_id: int) -> Optional[str]:
    mnemonic = bot.config.hd_mnemonic
    if not mnemonic:
        return None
    base_path = bot.config.hd_derivation_path.rstrip('/')
    index = user_id & 0x7FFFFFFF
    path = f"{base_path}/{index}"
    try:
        return derive_eth_address(mnemonic, path)
    except Exception as exc:
        logging.error("Failed to derive HD address: %s", exc)
        return None


@PharaohBot.tree.command(name="withdraw", description="Request a withdrawal.")
@app_commands.describe(amount="Amount of pDAI", to_address="Destination address")
async def withdraw_command(interaction: discord.Interaction, amount: float, to_address: str) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    await ensure_interaction_response(interaction)
    user = interaction.user
    account_age = utcnow() - user.created_at.replace(tzinfo=dt.timezone.utc)
    if account_age < dt.timedelta(days=7):
        await interaction.followup.send("Account must be at least 7 days old.", ephemeral=True)
        return
    if isinstance(user, discord.Member) and user.joined_at:
        join_age = utcnow() - user.joined_at.replace(tzinfo=dt.timezone.utc)
        if join_age < dt.timedelta(days=3):
            await interaction.followup.send("You must be in the server for 3 days before withdrawing.", ephemeral=True)
            return
    balance = await bot.economy.ledger_balance(user.id)
    if amount < bot.config.withdraw_min:
        await interaction.followup.send(f"Minimum withdrawal is {bot.config.withdraw_min} pDAI.", ephemeral=True)
        return
    if amount > balance:
        await interaction.followup.send("Insufficient balance.", ephemeral=True)
        return
    window_start = utcnow() - dt.timedelta(hours=24)
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS c FROM withdrawals WHERE discord_id = ? AND created_at >= ?",
        (str(user.id), window_start.isoformat()),
    )
    request_count = int(row["c"]) if row else 0
    if request_count >= bot.config.withdraw_per_24h:
        await interaction.followup.send("Withdrawal request limit reached for the past 24 hours.", ephemeral=True)
        return
    await bot.economy.add_ledger_entry(user.id, -amount, "WITHDRAW_REQUEST", note=f"Locking for withdrawal to {to_address}")
    await bot.db.execute(
        "INSERT INTO withdrawals(discord_id, amount, token, to_address, status, created_at) VALUES (?, ?, 'pDAI', ?, 'REQUESTED', ?)",
        (str(user.id), amount, to_address, utcnow().isoformat()),
    )
    last_row = await bot.db.fetchone(
        "SELECT id FROM withdrawals WHERE discord_id = ? ORDER BY id DESC LIMIT 1",
        (str(user.id),),
    )
    withdrawal_id = int(last_row["id"]) if last_row else None
    embed = discord.Embed(title="Withdrawal Requested", description=f"{user} requests {amount} pDAI → `{to_address}`")
    embed.add_field(name="User", value=f"{user} ({user.id})")
    embed.timestamp = utcnow()

    view = WithdrawalView(bot, user.id, amount, to_address, withdrawal_id)
    admin_channel = bot.get_channel(bot.config.admin_channel_id)
    if isinstance(admin_channel, discord.TextChannel):
        admin_message = await admin_channel.send(embed=embed, view=view)
        view.message_id = admin_message.id
        if withdrawal_id is not None:
            await bot.db.execute(
                "UPDATE withdrawals SET admin_msg_id = ? WHERE id = ?",
                (str(admin_message.id), withdrawal_id),
            )
    await interaction.followup.send("Withdrawing… please wait for admin approval.", ephemeral=True)


class WithdrawalView(discord.ui.View):
    def __init__(self, bot: PharaohBot, user_id: int, amount: float, to_address: str, withdrawal_id: Optional[int]):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.amount = amount
        self.to_address = to_address
        self.withdrawal_id = withdrawal_id
        self.message_id: Optional[int] = None

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore
        if not self.bot.is_admin(interaction.user.id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        modal = WithdrawalModal(self.bot, self.user_id, self.amount, self.to_address, self.message_id, self.withdrawal_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore
        if not self.bot.is_admin(interaction.user.id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await handle_withdrawal_status(
            self.bot,
            self.user_id,
            "REJECTED",
            interaction.user,
            admin_message_id=self.message_id,
            withdrawal_id=self.withdrawal_id,
        )
        await interaction.followup.send("Withdrawal rejected.", ephemeral=True)


class WithdrawalModal(discord.ui.Modal, title="Withdrawal Payment"):
    tx_hash = discord.ui.TextInput(label="Transaction Hash", placeholder="0x…", required=True)

    def __init__(
        self,
        bot: PharaohBot,
        user_id: int,
        amount: float,
        to_address: str,
        message_id: Optional[int],
        withdrawal_id: Optional[int],
    ):
        super().__init__()
        self.bot = bot
        self.user_id = user_id
        self.amount = amount
        self.to_address = to_address
        self.message_id = message_id
        self.withdrawal_id = withdrawal_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_admin(interaction.user.id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        tx_hash = str(self.tx_hash.value).strip()
        await interaction.response.defer(ephemeral=True)
        await handle_withdrawal_status(
            self.bot,
            self.user_id,
            "PAID",
            interaction.user,
            tx_hash=tx_hash,
            admin_message_id=self.message_id,
            withdrawal_id=self.withdrawal_id,
        )
        await interaction.followup.send("Marked as paid.", ephemeral=True)


async def handle_withdrawal_status(
    bot: PharaohBot,
    user_id: int,
    status: str,
    admin_user: discord.abc.User,
    tx_hash: Optional[str] = None,
    admin_message_id: Optional[int] = None,
    withdrawal_id: Optional[int] = None,
) -> None:
    if withdrawal_id is not None:
        row = await bot.db.fetchone(
            "SELECT id, amount, to_address FROM withdrawals WHERE id = ?",
            (withdrawal_id,),
        )
    else:
        row = await bot.db.fetchone(
            "SELECT id, amount, to_address FROM withdrawals WHERE discord_id = ? AND status = 'REQUESTED' ORDER BY id DESC LIMIT 1",
            (str(user_id),),
        )
    if not row:
        return
    withdrawal_id = row["id"]
    amount = float(row["amount"])
    to_address = row["to_address"]
    await bot.db.execute(
        "UPDATE withdrawals SET status=?, processed_at=?, tx_hash=? WHERE id=?",
        (status, utcnow().isoformat(), tx_hash, withdrawal_id),
    )
    if status == "PAID":
        note = f"Paid by {admin_user}"
        await bot.economy.add_ledger_entry(user_id, 0, "WITHDRAW_PAID", note=note, ref=tx_hash)
    elif status == "REJECTED":
        await bot.economy.add_ledger_entry(user_id, amount, "REFUND", note="Withdrawal rejected")
    if admin_message_id:
        channel = bot.get_channel(bot.config.admin_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(int(admin_message_id))
                embed = message.embeds[0] if message.embeds else discord.Embed(title="Withdrawal")
                embed.add_field(name="Status", value=status, inline=False)
                if tx_hash:
                    embed.add_field(name="txHash", value=tx_hash, inline=False)
                await message.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
    user = bot.get_user(user_id)
    if user:
        try:
            if status == "PAID":
                await user.send(f"Withdrawn {amount:.4f} pDAI → {to_address} (tx: {tx_hash}).")
            else:
                await user.send("Your withdrawal request was rejected and funds were returned.")
        except discord.HTTPException:
            pass


@PharaohBot.tree.command(name="setvc", description="Set the voice channel that grants rewards.")
@require_admin()
@app_commands.describe(voice_channel="Voice channel to monitor")
async def setvc_command(interaction: discord.Interaction, voice_channel: discord.VoiceChannel) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    bot.config.monitored_vc_id = voice_channel.id
    await bot.db.execute(
        "INSERT INTO config(key, value) VALUES('MONITORED_VC_ID', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(voice_channel.id),),
    )
    await interaction.response.send_message(f"Monitored VC set to {voice_channel.mention}.", ephemeral=True)


@PharaohBot.tree.command(name="settext", description="Toggle a text channel for earning.")
@require_admin()
@app_commands.describe(text_channel="Text channel to toggle")
async def settext_command(interaction: discord.Interaction, text_channel: discord.TextChannel) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    channel_ids = set(bot.config.earning_text_channel_ids)
    if text_channel.id in channel_ids:
        channel_ids.remove(text_channel.id)
        action = "removed"
    else:
        channel_ids.add(text_channel.id)
        action = "added"
    bot.config.earning_text_channel_ids = list(channel_ids)
    csv = ",".join(str(cid) for cid in channel_ids)
    await bot.db.execute(
        "INSERT INTO config(key, value) VALUES('EARNING_TEXT_CHANNEL_IDS', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (csv,),
    )
    await interaction.response.send_message(f"Channel {text_channel.mention} {action} for earning.", ephemeral=True)


@PharaohBot.tree.command(name="setrate", description="Update runtime tunables.")
@require_admin()
@app_commands.describe(key="Config key", value="New value")
async def setrate_command(interaction: discord.Interaction, key: str, value: str) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    allowed = {
        "EARNING_VC_PDAI_PER_MIN",
        "EARNING_VC_MAX_MINUTES_PER_DAY",
        "EARNING_CHAT_PDAI_PER_MSG",
        "EARNING_CHAT_DAILY_MSG_CAP",
        "EARNING_CHAT_SIMILARITY_MAX",
    }
    if key not in allowed:
        await interaction.response.send_message("Unknown or restricted key.", ephemeral=True)
        return
    await bot.db.execute(
        "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    mapping = {
        "EARNING_VC_PDAI_PER_MIN": ("vc_rate", float),
        "EARNING_VC_MAX_MINUTES_PER_DAY": ("vc_max_minutes", int),
        "EARNING_CHAT_PDAI_PER_MSG": ("chat_rate", float),
        "EARNING_CHAT_DAILY_MSG_CAP": ("chat_daily_cap", int),
        "EARNING_CHAT_SIMILARITY_MAX": ("chat_similarity_max", float),
    }
    attr, caster = mapping[key]
    setattr(bot.config, attr, caster(value))
    await interaction.response.send_message(f"{key} updated to {value}", ephemeral=True)


@PharaohBot.tree.command(name="grant", description="Owner: grant pDAI to a user.")
@require_owner()
@app_commands.describe(user="User to grant", amount="Amount of pDAI")
async def grant_command(interaction: discord.Interaction, user: discord.User, amount: float) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    await bot.economy.add_ledger_entry(user.id, amount, "GRANT", note=f"Manual grant by {interaction.user}")
    await interaction.response.send_message(f"Granted {amount} pDAI to {user.display_name}.", ephemeral=True)


@PharaohBot.tree.command(name="revoke", description="Owner: revoke pDAI from a user.")
@require_owner()
@app_commands.describe(user="User", amount="Amount to revoke")
async def revoke_command(interaction: discord.Interaction, user: discord.User, amount: float) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    await bot.economy.add_ledger_entry(user.id, -amount, "REVOKE", note=f"Manual revoke by {interaction.user}")
    await interaction.response.send_message(f"Revoked {amount} pDAI from {user.display_name}.", ephemeral=True)


@PharaohBot.tree.command(name="proof", description="Submit proof of a deposit transaction (HD fallback only).")
@app_commands.describe(txhash="Transaction hash")
async def proof_command(interaction: discord.Interaction, txhash: str) -> None:
    bot: PharaohBot = interaction.client  # type: ignore
    if bot.config.cryptoapis_key:
        await interaction.response.send_message("Proof command is only available for HD fallback.", ephemeral=True)
        return
    await ensure_interaction_response(interaction)
    txhash = txhash.strip()
    row = await bot.db.fetchone(
        "SELECT address FROM deposit_addresses WHERE discord_id = ? AND status='ACTIVE'",
        (str(interaction.user.id),),
    )
    if not row:
        await interaction.followup.send("No active deposit address found.", ephemeral=True)
        return
    address = row["address"].lower()
    try:
        tx_data = await bot.fetch_blockscout_json({"module": "proxy", "action": "eth_getTransactionByHash", "txhash": txhash})
        if "result" not in tx_data or tx_data["result"] is None:
            await interaction.followup.send("Transaction not found.", ephemeral=True)
            return
        tx = tx_data["result"]
        if tx.get("to", "").lower() != address:
            await interaction.followup.send("Transaction was not sent to your deposit address.", ephemeral=True)
            return
        receipt = await bot.fetch_blockscout_json({"module": "proxy", "action": "eth_getTransactionReceipt", "txhash": txhash})
        result_receipt = receipt.get("result")
        if not result_receipt or result_receipt.get("blockNumber") is None:
            await interaction.followup.send("Transaction pending. Try again later.", ephemeral=True)
            return
        block_number = int(result_receipt["blockNumber"], 16)
        latest_block = await bot.fetch_blockscout_json({"module": "proxy", "action": "eth_blockNumber"})
        latest_number = int(latest_block.get("result", "0x0"), 16)
        confirmations = latest_number - block_number
        if confirmations < bot.config.confirmations_required:
            await interaction.followup.send(
                f"Needs {bot.config.confirmations_required} confirmations (currently {confirmations}).",
                ephemeral=True,
            )
            return
        transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        dest_topic = "0x" + address.replace("0x", "").rjust(64, "0")
        credited_amount = None
        for log in result_receipt.get("logs", []):
            topics = [t.lower() for t in log.get("topics", [])]
            if not topics or topics[0] != transfer_sig:
                continue
            if len(topics) < 3:
                continue
            if topics[2] != dest_topic:
                continue
            credited_amount = int(log.get("data", "0x0"), 16) / (10 ** 18)
            break
        if credited_amount is None:
            await interaction.followup.send("Could not parse deposit amount from transaction.", ephemeral=True)
            return
    except Exception as exc:
        logging.error("Proof check failed: %s", exc)
        await interaction.followup.send("Failed to verify transaction.", ephemeral=True)
        return
    await bot.db.execute(
        "UPDATE deposit_addresses SET status='USED', used_at=? WHERE address=?",
        (utcnow().isoformat(), row["address"]),
    )
    await bot.economy.add_ledger_entry(
        interaction.user.id,
        credited_amount,
        "DEPOSIT",
        note=f"Manual proof {txhash}",
        ref=txhash,
    )
    await interaction.followup.send("Deposit credited!", ephemeral=True)




@PharaohBot.listen("on_message")
async def handle_message(message: discord.Message) -> None:
    bot: PharaohBot = message._state._get_client()  # type: ignore
    if not isinstance(bot, PharaohBot):
        return
    if message.author.bot or not message.guild or message.guild.id != bot.config.guild_id:
        return
    if bot.config.earning_text_channel_ids and message.channel.id not in set(bot.config.earning_text_channel_ids):
        return
    content = message.content.strip()
    if not content:
        return
    account_age = utcnow() - message.author.created_at.replace(tzinfo=dt.timezone.utc)
    if account_age < dt.timedelta(days=7):
        await send_temp_reply(message, "Account must be at least 7 days old to earn chat rewards.")
        return
    member = message.guild.get_member(message.author.id)
    if member and member.joined_at:
        join_age = utcnow() - member.joined_at.replace(tzinfo=dt.timezone.utc)
        if join_age < dt.timedelta(days=3):
            await send_temp_reply(message, "You must be in the server for 3 days to earn chat rewards.")
            return
    key = (message.author.id, message.channel.id)
    now = utcnow()
    last_time = bot.chat_cooldowns.get(key)
    if last_time and (now - last_time).total_seconds() < bot.config.chat_cooldown_seconds:
        remaining = bot.config.chat_cooldown_seconds - int((now - last_time).total_seconds())
        await send_temp_reply(message, f"Cooldown active. Try again in {remaining} seconds.")
        return
    long_enough = len(content) >= bot.config.chat_min_chars
    sentences = sum(content.count(c) for c in ".!?")
    if not long_enough and sentences < 2:
        await send_temp_reply(message, "Message too short. Aim for ≥120 characters or 2 sentences.")
        return
    await bot.economy.ensure_user(message.author.id)
    count_today = await bot.economy.todays_chat_count(message.author.id)
    if count_today >= bot.config.chat_daily_cap:
        await send_temp_reply(message, "Daily chat earning cap reached.")
        return
    previous = await bot.economy.last_messages(message.author.id)
    for prev in previous:
        ratio = difflib.SequenceMatcher(None, prev.lower(), content.lower()).ratio()
        if ratio >= bot.config.chat_similarity_max:
            await send_temp_reply(message, "Message too similar to recent ones. Mix it up!")
            return
    bot.chat_cooldowns[key] = now
    await bot.economy.record_chat_message(message.author.id, message.channel.id, content)
    await bot.economy.add_ledger_entry(
        message.author.id,
        bot.config.chat_rate,
        "CHAT_EARN",
        note=f"Message {message.channel.id}:{message.id}",
    )
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass


async def send_temp_reply(message: discord.Message, text: str) -> None:
    try:
        await message.reply(text, delete_after=15)
    except discord.HTTPException:
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
    config = BotConfig.from_env()
    if not config.token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")
    if not config.owner_id or not config.admin_channel_id or not config.guild_id:
        raise RuntimeError("OWNER_DISCORD_ID, ADMIN_CHANNEL_ID, and GUILD_ID must be set")
    db = Database(config.database_path)
    price_cache = PriceCache(config.price_asset_id, config.price_cache_seconds)
    bot = PharaohBot(config, db, price_cache)
    bot.run(config.token)


if __name__ == "__main__":
    main()
