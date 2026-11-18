"""python preview_budget.py outcomes.csv [now_ms] -> coldkeys whose check-budget multiplier changes."""
import csv, sys, time
rows = [(int(i), o, int(p), int(j), int(d) if d else None) for i, o, p, j, d in (r for r in csv.reader(open(sys.argv[1])) if len(r) == 5)]
now = int(sys.argv[2]) if len(sys.argv) > 2 else int(time.time() * 1000)
def scores(seq, start, hl):
    dec, q = 0.5 ** (1 / hl), {}
    for o, p in seq:
        q[o] = dec * q.get(o, start) + (1 - dec) * p
    return q
mult = lambda q: min(1.0, max(0.0, (q - 0.15) / 0.10))
old = scores([(o, p) for i, o, p, j, d in sorted(rows, key=lambda r: (r[3], r[0]))], 0.30, 50)
cut = now - 30 * 86400000; cut -= cut % 3600000
recent = [r for r in rows if r[4] is not None and r[4] >= cut]
new = scores([(o, p) for i, o, p, j, d in sorted(recent, key=lambda r: (r[4], r[0]))], 0.50, 20)
owners = set(old) | set(new)
changed = []
for o in sorted(owners):
    mo, mn = mult(old.get(o, 0.30)), mult(new.get(o, 0.50))
    if abs(mo - mn) > 1e-9:
        changed.append((o, round(old.get(o, 0.30), 3), round(mo, 2), round(new.get(o, 0.50), 3), round(mn, 2)))
print(f"coldkeys scored: old {len(old)}, new {len(new)}; budget multiplier changes for {len(changed)}")
print("owner,q_old,mult_old,q_new,mult_new")
for c in sorted(changed, key=lambda c: c[4] - c[2]):
    print(",".join(map(str, c)))
