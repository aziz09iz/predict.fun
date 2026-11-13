# Backtest — predict.fun 5m Up/Down (BTC/ETH/BNB)

Folder ini **publik**, kecuali `data/` (gitignore — data tumbuh ±20 MB/hari).

## Status saat ini: MENGUMPULKAN DATA REAL

Order book historis predict.fun **tidak tersedia** (API hanya menyediakan snapshot
terkini; timeseries `chance` praktis kosong — dicek langsung via OpenAPI spec
`dev.predict.fun`). Satu-satunya jalan mendapat data real: **merekam sendiri**.

Backtest dengan model probabilitas (Φ + spread asumsi) sudah pernah dibuat dan
**terbukti overfit 1 hari** — hasil live VPS (12 Nov: −$9.27, winrate 52.6% vs
prediksi 61-67%) membuktikannya. Detail verifikasi ada di riwayat git
(commit `46638d4`). Karena itu pipeline backtest lama dihapus dan akan **ditulis
ulang berbasis data fill REAL** setelah perekaman selesai.

## Recorder — `record_book.py`

Merekam data real per window 5 menit (btc/eth/bnb):

1. Resolve `marketId` tiap window via `/v1/series/{coin}-updown-5m/rounds`.
2. Subscribe `predictTopOfBook/{marketId}` di `wss://ws.predict.fun/ws` — **push
   setiap book berubah** (event-driven, bukan polling). Heartbeat di-echo otomatis.
3. Harga underlying (Binance bookTicker WS, mid bid-ask) dicatat di kolom `price`.
4. Sekali per window: snapshot **full-depth** via REST `/v1/markets/{id}/orderbook`.
5. Fallback otomatis ke REST batch-quotes 1×/detik jika WS gagal.

```bash
# di VPS (tmux / systemd), jalankan terus:
PREDICTFUN_API_KEY=... python record_book.py            # default btc,eth,bnb
PREDICTFUN_API_KEY=... python record_book.py btc,bnb    # pilih coin
```

Output di `data/` per coin/hari:

```
predictfun-book-{coin}-{YYYY-MM-DD}.csv
  ts_ms, sec, market_id, bid, ask, bid_size, ask_size, bid_down, ask_down, price
  (bid/ask = sisi UP/Yes; bid_down = 1-bid, ask_down = 1-ask; price = mid Binance)
predictfun-orderbook-{coin}-{YYYY-MM-DD}.jsonl   (full-depth, di detik 0)
```

Rate: WS push tanpa polling; fallback 1 req/detik ≈ 60 req/menit (limit 250/menit).

## Setelah data terkumpul (rencana)

1. `data/` berisi fill REAL (bid/ask per detik) + resolusi (close vs open dari
   Binance 1s — bisa diunduh ulang kapan pun).
2. Backtest ditulis ulang: entry di `time_entry[0]` pakai **ask real** untuk arah
   yang dipilih, exit di **bid real**, fee 2% dari proceeds — tanpa model.
3. Sweep ulang `per_coin` config + validasi IS/OOS seperti sebelumnya, kali ini
   hasilnya mencerminkan order book sebenarnya.
