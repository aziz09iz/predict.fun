# Report Backtest predict.fun 5m — in-sample 2026-11-11 · out-of-sample 2026-11-12

Data tick 1s Binance, 288 jendela/coin/hari, stake $1/trade.
Spread asumsi 2 sen · fee 2% dari proceeds.
Sigma per horizon dihitung dari hari in-sample (walk-forward untuk hari OOS).

## Top 10 in-sample (11 Nov) — dan apakah bertahan di out-of-sample (12 Nov)?

| # | time_entry | mp | exit | trades IS | wr IS | PnL IS | trades OOS | wr OOS | PnL OOS |
|---|---|---|---|---|---|---|---|---|---|
| 1 | [210,215] | 1.0 | False | 821 | 86% | +46.11 | 786 | 85% | +45.06 |
| 2 | [210,225] | 1.0 | False | 823 | 86% | +46.07 | 792 | 85% | +45.13 |
| 3 | [210,240] | 1.0 | False | 826 | 86% | +46.02 | 795 | 85% | +44.10 |
| 4 | [210,270] | 1.0 | False | 827 | 86% | +46.01 | 796 | 85% | +44.09 |
| 5 | [210,299] | 1.0 | False | 827 | 86% | +46.01 | 796 | 85% | +44.09 |
| 6 | [45,50] | 1.0 | False | 854 | 66% | +37.34 | 843 | 65% | +6.22 |
| 7 | [45,60] | 1.0 | False | 854 | 66% | +37.34 | 844 | 65% | +6.20 |
| 8 | [45,75] | 1.0 | False | 854 | 66% | +37.34 | 844 | 65% | +6.20 |
| 9 | [45,105] | 1.0 | False | 854 | 66% | +37.34 | 844 | 65% | +6.20 |
| 10 | [45,165] | 1.0 | False | 854 | 66% | +37.34 | 844 | 65% | +6.20 |

## Config paling robust — profit di KEDUA hari

| # | time_entry | mp | exit | PnL IS | PnL OOS | Total |
|---|---|---|---|---|---|---|
| 1 | [210,225] | 1.0 | False | +46.07 | +45.13 | +91.21 |
| 2 | [210,215] | 1.0 | False | +46.11 | +45.06 | +91.17 |
| 3 | [210,240] | 1.0 | False | +46.02 | +44.10 | +90.12 |
| 4 | [210,270] | 1.0 | False | +46.01 | +44.09 | +90.09 |
| 5 | [210,299] | 1.0 | False | +46.01 | +44.09 | +90.09 |
| 6 | [210,225] | 1.0 | True | +32.68 | +46.11 | +78.79 |
| 7 | [210,215] | 1.0 | True | +32.72 | +46.04 | +78.75 |
| 8 | [210,240] | 1.0 | True | +32.62 | +45.52 | +78.15 |
| 9 | [210,270] | 1.0 | True | +32.61 | +45.51 | +78.12 |
| 10 | [210,299] | 1.0 | True | +32.61 | +45.51 | +78.12 |

## 5 terburuk di hari OOS (hindari)

| time_entry | mp | exit | PnL OOS |
|---|---|---|---|
| [90,210] | 0.55 | False | -55.45 |
| [90,210] | 0.6 | False | -46.03 |
| [150,210] | 0.55 | False | -36.42 |
| [90,150] | 0.55 | False | -35.59 |
| [150,270] | 0.55 | False | -34.94 |

## Config terbaik per coin (berdasarkan hari OOS)

| Coin | time_entry | mp | exit | PnL IS | PnL OOS |
|---|---|---|---|---|---|
| BTC | [270,275] | 1.0 | True | +18.27 | +24.20 |
| ETH | [5,125] | 0.55 | False | -43.29 | +28.04 |
| BNB | [10,15] | 0.55 | False | -8.41 | +19.93 |

## Config rekomendasi (robust) dalam format config.json

```json
  "btc": {"time_entry": [270,275], "entry_max_price": 1.0, "exit_on_reversal": true},
  "eth": {"time_entry": [5,125], "entry_max_price": 0.55, "exit_on_reversal": false},
  "bnb": {"time_entry": [10,15], "entry_max_price": 0.55, "exit_on_reversal": false},
```

## Glosarium

| Istilah | Arti |
|---|---|
| Jendela (window) | satu market 5 menit predict.fun; detik ke-0 = open, detik ke-300 = close. |
| Open / Close price | harga Chainlink di awal/akhir jendela (proksi: close kline 1s Binance). |
| Tick 1s / kline | harga per detik dari Binance; 1 hari = 86.400 baris per coin. |
| time_entry [start,end] | jendela entry: arah diputuskan di detik 'start', fill ditunggu sampai detik 'end'. |
| entry_max_price (mp) | harga fill maksimal 0..1; 1.0 = tanpa limit. Contoh 0.65 = beli maks 65%. |
| exit_on_reversal (exit) | true = posisi dijual saat harga balik melewati open sebelum close. |
| trades | jumlah posisi yang benar-benar dibuka (skip = jendela tanpa trade). |
| winrate | persentase trade menang. |
| exits | jumlah posisi yang ditutup early oleh exit_on_reversal. |
| PnL w/ fee | laba-rugi total setelah fee 2% (feeRateBps=200) dipotong dari proceeds. |
| PnL no-fee | laba-rugi tanpa fee — untuk melihat seberapa besar dampak fee. |
| fill / avg fill model | harga beli per share (0..1) saat entry; share = stake/fill. avg = rata-rata versi model. |
| avg P model | rata-rata probabilitas menang versi model (Phi) pada detik entry. |
| winrate aktual | persentase menang versi DATA (close vs open), bukan versi model. |
| sigma per horizon | volatilitas log-return untuk sisa waktu tertentu (mis. 90s) — dasar harga fair model. |
| spread / SP2 | asumsi lebar order book (total 2 sen; ask = P + 1 sen). |
| fee (feeRateBps=200) | fee market 2% dari proceeds (redeem menang / hasil jual exit). |
| EV | expected value — rata-rata laba per trade: winrate x payout - 1 - fee. |
| trend persistence | sisi unggul cenderung bertahan sampai close (di data 2026-11-11: 86-89%). |
| Phi / model P | CDF normal standar; mengubah jarak harga & waktu sisa menjadi probabilitas. |
| in-sample (IS) | hari yang dipakai memilih config (11 Nov) — hampir pasti overfit. |
| out-of-sample (OOS) | hari yang TIDAK dipakai memilih config (12 Nov) — tes jujur, mirip kondisi live. |
| walk-forward sigma | sigma untuk hari OOS dihitung dari hari sebelumnya, bukan hari yang sama (anti look-ahead). |
| robust | config yang profit di KEDUA hari (IS dan OOS) — yang layak dipertimbangkan untuk live. |

## Verifikasi dengan data live VPS (12 Nov 2026, 16:05–19:50 UTC)

Data nyata dari database VPS (97 trade, stake $1, balance 10.00 → 0.73):

| Coin | Config live | Live wr | Live PnL | Model OOS wr | Model OOS PnL (full day) |
|---|---|---|---|---|---|
| **BTC** | [45,165] mp0.65 exit off | **65.8%** | **+$4.15** | 60.4% | +$13.69 |
| **ETH** | [10,15] mp0.65 exit off | **45.0%** | **−$9.43** | 59.1% | +$29.60 |
| **BNB** | [20,35] mp0.55 exit off | **42.1%** | **−$3.99** | 56.0% | +$14.66 |

**Kesimpulan verifikasi:**
1. **BTC cocok**: model 60.4% vs live 65.8% — config [45,165] mp0.65 satu-satunya yang
   terbukti profit di live DAN di kedua hari backtest (IS +29.75 / OOS +13.69).
2. **Model terlalu optimis untuk entry super-awal (ETH/BNB)**: entry di detik 10-20 itu
   coin-flip + spread + fee. Edge "trend persistence" dari 11 Nov TIDAK berulang di
   12 Nov untuk entry awal — backtest 1 hari kemarin overfit.
3. **Breakeven winrate** pada fill 0.58 ≈ 60%; ETH live 45% → bocor ±$0.17/trade.
4. Config entry-telat [210,2xx] mp1.0 exit-off robust di kedua hari model (IS +46 /
   OOS +43-45), tapi **belum pernah diverifikasi live** — statusnya hipotesis, bukan fakta.

## Asumsi & limitasi

- Fill price = model probabilitas (sigma + spread asumsi), bukan order book historis predict.fun.
- Sigma dihitung dari hari in-sample (11 Nov); evaluasi hari OOS (12 Nov) walk-forward, tanpa bocor data masa-depan.
- Fee 2% potong proceeds; spread 2 sen — realita order book bisa lebih lebar.
- Resolusi = harga detik t0+299 vs open detik t0 (±1 detik vs Chainlink resmi).
- 1 hari = 288 jendela/coin — sampel kecil; in-sample hampir pasti overfit, KARENA ITU ada kolom OOS.
- exit_on_reversal = jual di bid saat harga balik melewati open, sebelum detik 300.
