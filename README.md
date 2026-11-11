# Predict.fun Paper Bot

Bot **paper trading** untuk market **Up/Down 5 menit** di [predict.fun](https://predict.fun/)
untuk **BTC, ETH, dan BNB** — terintegrasi penuh dengan **Telegram**.

Simulasi "like a real": harga **open/close diambil langsung dari data resmi predict.fun**
(Chainlink `startPrice`/`endPrice`), bukan perkiraan lokal. Hasil settlement mengikuti
resolusi predict.fun secara persis.

## Fitur

- 🎯 Paper trade semua market 5m Up/Down (BTC / ETH / BNB) — jadwal deterministik, aligned ke menit UTC.
- ⚡ Harga **realtime** dari Binance `bookTicker` (mid price) — sumber yang sama dengan resolusi Chainlink predict.fun.
- 🎯 Open/close **persis predict.fun**: diambil dari `startPrice`/`endPrice` publik (Chainlink), bukan Binance lokal.
- 🤖 Telegram bot: informatif, singkat, friendly, to the point (HTML atau MarkdownV2).
- ⚙️ Ubah konfigurasi dari Telegram (`/set`) + tampil **asal nilainya** (`/config`).
- 🗄️ SQLite DB (`data/predictbot.db`): riwayat trade, PnL, saldo.
- 📝 Log berputar (`data/bot.log`).
- 🚀 Pesan *start* otomatis saat boot.
- 🐧 Unit systemd (`deploy/predictfun.service`).

## Strategi

Untuk setiap jendela 5 menit (open = `startPricePublishTime`):

1. Catat **open price** = `startPrice` predict.fun (Chainlink).
2. Tunggu **+1 detik** (`entry_delay_seconds`, default 1).
3. Jika harga live **di atas** open → entry **UP**; jika **di bawah** → **DOWN**.
4. Saat close, settlement persis aturan predict.fun:
   - UP menang bila `endPrice` > `startPrice`, DOWN menang bila sebaliknya, sama persis → push.

### Model eksekusi & PnL paper

Bot punya **dua mode** untuk menghitung PnL:

**1. Real fill (dengan `api.key`)** — order di-fill seperti real:
- Ambil **best bid/ask** live via `GET /v1/markets/{id}/quote`.
- Beli UP (Yes) di `bestAsk.price`; beli DOWN (No) di `1 − bestBid.price`.
- Fee diambil dari `feeRateBps` market.
- Menang → `+stake × (1/fill − 1)`, kalah → `−stake`, push → `0`.

**2. Flat (tanpa `api.key`)** — fallback:
- `win_multiplier` (default **1.8**). Menang → `+stake × (multiplier − 1)`, kalah → `−stake`.

> `win_multiplier` ≈ `1 / probabilitas`. 2.0 = fair 50/50; 1.8 ≈ masuk ke sisi favorit.

### Mendapatkan API key (untuk real fill)

1. Buka **Developers → API** di account predict.fun (https://developers.predict.fun).
2. Buat **API key**.
3. Isi di `config.json` (`api.key`) atau env `PREDICTFUN_API_KEY`.
4. Nonaktif-fallback: `api.real_fill` (default `true`).

## Instalasi & menjalankan (lokal)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.json config.json
# isi "telegram.token" dengan token bot dari @BotFather
python bot.py
```

Bot juga membaca `PREDICTFUN_TELEGRAM_TOKEN` dan `PREDICTFUN_TELEGRAM_CHAT_ID`
dari environment (menimpa `config.json`).

## Daftar perintah Telegram

**Trading**
| Perintah | Fungsi |
|---|---|
| `/trade on\|off` | mulai / berhenti trading |
| `/status` | status engine & market |
| `/positions` | posisi terbuka |
| `/sell <id>` | jual / tutup posisi manual |

**Info**
| Perintah | Fungsi |
|---|---|
| `/account` | balance, winrate, PnL hari ini |
| `/history [N]` | riwayat trade (default 20) |
| `/config` | setting efektif + asal nilainya |
| `/mode` | mode saat ini (paper/live) |
| `/prices` | harga realtime BTC/ETH/BNB |

**Notifikasi**
| Perintah | Fungsi |
|---|---|
| `/notify full\|compact\|settle` | level notifikasi |

**Config**
| Perintah | Fungsi |
|---|---|
| `/set <kunci> <nilai>` | ubah konfigurasi |
| `/set` | daftar kunci + contoh |

Kunci yang bisa diubah (`/set`):

```
trading.enabled        → on|off
trading.coins          → btc,eth,bnb
trading.stake          → USDT per trade (> 0)
trading.min_balance    → saldo minimum (>= 0)
payout.win_multiplier  → odds menang (> 1)
entry_delay_seconds    → delay entry setelah open (0..120)
telegram.style         → HTML | MarkdownV2
notify.level           → full | compact | settle
notify.startup         → on|off
api.real_fill          → on|off  (butuh api.key)
mode                   → paper | live  (live belum tersedia)
```

Contoh: `/set trading.stake 25`, `/set trading.coins btc,eth`, `/set notify.level compact`.

## Struktur proyek

```
bot.py                  # entry point
predictbot/
  config.py             # load/save/validasi + origin tracking
  db.py                 # SQLite (trades, saldo, balance events)
  market.py             # jadwal window & slug market (slug = open time)
  predictfun.py         # fetch startPrice/endPrice Chainlink dari halaman market
  prices.py             # feed Binance WebSocket (realtime mid, fallback endpoint)
  engine.py             # engine paper trading + strategi + settlement
  telegram.py           # transport Telegram Bot API (long polling)
  format.py             # formatting HTML / MarkdownV2
  logger.py             # logging ke data/bot.log
config.example.json
requirements.txt
deploy/predictfun.service
data/                   # dibuat otomatis: bot.log, predictbot.db
```

## Deploy dengan systemd (Linux server)

```bash
sudo mkdir -p /opt/predictfun
sudo cp -r . /opt/predictfun
cd /opt/predictfun
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp config.example.json config.json && sudo editor config.json   # isi token

sudo useradd -r -s /usr/sbin/nologin predictfun 2>/dev/null || true
sudo cp deploy/predictfun.service /etc/systemd/system/predictfun.service
sudo systemctl daemon-reload
sudo systemctl enable --now predictfun
sudo systemctl status predictfun

tail -f /opt/predictfun/data/bot.log
```

Update kode di server: `cd /opt/predictfun && git pull && sudo systemctl restart predictfun`.

## Catatan teknis

- **Sumber harga open/close**: diambil dari `startPrice`/`endPrice` (Chainlink) yang
  ter-embed di halaman publik `predict.fun/market/<slug>` — tanpa API key.
- **Sumber harga live**: Binance `bookTicker` mid (sama seperti stream Chainlink yang
  dipakai predict.fun), dengan fallback otomatis antar endpoint.
- **Slug market**: `<coin>-updown-5m-<open_ts>` (waktu **buka**, bukan tutup), contoh
  `btc-updown-5m-1794397200`. Tautan deep-link disertakan di tiap notifikasi.
- **Settlement**: menunggu `endPrice` terbit sampai `SETTLE_GRACE` (90s); jika belum
  ada (mis. CDN lambat), fallback ke Binance mid saat close.
- **Restart aman**: posisi OPEN dimuat ulang; yang sudah lewat grace ditandai `UNRESOLVED`.