"""Run the engine over every PDF in a folder and print a summary."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pressroom import engine  # noqa: E402

folder = sys.argv[1]
target = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
floor = int(sys.argv[3]) if len(sys.argv) > 3 else 200

for name in sorted(os.listdir(folder)):
    if not name.lower().endswith(".pdf"):
        continue
    path = os.path.join(folder, name)
    t = time.time()
    try:
        r = engine.compress(path, target, floor)
    except engine.EngineError as e:
        print(f"{name}: {e}")
        continue
    bad = [k for k, v in r["checks"].items() if not v]
    print(f"\n{name}: {r['original_size'] / 1e6:.2f} -> {r['size'] / 1e6:.2f} MB  [{r['status']}]"
          f"  dpi={r['dpi']} q={r['quality']}  {time.time() - t:.1f}s  checks={'OK' if not bad else 'FAILED ' + ','.join(bad)}")
    for c in r["changes"]:
        print(f"   {c['kind']:8} {c['text']}  {('-%.2f MB' % (c['saved'] / 1e6)) if c['saved'] else ''}")
    print(f"   output: {r['temp_output']}")
