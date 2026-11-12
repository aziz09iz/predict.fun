#!/usr/bin/env python
"""Backtest strategi predict.fun 5m Up/Down pakai data tick 1s Binance.

Model (asumsi; order book historis predict.fun tidak tersedia):
  open  = harga detik t0, close = harga detik t0+299  (proksi Chainlink)
  P(UP) di detik s = Phi((price_s - open) / (sigma * sqrt(300 - s)))
  ask_up = P + SP2, bid_up = P - SP2, ask_down = 1 - bid_up, bid_down = 1 - ask_up
  fee 2% (feeRateBps=200) dipotong dari proceeds (redeem menang & hasil jual exit)
Stake dinormalkan $1 per trade.
"""
import csv
import io
import re
import sys
import datetime
from pathlib import Path

import numpy as np
from scipy.special import erf

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
OUT_CSV = HERE / "results.csv"      # hasil sweep (semua config)
OUT_REPORT = HERE / "report.md"     # report markdown
WINDOW = 300
FEE = 0.02      # fee 2% dari proceeds
SPREAD = 0.02   # spread order book (2 sen)
SP2 = SPREAD / 2

SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "bnb": "BNBUSDT"}

STARTS = [1, 5, 10, 20, 45, 90, 150, 210, 270]   # time_entry[0] (detik)
WAITS = [5, 15, 30, 60, 120]                     # end - start (detik)
MAX_PRICES = [0.52, 0.55, 0.60, 0.65, 1.0]
EXITS = [False, True]

# Glosarium — ditulis ke report.md agar semua istilah jelas bagi pemula.
GLOSSARY = [
    ("Jendela (window)", "satu market 5 menit predict.fun; detik ke-0 = open, detik ke-300 = close."),
    ("Open / Close price", "harga Chainlink di awal/akhir jendela (proksi: close kline 1s Binance)."),
    ("Tick 1s / kline", "harga per detik dari Binance; 1 hari = 86.400 baris per coin."),
    ("time_entry [start,end]", "jendela entry: arah diputuskan di detik 'start', fill ditunggu sampai detik 'end'."),
    ("entry_max_price (mp)", "harga fill maksimal 0..1; 1.0 = tanpa limit. Contoh 0.65 = beli maks 65%."),
    ("exit_on_reversal (exit)", "true = posisi dijual saat harga balik melewati open sebelum close."),
    ("trades", "jumlah posisi yang benar-benar dibuka (skip = jendela tanpa trade)."),
    ("winrate", "persentase trade menang."),
    ("exits", "jumlah posisi yang ditutup early oleh exit_on_reversal."),
    ("PnL w/ fee", "laba-rugi total setelah fee 2% (feeRateBps=200) dipotong dari proceeds."),
    ("PnL no-fee", "laba-rugi tanpa fee — untuk melihat seberapa besar dampak fee."),
    ("fill / avg fill model", "harga beli per share (0..1) saat entry; share = stake/fill. avg = rata-rata versi model."),
    ("avg P model", "rata-rata probabilitas menang versi model (Phi) pada detik entry."),
    ("winrate aktual", "persentase menang versi DATA (close vs open), bukan versi model."),
    ("sigma per horizon", "volatilitas log-return untuk sisa waktu tertentu (mis. 90s) — dasar harga fair model."),
    ("spread / SP2", "asumsi lebar order book (total 2 sen; ask = P + 1 sen)."),
    ("fee (feeRateBps=200)", "fee market 2% dari proceeds (redeem menang / hasil jual exit)."),
    ("EV", "expected value — rata-rata laba per trade: winrate x payout - 1 - fee."),
    ("trend persistence", "sisi unggul cenderung bertahan sampai close (di data 2026-11-11: 86-89%)."),
    ("Phi / model P", "CDF normal standar; mengubah jarak harga & waktu sisa menjadi probabilitas."),
    ("in-sample (IS)", "hari yang dipakai memilih config (11 Nov) — hampir pasti overfit."),
    ("out-of-sample (OOS)", "hari yang TIDAK dipakai memilih config (12 Nov) — tes jujur, mirip kondisi live."),
    ("walk-forward sigma", "sigma untuk hari OOS dihitung dari hari sebelumnya, bukan hari yang sama (anti look-ahead)."),
    ("robust", "config yang profit di KEDUA hari (IS dan OOS) — yang layak dipertimbangkan untuk live."),
]

SQRT2 = 1.4142135623730951


def load_price_series(path: Path) -> np.ndarray:
    """CSV kline 1s Binance -> array harga close per detik (86400)."""
    prices = np.zeros(86_400)
    seen = np.zeros(86_400, dtype=bool)
    text = path.read_text(encoding="utf-8")
    for row in csv.reader(text.splitlines()):
            if not row or not row[0].strip():
                continue
            try:
                ot = int(row[0])
                close = float(row[4])
            except (ValueError, IndexError):
                continue
            if ot > 10**15:        # mikrodetik
                sec = ot // 1_000_000
            elif ot > 10**12:      # milidetik
                sec = ot // 1000
            else:                  # detik
                sec = ot
            idx = int(sec % 86_400)
            prices[idx] = close
            seen[idx] = True
    if not seen.any():
        raise ValueError(f"tidak ada data valid di {path.name}")
    last = 0.0
    for i in range(86_400):
        if seen[i]:
            last = prices[i]
        else:
            prices[i] = last
    first_valid = int(np.argmax(seen))
    prices[:first_valid] = prices[first_valid]
    return prices


def sim_window(prices, sig_rem, t0, start_s, end_s, max_price, do_exit, sp2, fee):
    """Simulasi 1 jendela 5m. Return dict hasil, atau None kalau tidak trade."""
    p = prices[t0:t0 + WINDOW]
    open_p = float(p[0])
    if open_p <= 0:
        return None
    sec = np.arange(WINDOW)
    rem = np.maximum(WINDOW - sec, 1)
    x = np.log(p / open_p) / sig_rem[rem - 1]
    P = 0.5 * (1.0 + erf(x / SQRT2))
    P = np.clip(P, 0.01, 0.985)

    i0 = int(start_s)
    if i0 >= WINDOW:
        return None
    p0 = float(p[i0])
    if p0 == open_p:
        return None  # tidak ada sinyal arah
    direction = "UP" if p0 > open_p else "DOWN"

    ask_up = P + sp2
    ask_dir = ask_up if direction == "UP" else (1.0 - (P - sp2))

    lo, hi = i0, min(int(end_s), WINDOW - 1)
    side_ok = (p[lo:hi + 1] > open_p) if direction == "UP" else (p[lo:hi + 1] < open_p)
    seg = ask_dir[lo:hi + 1]
    ok = np.nonzero((seg <= max_price) & (seg > 0.0) & (seg < 1.0) & side_ok)[0]
    if ok.size == 0:
        return None
    e_i = int(lo + ok[0])
    fill = float(ask_dir[e_i])
    shares = 1.0 / fill

    # exit_on_reversal: jual di bid saat harga balik melewati open
    if do_exit:
        cross = (p < open_p) if direction == "UP" else (p > open_p)
        after = np.nonzero(cross[e_i + 1:])[0]
        if after.size:
            x_i = int(e_i + 1 + after[0])
            bid_dir = (P - sp2) if direction == "UP" else (1.0 - (P + sp2))
            exit_price = float(bid_dir[x_i])
            if 0.0 < exit_price < 1.0:
                pnl = shares * exit_price * (1.0 - fee) - 1.0
                return {
                    "kind": "exit", "outcome": "WIN" if pnl > 0 else "LOSS",
                    "fill": fill, "exit_price": exit_price,
                    "pnl_fee": pnl, "pnl_nofee": shares * exit_price - 1.0,
                }

    close_p = float(p[WINDOW - 1])
    if close_p == open_p:
        return {"kind": "settle", "outcome": "PUSH", "fill": fill,
                "pnl_fee": 0.0, "pnl_nofee": 0.0}
    won = (close_p > open_p) == (direction == "UP")
    if won:
        return {"kind": "settle", "outcome": "WIN", "fill": fill,
                "pnl_fee": shares * (1.0 - fee) - 1.0, "pnl_nofee": shares - 1.0}
    return {"kind": "settle", "outcome": "LOSS", "fill": fill,
            "pnl_fee": -1.0, "pnl_nofee": -1.0}


def run_config(coins, sig_rem, start_s, end_s, max_price, do_exit):
    """Jalankan satu konfigurasi di semua coin & semua jendela 5m."""
    agg = {"trades": 0, "wins": 0, "losses": 0, "pushes": 0, "exits": 0,
           "pnl_fee": 0.0, "pnl_nofee": 0.0, "per_coin": {}}
    for coin, prices in coins.items():
        st = {"trades": 0, "wins": 0, "losses": 0, "pushes": 0, "exits": 0, "pnl_fee": 0.0}
        for t0 in range(0, 86_400 - WINDOW + 1, WINDOW):
            r = sim_window(prices, sig_rem[coin], t0, start_s, end_s, max_price, do_exit, SP2, FEE)
            if r is None:
                continue
            st["trades"] += 1
            st["wins"] += r["outcome"] == "WIN"
            st["losses"] += r["outcome"] == "LOSS"
            st["pushes"] += r["outcome"] == "PUSH"
            st["exits"] += r["kind"] == "exit"
            st["pnl_fee"] += r["pnl_fee"]
            agg["pnl_nofee"] += r["pnl_nofee"]
        agg["per_coin"][coin] = st
        agg["trades"] += st["trades"]
        agg["wins"] += st["wins"]
        agg["losses"] += st["losses"]
        agg["pushes"] += st["pushes"]
        agg["exits"] += st["exits"]
        agg["pnl_fee"] += st["pnl_fee"]
    return agg


def sweep(coins, sig_rem):
    """Jalankan seluruh grid pada satu hari data. Return dict per config-id."""
    out = {}
    for start in STARTS:
        for wait in WAITS:
            end = min(start + wait, 299)
            if end <= start:
                continue
            for mp in MAX_PRICES:
                for do_exit in EXITS:
                    key = f"{start},{end}|mp{mp}|exit{int(do_exit)}"
                    s = run_config(coins, sig_rem, start, end, mp, do_exit)
                    out[key] = {
                        "time_entry": f"{start},{end}",
                        "entry_max_price": mp,
                        "exit_on_reversal": do_exit,
                        "trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
                        "pushes": s["pushes"], "exits": s["exits"],
                        "pnl_fee": s["pnl_fee"], "pnl_nofee": s["pnl_nofee"],
                        "per_coin": s["per_coin"],
                    }
    return out


def _check_day(day: str) -> str:
    """Terima hanya YYYY-MM-DD (anti path-injection dari argv)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError(f"format tanggal tidak valid: {day!r}")
    datetime.date.fromisoformat(day)
    return day


def _safe_path(base: Path, name: str) -> Path:
    """Gabung base+nama file dengan cek containment (anti path-traversal)."""
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"nama file tidak aman: {name!r}")
    p = (base / name).resolve()
    if base not in p.parents:
        raise ValueError(f"path keluar dari {base}: {p}")
    return p


def main() -> int:
    day_is = _check_day(sys.argv[1] if len(sys.argv) > 1 else "2026-11-11")
    day_oos = _check_day(sys.argv[2] if len(sys.argv) > 2 else "2026-11-12")

    def load_day(day):
        coins = {}
        for coin, sym in SYMBOLS.items():
            path = _safe_path(DATA_DIR, f"{sym}-1s-{day}.csv")
            if not path.exists():
                print(f"data {sym} {day} tidak ada — jalankan download_data.py {day} dulu")
                return None
            coins[coin] = load_price_series(path)
        return coins

    prices_is = load_day(day_is)
    prices_oos = load_day(day_oos)
    if prices_is is None or prices_oos is None:
        return 1

    # sigma per horizon dari HARI IN-SAMPLE. Untuk hari OOS ini adalah
    # walk-forward yang bersih (sigma hari-12 diambil dari data hari-11 —
    # tidak ada data masa-depan yang bocor).
    sig = {}
    for coin in SYMBOLS:
        pr = prices_is[coin]
        s = np.zeros(WINDOW)
        for tau in range(1, WINDOW + 1):
            r = np.log(pr[tau:] / pr[:-tau])
            s[tau - 1] = float(np.std(r))
        sig[coin] = s
    res_is = sweep(prices_is, sig)
    res_oos = sweep(prices_oos, sig)

    merged = []
    for key, a in res_is.items():
        b = res_oos.get(key)
        if b is None:
            continue
        merged.append({
            **a,
            "trades_oos": b["trades"], "wins_oos": b["wins"], "exits_oos": b["exits"],
            "pnl_oos": b["pnl_fee"], "pnl_nofee_oos": b["pnl_nofee"],
            "per_coin_oos": b["per_coin"],
        })

    top_is = sorted(merged, key=lambda r: r["pnl_fee"], reverse=True)
    robust = [r for r in merged if r["pnl_fee"] > 0 and r["pnl_oos"] > 0]
    robust.sort(key=lambda r: (r["pnl_fee"] + r["pnl_oos"]), reverse=True)
    worst_oos = sorted(merged, key=lambda r: r["pnl_oos"])[:5]

    # config terbaik per coin berdasarkan HARI OOS (hari yang menyerupai live)
    best_per_coin = {}
    for coin in SYMBOLS:
        c_rows = [r for r in merged if r["per_coin_oos"].get(coin, {}).get("trades", 0) > 0]
        if c_rows:
            best_per_coin[coin] = max(c_rows, key=lambda r: r["per_coin_oos"][coin]["pnl_fee"])

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["time_entry", "entry_max_price", "exit_on_reversal",
                "trades_is", "wr_is_pct", "pnl_is",
                "trades_oos", "wr_oos_pct", "pnl_oos", "pnl_total"])
    for r in merged:
        wr_is = r["wins"] / r["trades"] * 100 if r["trades"] else 0.0
        wr_oos = r["wins_oos"] / r["trades_oos"] * 100 if r["trades_oos"] else 0.0
        w.writerow([f"[{r['time_entry']}]", r["entry_max_price"], r["exit_on_reversal"],
                    r["trades"], f"{wr_is:.1f}", f"{r['pnl_fee']:+.4f}",
                    r["trades_oos"], f"{wr_oos:.1f}", f"{r['pnl_oos']:+.4f}",
                    f"{r['pnl_fee'] + r['pnl_oos']:+.4f}"])
    OUT_CSV.write_text(buf.getvalue(), encoding="utf-8")

    L = []
    L.append(f"# Report Backtest predict.fun 5m — in-sample {day_is} · out-of-sample {day_oos}")
    L.append("")
    L.append("Data tick 1s Binance, 288 jendela/coin/hari, stake $1/trade.")
    L.append(f"Spread asumsi {SPREAD*100:.0f} sen · fee {FEE*100:.0f}% dari proceeds.")
    L.append("Sigma per horizon dihitung dari hari in-sample (walk-forward untuk hari OOS).")
    L.append("")
    L.append("## Top 10 in-sample (11 Nov) — dan apakah bertahan di out-of-sample (12 Nov)?")
    L.append("")
    L.append("| # | time_entry | mp | exit | trades IS | wr IS | PnL IS | trades OOS | wr OOS | PnL OOS |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(top_is[:10], 1):
        wr_i = r["wins"] / r["trades"] * 100 if r["trades"] else 0.0
        wr_o = r["wins_oos"] / r["trades_oos"] * 100 if r["trades_oos"] else 0.0
        L.append(f"| {i} | [{r['time_entry']}] | {r['entry_max_price']} | {r['exit_on_reversal']} | "
                 f"{r['trades']} | {wr_i:.0f}% | {r['pnl_fee']:+.2f} | "
                 f"{r['trades_oos']} | {wr_o:.0f}% | {r['pnl_oos']:+.2f} |")
    L.append("")
    L.append("## Config paling robust — profit di KEDUA hari")
    L.append("")
    L.append("| # | time_entry | mp | exit | PnL IS | PnL OOS | Total |")
    L.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(robust[:10], 1):
        L.append(f"| {i} | [{r['time_entry']}] | {r['entry_max_price']} | {r['exit_on_reversal']} | "
                 f"{r['pnl_fee']:+.2f} | {r['pnl_oos']:+.2f} | {r['pnl_fee'] + r['pnl_oos']:+.2f} |")
    L.append("")
    L.append("## 5 terburuk di hari OOS (hindari)")
    L.append("")
    L.append("| time_entry | mp | exit | PnL OOS |")
    L.append("|---|---|---|---|")
    for r in worst_oos:
        L.append(f"| [{r['time_entry']}] | {r['entry_max_price']} | {r['exit_on_reversal']} | {r['pnl_oos']:+.2f} |")
    L.append("")
    L.append("## Config terbaik per coin (berdasarkan hari OOS)")
    L.append("")
    L.append("| Coin | time_entry | mp | exit | PnL IS | PnL OOS |")
    L.append("|---|---|---|---|---|---|")
    for coin, b in best_per_coin.items():
        d = b["per_coin_oos"][coin]
        L.append(f"| {coin.upper()} | [{b['time_entry']}] | {b['entry_max_price']} | {b['exit_on_reversal']} | "
                 f"{b['pnl_fee']:+.2f} | {d['pnl_fee']:+.2f} |")
    L.append("")
    L.append("## Config rekomendasi (robust) dalam format config.json")
    L.append("")
    L.append("```json")
    for coin, b in best_per_coin.items():
        L.append(f'  "{coin}": {{"time_entry": [{b["time_entry"]}], "entry_max_price": '
                 f'{b["entry_max_price"]}, "exit_on_reversal": {str(b["exit_on_reversal"]).lower()}}},')
    L.append("```")
    L.append("")
    L.append("## Glosarium")
    L.append("")
    L.append("| Istilah | Arti |")
    L.append("|---|---|")
    for term, meaning in GLOSSARY:
        L.append(f"| {term} | {meaning} |")
    L.append("")
    L.append("## Asumsi & limitasi")
    L.append("")
    L.append("- Fill price = model probabilitas (sigma + spread asumsi), bukan order book historis predict.fun.")
    L.append("- Sigma dihitung dari hari in-sample (11 Nov); evaluasi hari OOS (12 Nov) walk-forward, tanpa bocor data masa-depan.")
    L.append("- Fee 2% potong proceeds; spread 2 sen — realita order book bisa lebih lebar.")
    L.append("- Resolusi = harga detik t0+299 vs open detik t0 (±1 detik vs Chainlink resmi).")
    L.append("- 1 hari = 288 jendela/coin — sampel kecil; in-sample hampir pasti overfit, KARENA ITU ada kolom OOS.")
    L.append("- exit_on_reversal = jual di bid saat harga balik melewati open, sebelum detik 300.")
    L.append("")
    OUT_REPORT.write_text("\n".join(L), encoding="utf-8")
    print(f"results.csv: {len(merged)} config (IS + OOS); report.md ditulis.")
    print("Top 3 in-sample (perhatikan OOS!):")
    for r in top_is[:3]:
        print(f"  [{r['time_entry']}] mp={r['entry_max_price']} exit={r['exit_on_reversal']} "
              f"IS={r['pnl_fee']:+.2f} -> OOS={r['pnl_oos']:+.2f}")
    print("Robust (profit di kedua hari) top 3:")
    for r in robust[:3]:
        print(f"  [{r['time_entry']}] mp={r['entry_max_price']} exit={r['exit_on_reversal']} "
              f"IS={r['pnl_fee']:+.2f} OOS={r['pnl_oos']:+.2f} "
              f"total={r['pnl_fee'] + r['pnl_oos']:+.2f}")
    print("Best per coin (by OOS):")
    for coin, b in best_per_coin.items():
        d = b["per_coin_oos"][coin]
        wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0.0
        print(f"  {coin.upper()}: time_entry=[{b['time_entry']}] mp={b['entry_max_price']} "
              f"exit={b['exit_on_reversal']} OOS pnl={d['pnl_fee']:+.2f} (wr OOS {wr:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())