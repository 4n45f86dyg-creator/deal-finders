#!/usr/bin/env python3
"""HQ read-out on the Mac: python3 report.py -> both finders' numbers (decrypts their GitHub state locally)."""
import getpass, json, os, subprocess, tarfile, io, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
key = subprocess.run(["security", "find-generic-password", "-a", getpass.getuser(), "-s", "deal-finders-state-key", "-w"],
                     capture_output=True, text=True).stdout.strip()
for name in ("flip", "free", "jobs", "report"):
    if subprocess.run(["git", "-C", str(ROOT), "fetch", "-q", "origin", f"state-{name}"], capture_output=True).returncode:
        print(f"{name}: no state on GitHub yet")
        continue
    enc = subprocess.run(["git", "-C", str(ROOT), "show", "FETCH_HEAD:state.enc"], capture_output=True).stdout
    dec = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "20000", "-md", "sha256", "-pass", "env:STATE_KEY"],
                         input=enc, capture_output=True, env={**os.environ, "STATE_KEY": key})
    if dec.returncode:
        print(f"{name}: can't decrypt state")
        continue
    with tempfile.TemporaryDirectory() as d:
        tarfile.open(fileobj=io.BytesIO(dec.stdout), mode="r:gz").extractall(d)
        base = Path(d) / name
        meta = json.loads((base / "meta.json").read_text()) if (base / "meta.json").exists() else {}
        deals = json.loads((base / "deals.json").read_text()) if (base / "deals.json").exists() else []
        if not deals and (base / "leads.json").exists():
            deals = json.loads((base / "leads.json").read_text())      # the job watcher keeps leads, not deals
        log = (base / "log.txt").read_text().splitlines() if (base / "log.txt").exists() else []
    print(f"== {name}: last run {meta.get('last_run', '?')}")
    days = meta.get("days", {})
    for day in (sorted(days)[-3:] if isinstance(days, dict) else []):
        print(f"   {day}: {days[day]}")
    states = {}
    for x in deals:
        states[x.get("state")] = states.get(x.get("state"), 0) + 1
    money = sum(x.get("sold", 0) - x.get("bought", 0) for x in deals if x.get("state") == "sold")
    print(f"   deals by state {states} · profit €{money:.0f}")
    for x in deals[-5:]:
        print(f"   #{x['no']} {x.get('at', '')} {x.get('state')} {x.get('price', 0):.0f}€ {(x.get('title') or x.get('name', ''))[:50]}")
    errs = [l for l in log if "error " in l or "error:" in l][-3:]
    print(f"   recent errors: {errs or 'none'}")
