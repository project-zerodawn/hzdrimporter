#!/usr/bin/env python3
"""
hzdr_meshswap.py - replace a rigid mesh (like a hat) in Horizon Zero Dawn Remastered.

Commands
  find     Locate the mesh's .core, its full-quality geometry entry and (optionally) its
           detail-level setup .core in the game files.
             py hzdr_meshswap.py find <MESH>.core <MESH>_lod_setup.core
  swap     Put the parts of an .ascii model into the full-quality geometry.
             py hzdr_meshswap.py swap <MESH>.mesh.json <NEW_MODEL>.ascii --keep 2
           --keep N   keep original part N (counting from 0) instead of replacing it; repeatable.
           If the setup .core was found, the full-quality model is used at every distance.
  restore  Undo the swap.
             py hzdr_meshswap.py restore <MESH>.mesh.json

The .ascii must have one part for every part that isn't kept (the mesh's part count minus the kept ones).
Every vertex is attached to the original's bone; only the full-quality level is replaced.

  <MESH>       the mesh's .core file name without .core (made-up example: "characterhelmet_lx")
  <NEW_MODEL>  your replacement model in .ascii format

Needs hzdr_texpatch.py in the same folder.
"""
import json
import math
import os
import struct
import sys

import hzdr_texpatch as T

MESH = 0x03773cc419d4989b
PRIM = 0x7811dcc411ba1288
VB = 0xc7f1a4abbe992e6c
IB = 0x0a2c463f56053f70
DRAW = 0x38f49cecd473a967


# ----------------------------------------------------------------------------- .core parsing
def objects(c):
    p, out = 0, []
    while p + 12 <= len(c):
        t, sz = struct.unpack_from("<QI", c, p)
        out.append({"at": p, "type": t, "size": sz})
        p += 12 + sz
    if p != len(c):
        raise SystemExit("Unexpected .core layout")
    return out


def parse_core(c):
    """Returns the meshes (detail levels) in order, each with its parts' buffer descriptions."""
    obs = objects(c)
    vbs, ibs, prims, meshes, draws = [], [], [], [], []
    for o in obs:
        d = o["at"] + 12
        if o["type"] == VB:
            nv, ns = struct.unpack_from("<II", c, d + 16)
            p = d + 25
            strides = []
            for _ in range(ns):
                _, stride, ne = struct.unpack_from("<III", c, p)
                strides.append(stride)
                p += 12 + 4 * ne + 16
            vbs.append({"at": d, "nv": nv, "strides": strides,
                        "bone_fmt": None})
        elif o["type"] == IB:
            n, flags = struct.unpack_from("<II", c, d + 16)
            ibs.append({"at": d, "count": n, "flags": flags})
        elif o["type"] == PRIM:
            prims.append({"at": d})
        elif o["type"] == MESH:
            (l,) = struct.unpack_from("<I", c, d + 16)
            meshes.append({"at": d, "guid": c[d:d + 16], "name": c[d + 24:d + 24 + l].decode(),
                           "bbox_at": d + 24 + l})
        elif o["type"] == DRAW:
            draws.append({"at": d})
    if not (len(vbs) == len(ibs) == len(prims)) or len(prims) % len(meshes):
        raise SystemExit("This .core doesn't look like a supported mesh.")
    per = len(prims) // len(meshes)
    for m_i, m in enumerate(meshes):
        m["parts"] = [{"vb": vbs[m_i * per + k], "ib": ibs[m_i * per + k], "prim": prims[m_i * per + k]}
                      for k in range(per)]
        m["draw"] = draws[m_i] if m_i < len(draws) else None
    return meshes


def align(x, a):
    return (x + a - 1) // a * a


def layout(parts):
    """Offsets of each buffer in a geometry entry: vertex streams then indices, each aligned
    to its own record size."""
    p, out = 0, []
    for nv, strides, nidx in parts:
        lay = []
        for s in strides:
            p = align(p, s); lay.append(p); p += nv * s
        p = align(p, 2); lay.append(p); p += nidx * 2
        out.append(lay)
    return out, p


def level_layout(mesh):
    return layout([(pt["vb"]["nv"], pt["vb"]["strides"], pt["ib"]["count"]) for pt in mesh["parts"]])


# ----------------------------------------------------------------------------- .ascii reading
def read_ascii(path):
    lines = [l.strip() for l in open(path) if l.strip()]
    i = 0

    def nx():
        nonlocal i
        i += 1
        return lines[i - 1]

    out = []
    for _ in range(int(nx())):
        name = nx(); uvl = int(nx()); nt = int(nx())
        for _ in range(nt):
            nx(); nx()
        verts = []
        for _ in range(int(nx())):
            p = [float(x) for x in nx().split()]
            n = [float(x) for x in nx().split()]
            nx()
            uvs = [[float(x) for x in nx().split()] for _ in range(uvl)]
            nx(); nx()
            verts.append((p, n, uvs))
        faces = [tuple(int(x) for x in nx().split()) for _ in range(int(nx()))]
        out.append({"name": name, "verts": verts, "faces": faces})
    return out


# ----------------------------------------------------------------------------- geometry building
def norm(v):
    l = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return [v[0] / l, v[1] / l, v[2] / l]


def tangents(verts, faces):
    t = [[0.0, 0.0, 0.0] for _ in verts]
    b = [[0.0, 0.0, 0.0] for _ in verts]
    for f in faces:
        (p0, _, u0), (p1, _, u1), (p2, _, u2) = (verts[f[0]], verts[f[1]], verts[f[2]])
        e1 = [p1[k] - p0[k] for k in range(3)]; e2 = [p2[k] - p0[k] for k in range(3)]
        du1, dv1 = u1[0][0] - u0[0][0], u1[0][1] - u0[0][1]
        du2, dv2 = u2[0][0] - u0[0][0], u2[0][1] - u0[0][1]
        r = du1 * dv2 - du2 * dv1
        if abs(r) < 1e-12:
            continue
        r = 1.0 / r
        sd = [(e1[k] * dv2 - e2[k] * dv1) * r for k in range(3)]
        td = [(e2[k] * du1 - e1[k] * du2) * r for k in range(3)]
        for vi in f:
            for k in range(3):
                t[vi][k] += sd[k]; b[vi][k] += td[k]
    out = []
    for (p, n, _), tv, bv in zip(verts, t, b):
        n = norm(n)
        d = sum(tv[k] * n[k] for k in range(3))
        tt = [tv[k] - n[k] * d for k in range(3)]
        if sum(x * x for x in tt) < 1e-20:   # no usable UV direction: any perpendicular will do
            a = [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0]
            d = sum(a[k] * n[k] for k in range(3))
            tt = [a[k] - n[k] * d for k in range(3)]
        tt = norm(tt)
        c = [n[1] * tt[2] - n[2] * tt[1], n[2] * tt[0] - n[0] * tt[2], n[0] * tt[1] - n[1] * tt[0]]
        sign = 0 if sum(c[k] * bv[k] for k in range(3)) >= 0 else 32767
        out.append((tt, sign))
    return out


def i16(x):
    return max(-32768, min(32767, int(round(x * 32768))))


def build_part(part, bone):
    verts, faces = part["verts"], part["faces"]
    if len(verts) > 65535:
        raise SystemExit(f"{part['name']}: more than 65535 vertices")
    tans = tangents(verts, faces)
    pos = bytearray(); nrm = bytearray(); uvs = bytearray()
    for (p, n, uv), (t, sign) in zip(verts, tans):
        pos += struct.pack("<eee", *p) + b"\x00\x3c" + bytes([bone, 0, 0, 0])
        nrm += struct.pack("<hhhh", i16(n[0]), i16(n[1]), i16(n[2]), 0)
        nrm += struct.pack("<hhhh", i16(t[0]), i16(t[1]), i16(t[2]), sign)
        uv0 = uv[0]; uv1 = uv[1] if len(uv) > 1 else [0.0, 0.0]
        uvs += b"\xff\xff\xff\xff" + struct.pack("<eeee", uv0[0], uv0[1], uv1[0], uv1[1])
    idx = bytearray()
    for a, b, c in faces:             # the game stores the opposite winding to the .ascii
        idx += struct.pack("<HHH", c, b, a)
    return [bytes(pos), bytes(nrm), bytes(uvs)], bytes(idx), len(verts), len(faces) * 3


def assemble(built, strides_list):
    """built: per part (streams, index bytes, nv, nidx) -> geometry entry bytes."""
    lay, total = layout([(nv, strides, nidx) for (_, _, nv, nidx), strides in zip(built, strides_list)])
    out = bytearray(total)
    for (streams, idx, nv, nidx), offs in zip(built, lay):
        for st, o in zip(streams, offs[:-1]):
            out[o:o + len(st)] = st
        out[offs[-1]:offs[-1] + len(idx)] = idx
    return bytes(out)


def original_parts(mesh, geometry):
    lay, _ = level_layout(mesh)
    out = []
    for pt, offs in zip(mesh["parts"], lay):
        nv, nidx = pt["vb"]["nv"], pt["ib"]["count"]
        streams = [geometry[o:o + nv * s] for o, s in zip(offs[:-1], pt["vb"]["strides"])]
        out.append((streams, geometry[offs[-1]:offs[-1] + nidx * 2], nv, nidx))
    return out


def bbox(verts):
    xs = [v[0] for v in verts]
    return [min(x[0] for x in xs), min(x[1] for x in xs), min(x[2] for x in xs),
            max(x[0] for x in xs), max(x[1] for x in xs), max(x[2] for x in xs)]


def union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3]), max(a[4], b[4]), max(a[5], b[5])]


# ----------------------------------------------------------------------------- index (PackFileLocators.bin)
def locator_record_offset(package_dir, pkg, entry):
    with open(os.path.join(package_dir, "PackFileLocators.bin"), "rb") as f:
        d = f.read()
    p = 4
    (n,) = struct.unpack_from("<I", d, 0)
    for _ in range(n):
        (l,) = struct.unpack_from("<I", d, p); p += 4
        name = d[p:p + l].decode(); p += l
        (c,) = struct.unpack_from("<I", d, p); p += 4
        if name == pkg:
            return p + 16 * entry
        p += 16 * c
    raise SystemExit(f"{pkg} not in the index")


# ----------------------------------------------------------------------------- commands
def find_core(pk, index, blob):
    for pkg, entries in index:
        if pkg.endswith(".stream"):
            continue
        for h, off, size in entries:
            if size == len(blob) and pk.get(pkg).read(off, size) == blob:
                return {"package": pkg, "offset": off, "size": size}
    return None


def lod_distances(setup, meshes):
    """Offsets of the switch distance stored after each detail level in the setup .core."""
    out = []
    for m in meshes:
        i = setup.find(b"\x02" + m["guid"])
        if i < 0:
            return None
        (l,) = struct.unpack_from("<I", setup, i + 17)
        out.append(i + 25 + l)
    return out


def cmd_find(args, pk):
    core_path = args[0]
    setup_path = args[1] if len(args) > 1 else None
    core = open(core_path, "rb").read()
    meshes = parse_core(core)
    print(f"{os.path.basename(core_path)}: {len(meshes)} detail levels, {len(meshes[0]['parts'])} parts each")
    index = T.read_index(pk.dir)

    core_loc = find_core(pk, index, core)
    if not core_loc:
        raise SystemExit("Could not find this .core inside the packages (already swapped? run 'restore' first).")
    print(f"  .core found in {core_loc['package']}")
    setup_loc = None
    if setup_path:
        setup = open(setup_path, "rb").read()
        dists = lod_distances(setup, meshes)
        if not dists:
            raise SystemExit(f"{setup_path} doesn't list this mesh's detail levels.")
        setup_loc = find_core(pk, index, setup)
        if not setup_loc:
            raise SystemExit("Could not find the setup .core inside the packages (already changed? run 'restore' first).")
        setup_loc.update(sha1=T.sha1(setup), distances=dists,
                         values=[struct.unpack_from("<f", setup, d)[0] for d in dists])
        print(f"  setup .core found in {setup_loc['package']}; switch distances {setup_loc['values']}")

    la = meshes[0]
    lay, total = level_layout(la)
    first = la["parts"][0]
    nv0 = first["vb"]["nv"]
    hit = None
    for pkg, entries in index:
        if not pkg.endswith(".stream"):
            continue
        for k, (h, off, size) in enumerate(entries):
            if size != total:
                continue
            a = pk.get(pkg)
            pos = a.read(off + lay[0][0], nv0 * 12)
            if all(pos[r * 12 + 6:r * 12 + 8] == b"\x00\x3c" for r in range(0, nv0, max(1, nv0 // 200))):
                hit = {"package": pkg, "entry": k, "key": f"{h:016x}", "offset": off, "size": size,
                       "bone": pos[8]}
                break
        if hit:
            break
    if not hit:
        raise SystemExit("Could not find the full-quality geometry.")
    data = pk.get(hit["package"]).read(hit["offset"], hit["size"])
    hit["sha1"] = T.sha1(data)
    print(f"  {la['name']} geometry found in {hit['package']} ({hit['size']} bytes, bone {hit['bone']})")
    m = {"core_file": os.path.basename(core_path), "core": core_loc, "core_sha1": T.sha1(core),
         "level": la["name"], "geometry": hit, "lod_setup": setup_loc}
    out = os.path.splitext(core_path)[0] + ".mesh.json"
    json.dump(m, open(out, "w"), indent=2)
    print(f"Saved {out}")


def backup_path(map_path):
    return os.path.splitext(map_path)[0] + ".backup.json"


def cmd_swap(args, pk):
    keep = []
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--keep":
            keep.append(int(args[i + 1])); i += 2
        else:
            rest.append(args[i]); i += 1
    map_path, ascii_path = rest[0], rest[1]
    m = json.load(open(map_path))
    bpath = backup_path(map_path)
    backup = json.load(open(bpath)) if os.path.exists(bpath) else {}
    first_time = not backup
    if not first_time and "orig_geometry" not in backup:
        raise SystemExit("This backup is from an older version of the script. Run 'restore' first, "
                         "then 'find' and 'swap' again.")

    cl, g = m["core"], m["geometry"]
    core_now = pk.get(cl["package"]).read(cl["offset"], cl["size"])
    if first_time:
        if T.sha1(core_now) != m["core_sha1"]:
            raise SystemExit("The .core changed since 'find' (game update?). Run 'find' again.")
        geo_now = pk.get(g["package"]).read(g["offset"], g["size"])
        if T.sha1(geo_now) != g["sha1"]:
            raise SystemExit("The geometry changed since 'find' (game update?). Run 'find' again.")
        orig_core, orig_geo = core_now, geo_now
    else:
        orig_core, orig_geo = bytes.fromhex(backup["orig_core"]), bytes.fromhex(backup["orig_geometry"])
    meshes = parse_core(orig_core)
    la = meshes[0]
    nparts = len(la["parts"])
    if any(k < 0 or k >= nparts for k in keep):
        raise SystemExit(f"--keep must be between 0 and {nparts - 1}.")

    parts = read_ascii(ascii_path)
    slots = [k for k in range(nparts) if k not in keep]
    if len(parts) != len(slots):
        raise SystemExit(f"The .ascii has {len(parts)} parts; with {len(keep)} kept, this mesh needs "
                         f"exactly {len(slots)}.")
    strides = [pt["vb"]["strides"] for pt in la["parts"]]
    if any(st != [12, 16, 12] for st in strides):
        raise SystemExit("Unsupported vertex layout in this mesh.")
    built = original_parts(la, orig_geo)
    for slot, part in zip(slots, parts):
        built[slot] = build_part(part, g["bone"])
    data = assemble(built, strides)
    if len(data) > g["size"]:
        raise SystemExit(f"New geometry is {len(data)} bytes but only {g['size']} fit. Simplify the model.")
    print(f"New geometry: {len(data)} bytes (room for {g['size']})"
          + (f", keeping original part(s) {keep}" if keep else ""))

    # .core: counts and bounding boxes for the replaced parts
    core = bytearray(orig_core)
    total_box = list(struct.unpack_from("<6f", orig_core, la["bbox_at"]))
    for slot, part in zip(slots, parts):
        pt = la["parts"][slot]
        _, _, nv, nidx = built[slot]
        struct.pack_into("<I", core, pt["vb"]["at"] + 16, nv)
        struct.pack_into("<I", core, pt["ib"]["at"] + 16, nidx)
        pa = pt["prim"]["at"]
        if struct.unpack_from("<I", orig_core, pa + 86)[0] != pt["ib"]["count"]:
            raise SystemExit("Unexpected primitive layout in this .core.")
        struct.pack_into("<I", core, pa + 86, nidx)
        nb = union(list(struct.unpack_from("<6f", orig_core, pa + 54)), bbox(part["verts"]))
        struct.pack_into("<6f", core, pa + 54, *nb)
        total_box = union(total_box, nb)
        if la["draw"]:
            da = la["draw"]["at"] + 44 + 41 * slot
            if struct.unpack_from("<I", orig_core, da)[0] == pt["vb"]["nv"]:
                struct.pack_into("<I", core, da, nv)
    struct.pack_into("<6f", core, la["bbox_at"], *total_box)

    # index record: size of the geometry entry
    rec = locator_record_offset(pk.dir, g["package"], g["entry"])
    loc_path = os.path.join(pk.dir, "PackFileLocators.bin")
    with open(loc_path, "rb") as f:
        f.seek(rec)
        cur = f.read(16)
    h, off, size = struct.unpack("<QII", cur)
    if f"{h:016x}" != g["key"] or off != g["offset"]:
        raise SystemExit("Index entry doesn't match the map. Run 'find' again.")
    if first_time:
        if size != g["size"]:
            raise SystemExit("Index entry size changed since 'find'. Run 'find' again.")
        backup["orig_core"] = orig_core.hex()
        backup["orig_geometry"] = orig_geo.hex()
        backup["locator"] = {"at": rec, "bytes": cur.hex()}

    writes = [(g["package"], g["offset"], data, "geometry"),
              (cl["package"], cl["offset"], bytes(core), ".core")]
    sl = m.get("lod_setup")
    if sl:
        setup_now = pk.get(sl["package"]).read(sl["offset"], sl["size"])
        if first_time and T.sha1(setup_now) != sl["sha1"]:
            raise SystemExit("The setup .core changed since 'find'. Run 'find' again.")
        setup = bytearray(setup_now)
        for k, d in enumerate(sl["distances"][1:]):      # never switch away from full quality
            struct.pack_into("<f", setup, d, 1000.0 + k)
        writes.append((sl["package"], sl["offset"], bytes(setup), "detail-level setup"))

    for pkg, off, blob, label in writes:
        b = backup.setdefault("packages", {}).setdefault(pkg, {"chunks": {}})
        pk.get(pkg).write(off, blob, b)
        json.dump(backup, open(bpath, "w"))
        if pk.get(pkg).read(off, len(blob)) != blob:
            raise SystemExit(f"Verification failed after writing {label}. Run 'restore'.")
        print(f"  wrote {label} -> {pkg}")
    with open(loc_path, "r+b") as f:
        f.seek(rec + 12)
        f.write(struct.pack("<I", len(data)))
    print("  updated geometry size in PackFileLocators.bin")
    print(f"Backup saved to {bpath}")
    print("Done. Launch the game to test. Use 'restore' to undo."
          + ("" if sl else " (Lower detail levels still show the old model at a distance.)"))


def cmd_restore(args, pk):
    bpath = backup_path(args[0])
    if not os.path.exists(bpath):
        raise SystemExit("No backup found; nothing to restore.")
    backup = json.load(open(bpath))
    for pkg, b in backup.get("packages", {}).items():
        pk.get(pkg).restore(b)
        print(f"  restored {pkg}")
    if "locator" in backup:
        with open(os.path.join(pk.dir, "PackFileLocators.bin"), "r+b") as f:
            f.seek(backup["locator"]["at"])
            f.write(bytes.fromhex(backup["locator"]["bytes"]))
        print("  restored PackFileLocators.bin entry")
    os.rename(bpath, bpath + ".used")
    print("Done.")


def main(codec_factory=T.LZ4):
    argv = sys.argv[1:]
    package_dir = T.get_package_dir(argv)
    if not argv or argv[0] not in ("find", "swap", "restore") or len(argv) < 2:
        print(__doc__)
        return
    if not os.path.exists(os.path.join(package_dir, "PackFileLocators.bin")):
        raise SystemExit(f"Can't find PackFileLocators.bin in {package_dir}. Use --package <folder>.")
    pk = T.Packages(package_dir, codec_factory(package_dir))
    {"find": cmd_find, "swap": cmd_swap, "restore": cmd_restore}[argv[0]](argv[1:], pk)


if __name__ == "__main__":
    try:
        main()
    except PermissionError:
        raise SystemExit("Permission denied writing game files. Run the command prompt as Administrator, "
                         "and make sure the game is closed.")
