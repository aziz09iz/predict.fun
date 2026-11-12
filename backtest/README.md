# Backtest — predict.fun 5m Up/Down (BTC/ETH/BNB)

Folder ini **lokal saja** (di `.gitignore`, tidak di-push). Semua script & data
backtest ada di sini.

## Script

| Script | Fungsi |
|---|---|
| `download_data.py` | unduh tick 1s Binance (arsip harian data.binance.vision; fallback REST `api.binance.com` jika arsip belum terbit, tahan gap data). Output: `data/{SYM}-1s-{date}.csv` |
| `backtest.py` | simulasi strategi + sweep parameter. **Dua hari**: in-sample (pilih config) + out-of-sample (tes jujur, walk-forward σ). Output: `results.csv`, `report.md` |
| `record_book.py` | **merekam data REAL predict.fun** via WebSocket `predictTopOfBook/{id}` — push per book-change (event-driven) + harga underlying (Binance bookTicker WS) di kolom `price`, plus full-depth REST snapshot di detik 0. Fallback otomatis ke REST polling 1/s jika WS gagal. Jalankan terus di VPS |
| `eval_live.py` | evaluasi hasil live dari `data/predictbot.db` (upload dari VPS) |

## Temuan ketersediaan data predict.fun (dicek 13 Nov 2026)

- **Order book historis: TIDAK tersedia.** API hanya menyediakan snapshot terkini
  (`/v1/markets/{id}/orderbook`, `/v1/markets/quotes`, `/v1/markets/orderbooks`).
- **Timeseries `metric=chance` (probabilitas implisit)**: endpoint ada di spec, tapi
  praktis kosong (0–1 poin per market). Retensi series/rounds juga cuma ±24 jam ke
  belakang (rounds dipre-create 24h ke depan).
- **Kesimpulan**: satu-satunya cara dapat data real = merekam sendiri mulai sekarang
  → `record_book.py`.

## Recorder (penting!)

```bash
# di VPS (tmux / systemd):
PREDICTFUN_API_KEY=... python record_book.py             # default btc,eth,bnb
PREDICTFUN_API_KEY=... python record_book.py btc,bnb     # pilih coin tertentu

# systemd (opsional):
# [Service] ExecStart=/opt/predictfun/.venv/bin/python record_book.py btc,bnb
# Environment=PREDICTFUN_API_KEY=...
```

Per window 5m: subscribe `predictTopOfBook/{marketId}` di `wss://ws.predict.fun/ws`
(push setiap book berubah — event-driven), harga underlying di kolom `price`
(Binance bookTicker WS), full-depth snapshot di detik 0.
File: `data/predictfun-book-*.csv` + `predictfun-orderbook-*.jsonl`.
Setelah beberapa hari berjalan, backtest bisa diganti dari model Φ ke **harga fill REAL**.

Rate: WS = tanpa polling; fallback REST 1/s ≈ 60 req/menit (limit 250/menit).
Folder `data/` TIDAK di-push (gitignore) — data tumbuh ±20 MB/hari.

## Pemakaian backtest

```bash
python download_data.py 2026-11-11          # + tanggal lain yang perlu
python backtest.py 2026-11-11 2026-11-12    # <in-sample> <out-of-sample>
```

Rule: 1 hari data per evaluasi, coin btc/eth/bnb, stake $1, fee 2% + spread 2 sen.
Pilih config yang **robust** (profit di kedua hari). Baca `report.md` (ada glosarium +
verifikasi model vs data live VPS 12 Nov: hanya BTC [45,165] mp0.65 yang terbukti).
