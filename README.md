# Predict.fun Paper Bot

Bot **paper trading** untuk market **Up/Down 5 menit** di [predict.fun](https://predict.fun/)
untuk **BTC, ETH, dan BNB** — terintegrasi penuh dengan **Telegram**.

Simulasikan strategi tanpa uang sungguhan: harga diambil realtime, entry dikalkulasi
otomatis, dan PnL dicatat ke SQLite.

## Fitur

- 🎯 Paper trade semua market 5m Up/Down (BTC / ETH / BNB) — jadwal deterministik, aligned ke menit UTC.
- ⚡ Harga **realtime** dari Binance `bookTicker` (mid price), sumber harga yang sama dengan resolusi Chainlink predict.fun.
- 🤖 Telegram bot: informatif, singkat, friendly, to the point (HTML atau MarkdownV2).
- ⚙️ Ubah konfigurasi langsung dari Telegram (`/set`), tanpa restart.
- 🗄️ SQLite DB (`data/predictbot.db`): riwayat trade, PnL, saldo.
- 📝 Log berputar (`data/bot.log`).
- 🚀 Pesan *start* otomatis saat bot jalan.
- 🐧 Unit systemd siap pakai (`deploy/predictfun.service`).

## Strategi

Untuk setiap jendela 5 menit (open = close − 300s):

1. Catat **open price** tepat saat market buka.
2. Tunggu **+1 detik** (`entry_delay_seconds`, default 1).
3. Jika harga live **di atas** open price → entry **UP**; jika **di bawah** → **DOWN**.
4. Saat close, settlement mengikuti aturan predict.fun:
   - UP menang bila close > open, DOWN menang bila close < open, sama persis → push.

### Model PnL paper

PnL dihitung sederhana & transparan: taruhan `stake` USDT dengan `win_multiplier`
(default **2.0**, fair coin-flip). Menang → `+stake × (multiplier − 1)`, kalah →
`−stake`, push → `0`. Dengan multiplier 2.0, akumulasi saldo murni mengukur *skill* arah strategi.

> Karena paper mode, tidak ada kunci API predict.fun maupun deposit yang dibutuhkan.

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
dari environment (menimpa `config.json`) — berguna agar token tidak tersimpan di file.

## Daftar perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/start` | Daftar (menjadi admin saat belum ada admin) |
| `/help` | Panduan singkat |
| `/status` | Saldo, PnL, posisi terbuka, harga live |
| `/prices` | Harga realtime BTC/ETH/BNB |
| `/config` | Lihat konfigurasi aktif |
| `/set <kunci> <nilai>` | Ubah konfigurasi |
| `/pause` / `/resume` | Jeda / lanjut trading |
| `/history [n]` | Riwayat trade terakhir |
| `/settle` | Paksa settle posisi terbuka sekarang |

Kunci yang bisa diubah lewat `/set`:

```
trading.stake          → jumlah USDT per trade (float > 0)
trading.coins          → subset coin, contoh: btc,eth  (pisahkan koma)
trading.enabled        → on / off
trading.min_balance    → saldo minimum sebelum berhenti buka posisi
payout.win_multiplier  → odds menang (float > 1)
entry_delay_seconds    → delay entry setelah open (0..120)
telegram.style         → HTML atau MarkdownV2
telegram.chat_id       → admin chat id
notify.on_trade        → on / off (notifikasi saat entry)
notify.on_settle       → on / off (notifikasi saat settle)
notify.startup         → on / off (pesan start saat boot)
```

Contoh: `/set trading.stake 25`, `/set trading.coins btc,eth`, `/set trading.enabled off`.

## Struktur proyek

```
bot.py                  # entry point
predictbot/
  config.py             # load/save/validasi config.json
  db.py                 # SQLite (trades, saldo, balance events)
  market.py             # jadwal window & slug market predict.fun
  prices.py             # feed Binance WebSocket (realtime mid price)
  engine.py             # engine paper trading + strategi
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
# 1. taruh project di /opt/predictfun
sudo mkdir -p /opt/predictfun
sudo cp -r . /opt/predictfun
cd /opt/predictfun
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp config.example.json config.json && sudo editor config.json   # isi token

# 2. buat user & pasang service
sudo useradd -r -s /usr/sbin/nologin predictfun 2>/dev/null || true
sudo cp deploy/predictfun.service /etc/systemd/system/predictfun.service
sudo systemctl daemon-reload
sudo systemctl enable --now predictfun
sudo systemctl status predictfun

# log live
tail -f /opt/predictfun/data/bot.log
```

Catatan: sesuaikan `User`, `WorkingDirectory`, dan path di dalam
`deploy/predictfun.service` dengan lokasi sebenarnya. Token Telegram dapat
diletakkan di `Environment=PREDICTFUN_TELEGRAM_TOKEN=...` agar tidak tersimpan
dalam `config.json`, atau lewat systemd drop-in.

## Catatan teknis

- **Sumber harga**: market crypto predict.fun di-resolve terhadap stream Chainlink
  yang memakai *mid* dari top-of-book **Binance**. Bot memakai `bookTicker`
  Binance secara langsung → harga realtime yang persis sama, tanpa API key.
- **Slug market**: `<coin>-updown-5m-<close_ts>`, contoh
  `btc-updown-5m-1794391800`. Tautan deep-link disertakan di setiap notifikasi.
- **Restart aman**: posisi yang masih OPEN dimuat ulang dan disettle normal;
  posisi yang sudah melewati close saat bot mati ditandai `UNRESOLVED` (tanpa PnL).