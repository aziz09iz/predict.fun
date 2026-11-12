"""Evaluasi hasil live paper dari data VPS (read-only)."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "predictbot.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== overall ===")
r = conn.execute(
    "SELECT COUNT(*) n, SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) w, "
    "SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) l, "
    "SUM(CASE WHEN outcome='PUSH' THEN 1 ELSE 0 END) p, "
    "SUM(COALESCE(pnl,0)) pnl FROM trades WHERE status='SETTLED'").fetchone()
n, w, l, p, pnl = r["n"], r["w"], r["l"], r["p"], r["pnl"]
print(f"settled={n} win={w} loss={l} push={p} pnl={pnl:+.2f} "
      f"winrate={w/(w+l)*100 if w+l else 0:.1f}%")

print("\n=== per coin ===")
rows = conn.execute(
    "SELECT coin, COUNT(*) n, SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) w, "
    "SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) l, "
    "SUM(COALESCE(pnl,0)) pnl, AVG(fill_price) af, MIN(fill_price) mn, MAX(fill_price) mx "
    "FROM trades WHERE status='SETTLED' GROUP BY coin").fetchall()
for r in rows:
    wr = r["w"] / (r["w"] + r["l"]) * 100 if (r["w"] + r["l"]) else 0
    print(f"{r['coin'].upper()}: n={r['n']} win={r['w']} loss={r['l']} pnl={r['pnl']:+.2f} "
          f"wr={wr:.1f}% fill avg={r['af']:.3f} [{r['mn']:.3f}..{r['mx']:.3f}]")

print("\n=== per direction ===")
rows = conn.execute(
    "SELECT direction, COUNT(*) n, SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) w, "
    "SUM(COALESCE(pnl,0)) pnl FROM trades WHERE status='SETTLED' GROUP BY direction").fetchall()
for r in rows:
    print(f"{r['direction']}: n={r['n']} win={r['w']} pnl={r['pnl']:+.2f}")

print("\n=== exit vs settle ===")
rows = conn.execute(
    "SELECT CASE WHEN exit_price IS NOT NULL THEN 'exit' ELSE 'settle' END kind, "
    "COUNT(*) n, SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) w, "
    "SUM(COALESCE(pnl,0)) pnl, AVG(COALESCE(exit_price, fill_price)) avg_px "
    "FROM trades WHERE status='SETTLED' GROUP BY kind").fetchall()
for r in rows:
    print(f"{r['kind']}: n={r['n']} win={r['w']} pnl={r['pnl']:+.2f} avg_px={r['avg_px']:.3f}")

print("\n=== balance events (terakhir 12) ===")
rows = conn.execute(
    "SELECT ts, type, amount, balance_after, note FROM balance_events "
    "ORDER BY id DESC LIMIT 12").fetchall()
for r in rows:
    print(f"{r['ts']} {r['type']:>7} {r['amount']:+.3f} bal={r['balance_after']:.2f}  {r['note']}")

print("\n=== 15 trade terakhir ===")
rows = conn.execute(
    "SELECT id, coin, direction, fill_price, exit_price, outcome, pnl, opened_at, settled_at "
    "FROM trades ORDER BY id DESC LIMIT 15").fetchall()
for r in rows:
    ep = f" exit={r['exit_price']:.3f}" if r["exit_price"] else ""
    print(f"#{r['id']} {r['coin']} {r['direction']} fill={r['fill_price']:.3f}{ep} "
          f"{r['outcome']} {r['pnl']:+.3f}  {r['opened_at']} -> {r['settled_at']}")

print("\n=== runtime ===")
rows = conn.execute("SELECT key, value FROM runtime").fetchall()
for r in rows:
    print(f"{r['key']} = {r['value']}")
