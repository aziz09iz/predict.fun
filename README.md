# Predict.fun Paper Bot

Bot **paper trading** untuk market **Up/Down 5 menit** di [predict.fun](https://predict.fun/)
untuk **BTC, ETH, dan BNB**, terintegrasi **Telegram**.

Dibuat *like a real*: harga **open/close** dari data resmi predict.fun (Chainlink
`startPrice`/`endPrice`), order **fill** dari order book live (via `x-api-key`),
dan fee market asli (`feeRateBps`). Tidak ada odds flat — PnL 100% dari fill real.

## Cara kerja (sederhana)

Setiap 5 menit ada market baru. Untuk setiap window:

1. **Open price** = `startPrice` Chainlink milik predict.fun.
2. Di detik **`time_entry[0]`** setelah open, bot lihat harga live (Binance mid —
   sumber yang sama dengan Chainlink):
   - harga di **atas** open → entry **UP**
   - harga di **bawah** open → entry **DOWN**
3. Kalau harga fill-nya lebih mahal dari **`entry_max_price`**, bot menunggu re-quote
   sampai **`time_entry[1]`**; lewat batas itu entry di-skip (seperti limit order hangus).
4. **`exit_on_reversal`** (opsional): kalau harga balik melewati open price melawan
   posisi sebelum close, posisi dijual di harga pasar saat itu.
5. Saat close: settlement pakai `endPrice` Chainlink — UP menang bila close > open,
   DOWN menang bila sebaliknya, sama persis → push.
6. **Jumlah share** = `stake / fill_price`. Menang: share di-redeem $1 (dikurangi fee).
   Kalah: share hangus (−stake). Contoh: stake $1, fill 52% → 1.923 share; menang
   ≈ +$0.92 (setelah fee 2%), kalah −$1.

## Config

Semua di `config.json` (lihat `config.example.json`) dan bisa diubah live via Telegram
(`/set`). Asal nilai tampil di `/config` (default/file/env/telegram).

| Kunci | Default | Arti (bahasa sederhana) |
|---|---|---|
| `telegram.token` / `telegram.chat_id` | — | token bot & chat admin (boleh via env) |
| `telegram.style` | `HTML` | format pesan: `HTML` atau `MarkdownV2` |
| `mode` | `paper` | paper (simulasi); live belum tersedia |
| `trading.enabled` | `true` | hidup/mati trading |
| `trading.coins` | `btc,eth,bnb` | coin yang ditradingkan |
| `trading.stake` | `1.0` | USDT per trade |
| `trading.min_balance` | `0.0` | berhenti entry jika saldo di bawah ini |
| `time_entry` | `[1, 10]` | **jendela entry**: `[mulai, batas]` detik setelah open |
| `entry_max_price` | `0.55` | harga fill maksimal 0–1 (`1.0` = nonaktif) |
| `exit_on_reversal` | `true` | close posisi saat harga balik melewati open |
| `per_coin.<coin>.*` | lihat bawah | override khusus per coin (time_entry / entry_max_price / exit_on_reversal) |
| `notify.*` | — | level notifikasi + rekap harian otomatis |
| `api.key` / `api.real_fill` | — | API key predict.fun (wajib untuk fill real) |
| `api.rate_limits` | basic tier | limit request (250/min general, dll.) |
| `initial_balance` | `100.0` | saldo awal simulasi |

Contoh config strategi (hasil backtest 1 hari, lihat bagian Backtest):

```json
"time_entry": [210, 215],
"entry_max_price": 1.0,
"exit_on_reversal": false
```

> **Config lama masih dikenali**: jika `config.json` lama memakai
> `entry_delay_seconds`/`entry_delay_max_seconds`, bot otomatis menerjemahkannya ke
> `time_entry`.

### Setting per coin

Setiap coin punya karakter berbeda, jadi `time_entry`, `entry_max_price`, dan
`exit_on_reversal` bisa diatur **khusus per coin** lewat `per_coin` (nilai di sini
meng-override nilai global; key yang tidak diisi ikut nilai global):

```json
"per_coin": {
  "btc": {"time_entry": [45, 165], "entry_max_price": 0.65, "exit_on_reversal": false},
  "eth": {"time_entry": [10, 15], "entry_max_price": 0.65, "exit_on_reversal": false},
  "bnb": {"time_entry": [20, 35], "entry_max_price": 0.52, "exit_on_reversal": false}
}
```

Nilai default di atas = **config terbaik per coin** hasil backtest 1 hari (2026-11-11):
BTC +$29.43, ETH +$19.02, BNB +$13.03 per hari (stake $1) — total +$61.48, lebih baik
dari satu config global untuk semua coin (+$46.11). Ubah via Telegram, contoh:

```
/set per_coin.btc.time_entry 45,165
/set per_coin.eth.entry_max_price 0.65
/set per_coin.bnb.exit_on_reversal off
```

Hasil efektif per coin tampil di `/config` (bagian "Per coin").

## Perintah Telegram

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
| `/stats` | statistik lengkap (per pair, ROI, streak) |
| `/history [N]` | riwayat trade (default 20) |
| `/summary` | rekap harian (hari sebelumnya) |
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
| `/set <kunci> <nilai>` | ubah konfigurasi (daftar kunci: kirim /set) |

Kunci `/set` utama:

```
trading.enabled        → on|off
trading.coins          → btc,eth,bnb
trading.stake          → USDT per trade (> 0)
trading.min_balance    → saldo minimum (>= 0)
time_entry             → start,end (contoh: 1,10)
entry_max_price        → angka 0..1 (1.0 = nonaktif)
exit_on_reversal       → on|off
per_coin.<coin>.*      → override per coin (contoh: per_coin.btc.time_entry 45,165)
telegram.style         → HTML | MarkdownV2
notify.*               → level/startup/daily_summary/daily_summary_time
api.real_fill          → on|off (butuh api.key)
mode                   → paper | live (live belum tersedia)
```

## Backtest

Folder `backtest/` berisi script & data backtest — **lokal saja** (tidak di-push ke
GitHub, sudah di `.gitignore`).

Isi:
- `download_data.py` — unduh tick 1 detik Binance (data.binance.vision, publik).
- `backtest.py` — simulasi strategi + parameter sweep (450 kombinasi).
- `data/` — CSV tick 1s BTC/ETH/BNB (1 hari = 86.400 detik per coin).
- `report.md` + `results.csv` — hasil sweep.

Cara pakai:

```bash
cd backtest
python download_data.py 2026-11-11   # tanggal bebas (YYYY-MM-DD)
python backtest.py 2026-11-11
```

Hasil 1 hari (2026-11-11, stake $1, fee 2% + spread 2 sen):

| Config | Trades | Winrate | PnL/hari |
|---|---|---|---|
| `time_entry [210,215]`, mp 1.0, exit off | 821 | 86% | **+$46.11** |
| `time_entry [45,50]`, mp 1.0, exit off | 854 | 66% | +$37.34 |

Pola yang terbaca: **entry menit ke-3.5 mengikuti sisi unggul** (trend persistence —
sisi unggul di detik 210 menang ~86–89% di data) dan **tanpa early-exit**. Config
`entry_max_price 0.55` + entry awal (detik 5) justru **terburuk (−$37..−$43/hari)** —
persis kombinasi yang barusan jelek di VPS live kamu.

**Catatan jujur**: fill dimodelkan dari volatilitas + spread asumsi (order book
historis predict.fun tidak tersedia), jadi angka ini indikasi arah — validasi live
dengan stake kecil dulu. Detail asumsi: `backtest/report.md`.

## Deploy dengan systemd (Linux server)

```bash
sudo mkdir -p /opt/predictfun
sudo cp -r . /opt/predictfun
cd /opt/predictfun
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp config.example.json config.json && sudo editor config.json   # isi token + api.key

sudo useradd -r -s /usr/sbin/nologin predictfun 2>/dev/null || true
sudo cp deploy/predictfun.service /etc/systemd/system/predictfun.service
sudo systemctl daemon-reload
sudo systemctl enable --now predictfun
tail -f /opt/predictfun/data/bot.log
```

Update: `cd /opt/predictfun && git pull && sudo systemctl restart predictfun`.

## Catatan teknis

- **Slug market**: `<coin>-updown-5m-<open_ts>` (waktu **buka**), contoh
  `btc-updown-5m-1794397200`. Open/close diambil dari halaman publik predict.fun
  (tanpa API key) — embed `marketData` berisi `startPrice`/`endPrice` + numeric
  `marketId` yang dipakai endpoint `/v1/markets/{id}/quote`.
- **User-Agent browser**: `api.predict.fun` di belakang Cloudflare (error 1010) memblokir
  UA bot, jadi client memakai signature browser desktop.
- **Settlement**: menunggu `endPrice` terbit sampai `SETTLE_GRACE` (90s); fallback ke
  Binance mid saat close.
- **Restart aman**: posisi OPEN dimuat ulang; yang sudah lewat grace ditandai `UNRESOLVED`.
- **`time_entry` & resolusi**: window 5m = `[t0, t0+300)`. `time_entry[1]` maksimal 299
  supaya selalu ada waktu sebelum close.