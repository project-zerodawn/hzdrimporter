#!/usr/bin/env python3
"""
hzdr_apply.py - apply (or undo) a prepared mod: replacement stream entries + patched .core files.

  py hzdr_apply.py apply   <MOD_FOLDER>\\manifest.json
  py hzdr_apply.py restore <MOD_FOLDER>\\manifest.json

  <MOD_FOLDER>  the folder holding manifest.json and the files it lists

The manifest lists, for each changed entry, its index key, original size and checksum, and the new
data file; and for each changed .core, its original size/checksum and the patched file. Nothing is
written unless every original is found exactly as expected. Everything changed is backed up first.
Needs hzdr_texpatch.py and hzdr_meshswap.py in the same folder.
"""
import json, os, struct, sys
import hzdr_texpatch as T
import hzdr_meshswap as MS
import hzdr_grow as G


def backup_path(man_path):
    return os.path.splitext(man_path)[0] + ".backup.json"


def cmd_apply(man_path, pk):
    base = os.path.dirname(os.path.abspath(man_path))
    man = json.load(open(man_path))
    bpath = backup_path(man_path)
    if os.path.exists(bpath):
        raise SystemExit("This mod is already applied (backup exists). Run 'restore' first.")
    index = T.read_index(pk.dir)
    plan = []
    for e in man["entries"]:
        if e.get("find"):
            # entry located by its size and its vertex layout (used for the lower detail levels)
            nv = e["find"]["nv"]; sd = e["find"].get("stride", 24); cands = []
            for pkg, entries in index:
                if not pkg.endswith(".stream"):
                    continue
                for k, (h, off, size) in enumerate(entries):
                    if size != e["orig_size"]:
                        continue
                    head = pk.get(pkg).read(off, nv * sd)
                    if all(head[r * sd + 6:r * sd + 8] == b"\x00\x3c" for r in range(0, nv, max(1, nv // 300))):
                        cands.append((pkg, k, off, size, h))
            if len(cands) > 1 and e["find"].get("bbox"):
                # keep only copies whose points lie inside this mesh's own bounding box (from its .core)
                lo, hi = e["find"]["bbox"][:3], e["find"]["bbox"][3:]
                def inside(c):
                    head = pk.get(c[0]).read(c[2], nv * sd)
                    for r in range(0, nv, max(1, nv // 300)):
                        p = struct.unpack_from("<3e", head, r * sd)
                        if any(p[j] < lo[j] - 0.01 or p[j] > hi[j] + 0.01 for j in range(3)):
                            return False
                    return True
                cands = [c for c in cands if inside(c)]
            if len(cands) > 1:
                datas = {T.sha1(pk.get(c[0]).read(c[2], c[3])) for c in cands}
                if len(datas) != 1:
                    raise SystemExit(f"{e['name']}: {len(cands)} different entries match; not changing anything.")
                print(f"  {e['name']}: {len(cands)} identical copies found; all are replaced")
            if not cands:
                raise SystemExit(f"{e['name']}: original entry not found (already applied? run 'restore').")
            for pkg, k, off, size, h in cands[1:]:
                print(f"  {e['name']} found in {pkg} (key {h:016x})")
                data = open(os.path.join(base, e["file"]), "rb").read()
                plan.append(("entry" if len(data) <= size else "grow", e["name"], pkg, k, off, data))
            pkg, k, off, size, h = cands[0]
            print(f"  {e['name']} found in {pkg} (key {h:016x})")
        else:
            key = int(e["key"], 16); hit = None
            for pkg, entries in index:
                for k, (h, off, size) in enumerate(entries):
                    if h == key:
                        hit = (pkg, k, off, size)
            if not hit:
                raise SystemExit(f"{e['name']}: entry {e['key']} not in the index.")
            pkg, k, off, size = hit
            if size != e["orig_size"] or T.sha1(pk.get(pkg).read(off, size)) != e["orig_sha1"]:
                raise SystemExit(f"{e['name']}: game data isn't the expected original (game update?).")
        data = open(os.path.join(base, e["file"]), "rb").read()
        if len(data) > size and not e.get("allow_grow"):
            raise SystemExit(f"{e['name']}: new data is larger than the original.")
        plan.append(("entry" if len(data) <= size else "grow", e["name"], pkg, k, off, data))
    for cdesc in man["cores"]:
        hit = None
        for pkg, entries in index:
            if pkg.endswith(".stream"):
                continue
            for h, off, size in entries:
                if size == cdesc["size"] and T.sha1(pk.get(pkg).read(off, size)) == cdesc["orig_sha1"]:
                    hit = (pkg, off); break
            if hit: break
        if not hit:
            raise SystemExit(f"{cdesc['name']}: original not found in the packages (game update?).")
        data = open(os.path.join(base, cdesc["file"]), "rb").read()
        if len(data) != cdesc["size"]:
            raise SystemExit(f"{cdesc['name']}: patched file has the wrong size.")
        plan.append(("core", cdesc["name"], hit[0], None, hit[1], data))

    backup = {"packages": {}, "locators": [], "grown": []}
    loc_path = os.path.join(pk.dir, "PackFileLocators.bin")
    plan.sort(key=lambda t: t[0] == "grow")          # in-place writes first, grown entries last
    for kind, name, pkg, k, off, data in plan:
        if kind == "grow":
            gb = {}
            backup["grown"].append(gb); json.dump(backup, open(bpath, "w"))
            new_off = G.append_entry(pk.get(pkg), data, gb)
            json.dump(backup, open(bpath, "w"))
            rec = MS.locator_record_offset(pk.dir, pkg, k)
            with open(loc_path, "r+b") as f:
                f.seek(rec); old = f.read(16)
                backup["locators"].append({"at": rec, "bytes": old.hex()})
                json.dump(backup, open(bpath, "w"))
                f.seek(rec + 8); f.write(struct.pack("<II", new_off, len(data)))
            print(f"  wrote {name} -> {pkg} (enlarged: {len(data)} bytes, relocated)")
            continue
        b = backup["packages"].setdefault(pkg, {"chunks": {}})
        pk.get(pkg).write(off, data, b)
        json.dump(backup, open(bpath, "w"))
        if pk.get(pkg).read(off, len(data)) != data:
            raise SystemExit(f"Verification failed after writing {name}. Run 'restore'.")
        if kind == "entry":
            rec = MS.locator_record_offset(pk.dir, pkg, k)
            with open(loc_path, "r+b") as f:
                f.seek(rec); old = f.read(16)
                backup["locators"].append({"at": rec, "bytes": old.hex()})
                json.dump(backup, open(bpath, "w"))
                f.seek(rec + 12); f.write(struct.pack("<I", len(data)))
        print(f"  wrote {name} -> {pkg}")
    print(f"Backup saved to {bpath}\nDone. Use 'restore' to undo.")


def cmd_restore(man_path, pk):
    bpath = backup_path(man_path)
    if not os.path.exists(bpath):
        raise SystemExit("No backup found; nothing to restore.")
    backup = json.load(open(bpath))
    for gb in reversed(backup.get("grown", [])):
        if "head" in gb:
            G.undo(gb); print(f"  restored chunk table of {os.path.basename(gb['path'])}")
    for pkg, b in backup["packages"].items():
        pk.get(pkg).restore(b); print(f"  restored {pkg}")
    with open(os.path.join(pk.dir, "PackFileLocators.bin"), "r+b") as f:
        for r in backup["locators"]:
            f.seek(r["at"]); f.write(bytes.fromhex(r["bytes"]))
    print("  restored PackFileLocators.bin entries")
    os.replace(bpath, bpath + ".used")          # replace an older .used file if one is already there (os.rename fails on Windows)
    print("Done.")


def main():
    argv = sys.argv[1:]
    package_dir = T.get_package_dir(argv)
    if len(argv) != 2 or argv[0] not in ("apply", "restore"):
        print(__doc__); return
    pk = T.Packages(package_dir, T.LZ4())
    {"apply": cmd_apply, "restore": cmd_restore}[argv[0]](argv[1], pk)


if __name__ == "__main__":
    try:
        main()
    except PermissionError:
        raise SystemExit("Permission denied writing game files. Run the command prompt as Administrator, "
                         "and make sure the game is closed.")
