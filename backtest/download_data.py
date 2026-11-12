"""Download 1 hari data tick (kline 1 detik) Binance untuk backtest.

Sumber utama: https://data.binance.vision (arsip harian publik).
Fallback: REST api.binance.com jika arsip harian belum terbit (biasanya
muncul beberapa jam setelah hari berakhir).

Output: backtest/data/{SYMBOL}-1s-{date}.csv
Pemakaian:
    python download_data.py [YYYY-MM-DD]
"""
from __future__ import annotations

import io
import re
import sys
import time
import zipfile
import datetime
from pathlib import Path

import httpx

BASE = "https://data.binance.vision"
API_BASE = "https://api.binance.com"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"


def _check_url(url: str) -> None:
    """Allowlist ketat: hanya https + host tetap (anti-SSRF)."""
    parts = httpx.URL(url)
    if parts.scheme != "https" or parts.host not in ("data.binance.vision", "api.binance.com"):
        raise ValueError(f"URL diblokir: {url}")


def _check_day(day: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError(f"format tanggal tidak valid: {day!r}")
    datetime.date.fromisoformat(day)
    return day


def _write_csv(data_dir: Path, expected: str, content: bytes | str) -> None:
    if "/" in expected or "\\" in expected or ".." in expected:
        raise ValueError(f"nama file tidak aman: {expected!r}")
    out = (data_dir / expected).resolve()
    if data_dir not in out.parents:
        raise ValueError(f"path keluar dari folder data: {out}")
    if isinstance(content, bytes):
        out.write_bytes(content)
    else:
        out.write_text(content, encoding="utf-8")


def fetch_api_klines(client: httpx.Client, symbol: str, day: str) -> str:
    """Kline 1s via REST api.binance.com — fallback saat arsip harian 404."""
    start = int(datetime.datetime.fromisoformat(day + "T00:00:00+00:00").timestamp() * 1000)
    end = start + 86_400_000
    cursor, rows = start, []
    empty_streak = 0
    while cursor < end:
        resp = client.get("/api/v3/klines", params={
            "symbol": symbol, "interval": "1s",
            "startTime": cursor, "endTime": end - 1, "limit": 1000,
        })
        if resp.status_code in (418, 429):
            print("  rate-limited — tunggu 10s")
            time.sleep(10)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"API {resp.status_code}: {resp.text[:120]}")
        batch = resp.json()
        if not batch:
            # data kosong/gap — maju 1 menit dan lanjut (jangan break)
            empty_streak += 1
            cursor += 60_000
            if empty_streak % 10 == 0:
                print(f"  gap data di detik {(cursor // 1000) % 86_400} — skip 1 menit")
            continue
        empty_streak = 0
        for k in batch:
            rows.append(",".join([str(k[0]), k[1], k[2], k[3], k[4], k[5]]))
        cursor = int(batch[-1][0]) + 1000
        time.sleep(0.1)
    return "\n".join(rows)


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else str(datetime.date.today() - datetime.timedelta(days=1))
    day = _check_day(day)
    data_dir = DATA_DIR.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=BASE, follow_redirects=True, timeout=120,
                      headers={"User-Agent": "predictfun-backtest/1.0"}) as client:
        for sym in SYMBOLS:
            if not re.fullmatch(r"[A-Z0-9]{5,12}", sym):
                raise ValueError(f"simbol tidak valid: {sym!r}")
            path = f"/data/spot/daily/klines/{sym}/1s/{sym}-1s-{day}.zip"
            print(f"[{sym}] mengunduh {day} ...", flush=True)
            resp = client.get(path)
            if resp.status_code != 200:
                print("  arsip 404 — fallback REST API ...")
                with httpx.Client(base_url=API_BASE, follow_redirects=True, timeout=120,
                                  headers={"User-Agent": "predictfun-backtest/1.0"}) as api_client:
                    text = fetch_api_klines(api_client, sym, day)
                if not text:
                    print("  kosong — lewati")
                    continue
                _write_csv(data_dir, f"{sym}-1s-{day}.csv", text)
                print(f"  tersimpan {sym}-1s-{day}.csv ({text.count(chr(10)) + 1:,} baris, via REST API)")
                continue
            expected = f"{sym}-1s-{day}.csv"
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                if z.namelist() != [expected]:
                    raise ValueError(f"isi arsip tak terduga: {z.namelist()}")
                raw = z.read(expected)
            _write_csv(data_dir, expected, raw)
            print(f"  tersimpan {expected} (~{raw.count(chr(10).encode()):,} baris)")
    print("selesai.")
    return 0


if __name__ == "__main__":
    sys.exit(main())