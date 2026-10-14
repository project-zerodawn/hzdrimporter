#!/usr/bin/env python3
"""
hzdr_texpatch.py - replace textures in Horizon Zero Dawn Remastered (same size, same format only)

Commands
  find     Locate a texture set's .core and its streamed mip data inside the game's package files.
           Needs the .core and the .dds files exported by your extraction tool (unedited originals).
  patch    Write edited .dds files into the game files. Backs up everything it changes first.
  restore  Undo a patch using the backup.

Examples
  py hzdr_texpatch.py find    <SET>.core <SET>00_original.dds <SET>01_original.dds ...
  py hzdr_texpatch.py patch   <SET>.map.json <SET>01_edited.dds ...
  py hzdr_texpatch.py restore <SET>.map.json

  <SET>  the texture set's file name without .core, e.g. "characterhead_set" (made-up example)

Rules for edited .dds files
  - File name must start with the texture's name (e.g. <SET>01...).
  - Same width/height and the same compression format as the original (BC1/DXT1, BC7, ...).
  - Must include the FULL mipmap chain (export with "generate mip maps" on).

The package files are DSAR archives made of LZ4-compressed chunks. For speed, install the lz4
package once with:  py -m pip install lz4   (a slower built-in fallback is used otherwise).
Changed chunks are written back in place when they fit, otherwise appended to the end of the
same package file, and the archive's chunk table is updated. PackFileLocators.bin is only changed
when an edited texture set is larger than the original (its record is pointed at the new data).
"""
import bisect
import hashlib
import json
import os
import struct
import sys

DEFAULT_PACKAGE_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Horizon Zero Dawn Remastered\LocalCacheDX12\package"
HERE = os.path.dirname(os.path.abspath(__file__))


def sha1(b):
    return hashlib.sha1(b).hexdigest()


# ----------------------------------------------------------------------------- LZ4
def lz4_decompress_py(src, raw_size):
    """Pure-Python LZ4 block decompressor (fallback when the lz4 package isn't installed)."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255:
                    break
        out += src[i:i + lit]; i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            for k in range(ml):
                out.append(out[start + k])
    if len(out) != raw_size:
        raise ValueError(f"LZ4 size mismatch ({len(out)} vs {raw_size})")
    return bytes(out)


def lz4_store_py(raw):
    """Valid LZ4 block holding everything as literals (no compression). Used only as a fallback."""
    lit = len(raw)
    out = bytearray()
    if lit < 15:
        out.append(lit << 4)
    else:
        out.append(0xF0)
        rest = lit - 15
        while rest >= 255:
            out.append(255); rest -= 255
        out.append(rest)
    return bytes(out) + raw


class LZ4:
    """The package chunks are LZ4 block-compressed (chunk type 3)."""
    def __init__(self, package_dir=None):
        try:
            import lz4.block
            self.lib = lz4.block
            print("Using lz4 package")
        except ImportError:
            self.lib = None
            print("Note: the lz4 package isn't installed, using a slower built-in fallback.\n"
                  "      For much faster searching run:  py -m pip install lz4")

    def decompress(self, comp, raw_size):
        if self.lib:
            return self.lib.decompress(comp, uncompressed_size=raw_size)
        return lz4_decompress_py(comp, raw_size)

    def compress(self, raw):
        if self.lib:
            comp = self.lib.compress(raw, mode="high_compression", compression=9, store_size=False)
        else:
            comp = lz4_store_py(raw)
        if self.decompress(comp, len(raw)) != raw:
            raise RuntimeError("LZ4 round-trip check failed")
        return comp


# ----------------------------------------------------------------------------- DSAR archives
CHUNK_FMT = "<QQIIB7s"  # raw offset, compressed offset, raw size, compressed size, type, padding


class Archive:
    def __init__(self, path, codec):
        self.path, self.codec, self.cache = path, codec, {}
        self.load_table()

    def load_table(self):
        with open(self.path, "rb") as f:
            head = f.read(32)
            magic, major, minor, count, table_end, total = struct.unpack_from("<4sHHIIQ", head, 0)
            if magic != b"DSAR":
                raise SystemExit(f"{self.path}: not a DSAR archive")
            table = f.read(count * 32)
        self.count, self.total = count, total
        self.chunks = [list(struct.unpack_from(CHUNK_FMT, table, 32 * i)) for i in range(count)]
        self.starts = [c[0] for c in self.chunks]

    def chunk_range(self, offset, size):
        first = bisect.bisect_right(self.starts, offset) - 1
        last = bisect.bisect_right(self.starts, offset + size - 1) - 1
        return range(first, last + 1)

    def chunk_raw(self, i):
        if i not in self.cache:
            raw_off, comp_off, raw_size, comp_size, ctype, _ = self.chunks[i]
            with open(self.path, "rb") as f:
                f.seek(comp_off)
                comp = f.read(comp_size)
            if ctype == 3:
                data = self.codec.decompress(comp, raw_size)
            elif ctype == 0 and comp_size == raw_size:
                data = comp
            else:
                raise RuntimeError(f"{self.path}: chunk {i} has unknown compression type {ctype}")
            if len(self.cache) > 64:
                self.cache.clear()
            self.cache[i] = data
        return self.cache[i]

    def read(self, offset, size):
        out = bytearray()
        for i in self.chunk_range(offset, size):
            c = self.chunks[i]
            data = self.chunk_raw(i)
            a = max(offset, c[0]) - c[0]
            b = min(offset + size, c[0] + c[2]) - c[0]
            out += data[a:b]
        return bytes(out)

    def write(self, offset, data, backup):
        """Replace raw bytes at offset; recompress the touched chunks. `backup` records originals."""
        size = len(data)
        touched = list(self.chunk_range(offset, size))
        new_chunks = {}
        for i in touched:
            c = self.chunks[i]
            raw = bytearray(self.chunk_raw(i))
            a = max(offset, c[0])
            b = min(offset + size, c[0] + c[2])
            raw[a - c[0]:b - c[0]] = data[a - offset:b - offset]
            new_chunks[i] = bytes(raw)
        with open(self.path, "r+b") as f:
            f.seek(0, 2)
            end = f.tell()
            backup.setdefault("orig_size", end)
            for i, raw in new_chunks.items():
                c = self.chunks[i]
                key = str(i)
                if key not in backup["chunks"]:
                    f.seek(c[1])
                    backup["chunks"][key] = {"entry": struct.pack(CHUNK_FMT, *c).hex(),
                                             "bytes": f.read(c[3]).hex()}
                comp = self.codec.compress(raw)
                orig_entry = struct.unpack(CHUNK_FMT, bytes.fromhex(backup["chunks"][key]["entry"]))
                if len(comp) <= orig_entry[3]:
                    comp_off = orig_entry[1]              # fits in the original slot
                else:
                    comp_off = (end + 4095) // 4096 * 4096  # append to end of file
                    end = comp_off + len(comp)
                f.seek(comp_off)
                f.write(comp)
                c[1], c[3], c[4] = comp_off, len(comp), 3
                f.seek(32 + 32 * i)
                f.write(struct.pack(CHUNK_FMT, *c))
                self.cache[i] = raw
        backup["chunks_written"] = sorted(set(backup.get("chunks_written", []) + touched))

    def restore(self, backup):
        with open(self.path, "r+b") as f:
            for key, rec in backup["chunks"].items():
                entry = struct.unpack(CHUNK_FMT, bytes.fromhex(rec["entry"]))
                f.seek(entry[1])
                f.write(bytes.fromhex(rec["bytes"]))
                f.seek(32 + 32 * int(key))
                f.write(bytes.fromhex(rec["entry"]))
            # Never cut the file short: other mods may have appended their own data after ours.
        self.cache.clear()
        self.load_table()


class Packages:
    def __init__(self, package_dir, codec):
        self.dir, self.codec, self.open = package_dir, codec, {}

    def get(self, name):
        if name not in self.open:
            self.open[name] = Archive(os.path.join(self.dir, name), self.codec)
        return self.open[name]


def read_index(package_dir):
    with open(os.path.join(package_dir, "PackFileLocators.bin"), "rb") as f:
        d = f.read()
    p = 0
    (n,) = struct.unpack_from("<I", d, p); p += 4
    files = []
    for _ in range(n):
        (l,) = struct.unpack_from("<I", d, p); p += 4
        name = d[p:p + l].decode("ascii"); p += l
        (c,) = struct.unpack_from("<I", d, p); p += 4
        files.append((name, [struct.unpack_from("<QII", d, p + 16 * k) for k in range(c)]))
        p += 16 * c
    return files


# ----------------------------------------------------------------------------- DDS
BLOCK_BYTES_DXGI = {70: 8, 71: 8, 72: 8, 73: 16, 74: 16, 75: 16, 76: 16, 77: 16, 78: 16,
                    79: 8, 80: 8, 81: 8, 82: 16, 83: 16, 84: 16, 94: 16, 95: 16, 96: 16,
                    97: 16, 98: 16, 99: 16}
BLOCK_BYTES_FOURCC = {b"DXT1": 8, b"DXT3": 16, b"DXT5": 16, b"ATI1": 8, b"BC4U": 8,
                      b"ATI2": 16, b"BC5U": 16}
# The same format can be labelled two ways; treat these as equal.
FORMAT_ALIASES = {"DXT1": "BC1", "DXGI 71": "BC1", "DXGI 72": "BC1",
                  "DXT5": "BC3", "DXGI 77": "BC3", "DXGI 78": "BC3",
                  "ATI1": "BC4", "BC4U": "BC4", "DXGI 80": "BC4",
                  "ATI2": "BC5", "BC5U": "BC5", "DXGI 83": "BC5",
                  "DXGI 98": "BC7", "DXGI 99": "BC7"}


def read_dds(path):
    with open(path, "rb") as f:
        d = f.read()
    if d[:4] != b"DDS ":
        raise SystemExit(f"{path}: not a DDS file")
    height, width = struct.unpack_from("<II", d, 12)
    (mips,) = struct.unpack_from("<I", d, 28)
    mips = max(mips, 1)
    fourcc = d[84:88]
    if fourcc == b"DX10":
        (dxgi,) = struct.unpack_from("<I", d, 128)
        if dxgi not in BLOCK_BYTES_DXGI:
            raise SystemExit(f"{path}: unsupported DXGI format {dxgi} (needs a block-compressed format)")
        bb, fmt, start = BLOCK_BYTES_DXGI[dxgi], f"DXGI {dxgi}", 148
    else:
        if fourcc not in BLOCK_BYTES_FOURCC:
            raise SystemExit(f"{path}: unsupported format {fourcc!r} (needs a block-compressed format)")
        bb, fmt, start = BLOCK_BYTES_FOURCC[fourcc], fourcc.decode(), 128
    sizes, w, h = [], width, height
    for _ in range(mips):
        sizes.append(max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bb)
        w, h = max(1, w // 2), max(1, h // 2)
    data = d[start:]
    if len(data) < sum(sizes):
        raise SystemExit(f"{path}: file is shorter than its header says")
    out, p = [], 0
    for s in sizes:
        out.append(data[p:p + s]); p += s
    return {"width": width, "height": height, "format": FORMAT_ALIASES.get(fmt, fmt), "mips": out}


def full_mip_count(w, h):
    n = 1
    while w > 1 or h > 1:
        w, h = max(1, w // 2), max(1, h // 2); n += 1
    return n


# ----------------------------------------------------------------------------- .core parsing
def find_textures(core):
    found = []
    for i in range(0, len(core) - 16):
        total, emb, strm, smips = struct.unpack_from("<IIII", core, i)
        if total == emb + 12 and 0 < emb and i + 16 + emb <= len(core) and smips <= 16 and strm % 16 == 0:
            found.append({"name": last_name_before(core, i), "header": i, "data": i + 16,
                          "embedded": emb, "streamed": strm, "stream_mips": smips})
    return found


def last_name_before(core, end):
    best = None
    for i in range(0, end - 8):
        (l,) = struct.unpack_from("<I", core, i)
        if 3 <= l <= 200 and i + 8 + l <= end:
            s = core[i + 8:i + 8 + l]
            if all(48 <= c <= 57 or 65 <= c <= 90 or 97 <= c <= 122 or c in b"_-." for c in s):
                best = s.decode()
    return best


# ----------------------------------------------------------------------------- commands
def mip_sizes(t):
    """Byte sizes of a texture's streamed mip levels, largest first."""
    total, sizes = t["streamed"], []
    n = t["stream_mips"]
    s = total * 3 * 4 ** (n - 1) // (4 ** n - 1) if n else 0   # size of the largest level
    for k in range(n):
        sizes.append(s >> (2 * k))
    if sum(sizes) != total:
        raise SystemExit(f"{t['name']}: can't split {total} bytes into {n} mip levels")
    return sizes


def stream_layout(textures):
    """The game stores a texture set's streamed mips in ONE entry: every mip level of every texture,
    largest first; levels of equal size keep the .core order of their textures.
    (For sets whose textures all have the same size this is: level 0 of every texture, then level 1 ...)
    Returns {texture name: [(offset in entry, size), ...]} and the total size."""
    sizes = {t["name"]: mip_sizes(t) for t in textures}
    chunks = [(-sz, k, lvl, t["name"]) for k, t in enumerate(textures) for lvl, sz in enumerate(sizes[t["name"]])]
    chunks.sort()
    layout = {t["name"]: [None] * len(sizes[t["name"]]) for t in textures}
    off = 0
    for negsz, k, lvl, name in chunks:
        layout[name][lvl] = (off, -negsz)
        off += -negsz
    return layout, off


def _bc1_block_rgb(b):
    """Decode one 8-byte BC1 block to 16 (r,g,b) tuples (pure Python, used to recognise re-encoded exports)."""
    c0, c1, bits = struct.unpack("<HHI", b)
    def rgb(c): return ((c >> 11) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31)
    a, b_ = rgb(c0), rgb(c1)
    if c0 > c1:
        pal = [a, b_, tuple((2 * x + y) // 3 for x, y in zip(a, b_)), tuple((x + 2 * y) // 3 for x, y in zip(a, b_))]
    else:
        pal = [a, b_, tuple((x + y) // 2 for x, y in zip(a, b_)), (0, 0, 0)]
    return [pal[(bits >> (2 * i)) & 3] for i in range(16)]


def _bc1_difference(x, y, nblocks=400):
    """Mean colour difference (0-255) between two BC1 mip levels, sampled at the same blocks."""
    n = min(len(x), len(y)) // 8
    step = max(1, n // nblocks); tot = tsw = cnt = 0
    for k in range(0, n, step):
        for p, q in zip(_bc1_block_rgb(x[8 * k:8 * k + 8]), _bc1_block_rgb(y[8 * k:8 * k + 8])):
            tot += abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2]); cnt += 3
            tsw += abs(p[0] - q[2]) + abs(p[1] - q[1]) + abs(p[2] - q[0])
    return min(tot, tsw) / max(cnt, 1)


def layout_variants(textures):
    """Every plausible way a set's streamed mips can be ordered inside one entry."""
    import itertools
    sizes0 = {t["name"]: mip_sizes(t) for t in textures}
    out = []
    for perm in itertools.permutations(textures):
        nlev = max(len(sizes0[t["name"]]) for t in perm)
        for small_first in (False, True):
            levels = list(range(nlev))[::-1] if small_first else list(range(nlev))
            for mode in ("interleaved", "contiguous"):
                lay = {t["name"]: [None] * len(sizes0[t["name"]]) for t in perm}; off = 0
                if mode == "interleaved":
                    order = [(t, l) for l in levels for t in perm if l < len(sizes0[t["name"]])]
                else:
                    order = [(t, l) for t in perm for l in levels if l < len(sizes0[t["name"]])]
                for t, l in order:
                    sz = sizes0[t["name"]][l]; lay[t["name"]][l] = (off, sz); off += sz
                out.append((f"{'/'.join(t['name'][-5:] for t in perm)} {mode} {'small-first' if small_first else 'large-first'}", lay))
    return out


def _bc1_decode_level(b, w, h):
    """Decode a BC1 level to a flat list of rows of (r,g,b)."""
    bw, bh = max(1, w // 4), max(1, h // 4)
    img = [[None] * w for _ in range(h)]
    for by in range(bh):
        for bx in range(bw):
            px = _bc1_block_rgb(b[8 * (by * bw + bx):8 * (by * bw + bx) + 8])
            for i in range(16):
                y, x = by * 4 + i // 4, bx * 4 + i % 4
                if y < h and x < w:
                    img[y][x] = px[i]
    return img


def _embedded_match(core, t, level_bytes):
    """Colour difference between a streamed level (the smallest streamed one) shrunk by 2 and the
    first level kept inside the .core: identifies the entry without needing any exported file."""
    first = core[t["data"]:t["data"] + t["embedded"]]
    n = len(level_bytes) // 8; side = int(round((n ** 0.5))) * 4
    if side * side // 16 * 8 != len(level_bytes) or side < 8:
        return 999
    big = _bc1_decode_level(level_bytes, side, side)
    small = _bc1_decode_level(first[:(side // 8) * (side // 8) * 8], side // 2, side // 2)
    tot = cnt = 0
    for y in range(0, side // 2, 2):
        for x in range(0, side // 2, 2):
            for c in range(3):
                a = (big[2 * y][2 * x][c] + big[2 * y + 1][2 * x][c] + big[2 * y][2 * x + 1][c] + big[2 * y + 1][2 * x + 1][c]) / 4
                tot += abs(a - small[y][x][c]); cnt += 1
    return tot / max(cnt, 1)

def cmd_find(args, pk):
    core_path, dds_paths = args[0], args[1:]
    core = open(core_path, "rb").read()
    textures = find_textures(core)
    if not textures:
        raise SystemExit("No textures found in that .core file.")
    print(f"Textures in {os.path.basename(core_path)}:")
    for t in textures:
        print(f"  {t['name']}: {t['embedded']} bytes embedded, {t['streamed']} bytes streamed "
              f"({t['stream_mips']} mips)")
    print("Reading PackFileLocators.bin ...")
    index = read_index(pk.dir)

    core_loc = None
    for pkg, entries in index:
        if pkg.endswith(".stream"):
            continue
        for h, off, size in entries:
            if size == len(core) and pk.get(pkg).read(off, size) == core:
                core_loc = {"package": pkg, "offset": off, "size": size, "hash": f"{h:016x}"}
                break
        if core_loc:
            break
    if not core_loc:
        raise SystemExit("Could not find this .core inside the packages (game updated, or wrong file?).")
    print(f"  .core found in {core_loc['package']} at offset {core_loc['offset']}")

    # exported top mips, used to recognise the right entry
    probes = {}; fulls = {}
    for t in textures:
        dds_path = next((p for p in dds_paths
                         if os.path.basename(p).lower().startswith(t["name"].lower())), None)
        if dds_path:
            dds = read_dds(dds_path)
            t.update(format=dds["format"], width=dds["width"], height=dds["height"])
            probes[t["name"]] = dds["mips"][0][:4096]
            fulls[t["name"]] = dds["mips"][0]
    streamed = [t for t in textures if t["streamed"]]
    if not probes:
        raise SystemExit("Give the exported .dds files too, so the streamed data can be recognised.")

    # try the whole set in one entry first, then each texture on its own
    # every combination of the set's streamed textures, largest first (a texture that is shared with another
    # set can be stored elsewhere, so the entry may hold only some of them)
    import itertools
    groups = [list(c) for n in range(len(streamed), 0, -1) for c in itertools.combinations(streamed, n)]
    found_groups = []
    covered = set()
    for group in groups:
        if any(t["name"] in covered for t in group):
            continue
        checkable = [t for t in group if t["name"] in probes]
        if not checkable:
            continue
        layout, total = stream_layout(group)
        print(f"  searching for streamed data of {', '.join(t['name'] for t in group)} "
              f"({total} bytes) ...")
        hit = None
        for pkg, entries in index:
            if not pkg.endswith(".stream"):
                continue
            for h, off, size in entries:
                if size != total:
                    continue
                a = pk.get(pkg)
                if all(a.read(off + layout[t["name"]][0][0], 4096) == probes[t["name"]] for t in checkable):
                    hit = (pkg, h, off, size)
                    break
            if hit:
                break
        if not hit:
            # the exported .dds may have been re-encoded by the export tool, so its bytes differ from the
            # game's although the picture is the same: compare decoded colours of entries of the right size
            fuzzy = [t for t in checkable if t.get("format") == "BC1"]
            scored = []
            for pkg, entries in index:
                if not pkg.endswith(".stream") or not fuzzy:
                    continue
                for h, off, size in entries:
                    if size != total:
                        continue
                    a = pk.get(pkg)
                    d = max(_bc1_difference(a.read(off + layout[t["name"]][0][0], layout[t["name"]][0][1]),
                                            fulls[t["name"]]) for t in fuzzy)
                    scored.append((d, pkg, h, off, size))
            scored.sort()
            print(f"    no exact match; {len(scored)} entries of that size compared by colour"
                  + (f" (best difference {scored[0][0]:.1f}" + (f", next {scored[1][0]:.1f}" if len(scored) > 1 else "") + ")" if scored else ""))
            if scored and scored[0][0] < 8 and (len(scored) == 1 or scored[1][0] > 2 * scored[0][0] + 4):
                hit = scored[0][1:]
        if hit:
            pkg, h, off, size = hit
            data = pk.get(pkg).read(off, size)
            found_groups.append({"package": pkg, "offset": off, "size": size, "hash": f"{h:016x}",
                                 "sha1": sha1(data), "textures": [t["name"] for t in group],
                                 "layout": layout})
            covered.update(t["name"] for t in group)
            print(f"    found in {pkg} at offset {off}")
        else:
            print("    not found")

    if any(t["name"] not in covered for t in streamed):
        # deep search: entries whose size matches the whole set, every mip ordering, checked against the
        # small levels stored inside the .core itself (so it works even if the exported .dds differ)
        rest = [t for t in streamed if t["name"] not in covered]
        bc1 = [t for t in rest if t.get("format", "BC1") == "BC1" and t["embedded"] >= 2048]
        for group in ([streamed] + ([rest] if rest != streamed else [])):
            total = sum(t["streamed"] for t in group)
            variants = layout_variants(group)
            print(f"  deep search: entries of {total} bytes, {len(variants)} mip orderings ...")
            best = []
            for pkg, entries in index:
                if not pkg.endswith(".stream"):
                    continue
                for h, off, size in entries:
                    if size != total:
                        continue
                    data = pk.get(pkg).read(off, size)
                    for t in group:
                        if t["name"] in fulls:
                            f = data.find(fulls[t["name"]][:4096])
                            if f >= 0:
                                print(f"    exact piece of {t['name']} found in {pkg} (key {h:016x}) at +{f}")
                    for vname, lay in variants:
                        ds = []
                        for t in group:
                            if t in bc1:
                                lo, sz = lay[t["name"]][-1]
                                ds.append(_embedded_match(core, t, data[lo:lo + sz]))
                        if ds:
                            best.append((max(ds), vname, pkg, h, off, size, lay))
            best.sort(key=lambda b: b[0])
            for b in best[:3]:
                print(f"    {b[0]:6.1f}  {b[2]} key {b[3]:016x}  {b[1]}")
            if best and best[0][0] < 6 and (len(best) == 1 or best[1][0] > best[0][0] + 6 or best[1][3] == best[0][3]):
                d, vname, pkg, h, off, size, lay = best[0]
                data = pk.get(pkg).read(off, size)
                found_groups.append({"package": pkg, "offset": off, "size": size, "hash": f"{h:016x}",
                                     "sha1": sha1(data), "textures": [t["name"] for t in group], "layout": lay})
                covered.update(t["name"] for t in group)
                print(f"    found in {pkg} at offset {off} ({vname})")
                break

    for t in streamed:
        if t["name"] not in covered:
            print(f"  WARNING: streamed data for {t['name']} not found; it can't be patched.")
    mapping = {"core_file": os.path.basename(core_path), "core": core_loc, "core_sha1": sha1(core),
               "textures": textures, "streams": found_groups}
    out = os.path.splitext(core_path)[0] + ".map.json"
    with open(out, "w") as f:
        json.dump(mapping, f, indent=2)
    print(f"Saved {out}")


def backup_path(map_path):
    return os.path.splitext(map_path)[0] + ".backup.json"


def cmd_patch(args, pk):
    map_path, dds_paths = args[0], args[1:]
    m = json.load(open(map_path))
    if "streams" not in m:
        raise SystemExit("This map file is from an older version of the script. Run 'find' again.")
    bpath = backup_path(map_path)
    backup = json.load(open(bpath)) if os.path.exists(bpath) else {}
    first_time = not backup

    core_loc = m["core"]
    core_now = pk.get(core_loc["package"]).read(core_loc["offset"], core_loc["size"])
    if first_time and sha1(core_now) != m["core_sha1"]:
        raise SystemExit("The .core in the package has changed since 'find' (game update?). Run 'find' again.")
    new_core = bytearray(core_now)

    streams = {}
    for g in m["streams"]:
        data = pk.get(g["package"]).read(g["offset"], g["size"])
        if first_time and sha1(data) != g["sha1"]:
            raise SystemExit("Streamed data changed since 'find' (game update?). Run 'find' again.")
        streams[id(g)] = (g, bytearray(data))

    changed = set(); grow_tex = []
    if backup.get("_grown") and not first_time:
        raise SystemExit("A larger texture is already installed for this set. Run the uninstall (restore) first, then install again.")
    for dds_path in dds_paths:
        base = os.path.basename(dds_path).lower()
        # the most specific name wins (e.g. 'example_set02' over 'example_set'), and stray
        # near-empty entries that 'find' picked up by accident are never used
        cands = [t for t in m["textures"] if base.startswith(t["name"].lower())
                 and not (t["streamed"] == 0 and t["embedded"] < 64)]
        t = max(cands, key=lambda t: len(t["name"])) if cands else None
        if not t:
            raise SystemExit(f"{dds_path}: name doesn't start with any texture name in the map "
                             f"({', '.join(x['name'] for x in m['textures'])})")
        dds = read_dds(dds_path)
        if t.get("width") and (dds["width"], dds["height"]) != (t["width"], t["height"]):
            fw, fh = dds["width"] / t["width"], dds["height"] / t["height"]
            if not (fw == fh and fw > 1 and fw == int(fw) and (int(fw) & (int(fw) - 1)) == 0 and t["streamed"]):
                raise SystemExit(f"{dds_path}: size is {dds['width']}x{dds['height']}, "
                                 f"must be {t['width']}x{t['height']} (or 2x/4x larger in both directions)")
            grow_tex.append((t, dds, int(fw).bit_length() - 1))
            print(f"  {t['name']}: enlarged {t['width']}x{t['height']} -> {dds['width']}x{dds['height']}")
            continue
        if t.get("format") and dds["format"] != t["format"]:
            raise SystemExit(f"{dds_path}: format is {dds['format']}, must be {t['format']} (same as the original)")
        need = full_mip_count(dds["width"], dds["height"])
        if len(dds["mips"]) != need:
            raise SystemExit(f"{dds_path}: has {len(dds['mips'])} mip levels, needs all {need}. "
                             "Re-export with mipmaps enabled.")
        sm = t["stream_mips"]
        embedded = b"".join(dds["mips"][sm:])
        if len(embedded) != t["embedded"]:
            # explain exactly what doesn't line up, so it can be fixed
            sizes=[len(x) for x in dds["mips"]]
            print(f"{dds_path}: data size doesn't match the original layout.")
            print(f"  map entry used: '{t['name']}'  (stream_mips={sm}, embedded={t['embedded']}, streamed={t['streamed']}, "
                  f"size={t.get('width')}x{t.get('height')}, format={t.get('format')})")
            print(f"  this file: {dds['width']}x{dds['height']}, {dds['format']}, {len(sizes)} mips, "
                  f"mip sizes {sizes}")
            print(f"  this file's levels {sm}+ add up to {len(embedded)} bytes; levels 0-{sm-1} to {sum(sizes[:sm])} bytes")
            for k in range(len(sizes)+1):
                if sum(sizes[k:])==t["embedded"]:
                    print(f"  -> the game keeps {len(sizes)-k} small levels inside the .core (from level {k}), not {len(sizes)-sm}")
            print("  all textures in the map:")
            for x in m["textures"]:
                print(f"    {x['name']}: embedded={x['embedded']} streamed={x['streamed']} stream_mips={x['stream_mips']} "
                      f"size={x.get('width')}x{x.get('height')} format={x.get('format')}")
            raise SystemExit("Please send all of the text above.")
        if t["streamed"]:
            g = next((g for g in m["streams"] if t["name"] in g["textures"]), None)
            if not g:
                raise SystemExit(f"{t['name']}: streamed data location unknown (see 'find' output).")
            buf = streams[id(g)][1]
            for level, (off, size) in enumerate(g["layout"][t["name"]]):
                if len(dds["mips"][level]) != size:
                    raise SystemExit(f"{dds_path}: mip {level} size doesn't match the original.")
                buf[off:off + size] = dds["mips"][level]
            changed.add(id(g))
        new_core[t["data"]:t["data"] + t["embedded"]] = embedded
        print(f"  prepared {t['name']}")

    grow_writes = []
    if grow_tex:
        # textures larger than the originals: more mip levels are streamed (the small levels kept in the .core stay
        # the same size), the streamed entry is rebuilt with the new sizes and added to the end of its package,
        # and the .core gets the new size, level count and streamed size
        for g in m["streams"]:
            mine = [x for x in grow_tex if x[0]["name"] in g["textures"]]
            if not mine:
                continue
            if len(mine) != len(g["textures"]):
                raise SystemExit("To enlarge textures, every texture of the set has to be given, all enlarged the same way.")
            ts = [next(t for t in m["textures"] if t["name"] == n) for n in g["textures"]]
            new_ts = []
            for t in ts:
                tt, dds, extra = next(x for x in mine if x[0]["name"] == t["name"])
                sm = t["stream_mips"] + extra
                if len(dds["mips"]) != full_mip_count(dds["width"], dds["height"]):
                    raise SystemExit(f"{t['name']}: the larger texture needs all its mip levels.")
                embedded = b"".join(dds["mips"][sm:])
                if len(embedded) != t["embedded"]:
                    raise SystemExit(f"{t['name']}: the larger texture's small levels don't match the original layout.")
                nt = dict(t, streamed=sum(len(x) for x in dds["mips"][:sm]), stream_mips=sm, width=dds["width"], height=dds["height"])
                new_ts.append((nt, dds, embedded))
            lay, total = stream_layout([nt for nt, _, _ in new_ts])
            buf = bytearray(total)
            for nt, dds, embedded in new_ts:
                for level, (off, size) in enumerate(lay[nt["name"]]):
                    assert len(dds["mips"][level]) == size
                    buf[off:off + size] = dds["mips"][level]
                h = nt["header"]
                struct.pack_into("<HH", new_core, h - 30, nt["width"], nt["height"])
                new_core[h - 24] = full_mip_count(nt["width"], nt["height"])
                struct.pack_into("<II", new_core, h + 8, nt["streamed"], nt["stream_mips"])
                new_core[nt["data"]:nt["data"] + nt["embedded"]] = embedded
                print(f"  prepared {nt['name']} ({nt['width']}x{nt['height']}, {nt['stream_mips']} streamed levels)")
            hits = [i for i in range(0, len(new_core) - 3) if struct.unpack_from("<I", new_core, i)[0] == g["size"]]
            if len(hits) != 1:
                raise SystemExit(f"Can't find the streamed size of this set in the .core ({len(hits)} matches); not changing anything.")
            struct.pack_into("<I", new_core, hits[0], total)
            grow_writes.append((g, bytes(buf)))
    writes = [(g["package"], g["offset"], bytes(buf), "streamed mips")
              for key, (g, buf) in streams.items() if key in changed]
    writes.append((core_loc["package"], core_loc["offset"], bytes(new_core), "small mips in .core"))
    for pkg, off, data, label in writes:
        b = backup.setdefault(pkg, {"chunks": {}})
        pk.get(pkg).write(off, data, b)
        with open(bpath, "w") as f:      # save after every write so a crash can still be undone
            json.dump(backup, f)
        if pk.get(pkg).read(off, len(data)) != data:
            raise SystemExit(f"Verification failed after writing {label}. Run 'restore'.")
        print(f"  wrote {label} -> {pkg}")
    if grow_writes:
        import hzdr_grow as G, hzdr_meshswap as MS
        index = read_index(pk.dir)
        loc_path = os.path.join(pk.dir, "PackFileLocators.bin")
        for g, data in grow_writes:
            k = next(i for pkg_, entries in index if pkg_ == g["package"] for i, (h, off, size) in enumerate(entries) if f"{h:016x}" == g["hash"])
            gb = {}; backup.setdefault("_grown", []).append(gb)
            with open(bpath, "w") as f: json.dump(backup, f)
            new_off = G.append_entry(pk.get(g["package"]), data, gb)
            with open(bpath, "w") as f: json.dump(backup, f)
            rec = MS.locator_record_offset(pk.dir, g["package"], k)
            with open(loc_path, "r+b") as f:
                f.seek(rec); old = f.read(16)
                backup.setdefault("_locators", []).append({"at": rec, "bytes": old.hex()})
                with open(bpath, "w") as f2: json.dump(backup, f2)
                f.seek(rec + 8); f.write(struct.pack("<II", new_off, len(data)))
            print(f"  wrote enlarged streamed mips -> {g['package']} ({len(data)} bytes, added at the end)")
    print(f"Backup saved to {bpath}")
    print("Done. Launch the game to test. Use 'restore' to undo.")


def cmd_restore(args, pk):
    bpath = backup_path(args[0])
    if not os.path.exists(bpath):
        raise SystemExit("No backup found; nothing to restore.")
    backup = json.load(open(bpath))
    if backup.get("_grown"):
        import hzdr_grow as G
        for gb in reversed(backup["_grown"]):
            if "head" in gb:
                G.undo(gb); print(f"  restored chunk table of {os.path.basename(gb['path'])}")
    for pkg, b in backup.items():
        if pkg.startswith("_"):
            continue
        pk.get(pkg).restore(b)
        print(f"  restored {pkg}")
    if backup.get("_locators"):
        with open(os.path.join(pk.dir, "PackFileLocators.bin"), "r+b") as f:
            for r in backup["_locators"]:
                f.seek(r["at"]); f.write(bytes.fromhex(r["bytes"]))
        print("  restored PackFileLocators.bin entries")
    os.replace(bpath, bpath + ".used")          # replace an older .used file if one is already there (os.rename fails on Windows)
    print("Done.")


def get_package_dir(argv):
    if "--package" in argv:
        i = argv.index("--package")
        d = argv[i + 1]
        del argv[i:i + 2]
        return d
    ini = os.path.join(HERE, "horizon.ini")
    if os.path.exists(ini):
        line = open(ini).readline().strip()
        if line:
            return line
    return DEFAULT_PACKAGE_DIR


def main(codec_factory=LZ4):
    argv = sys.argv[1:]
    package_dir = get_package_dir(argv)
    if not argv or argv[0] not in ("find", "patch", "restore") or len(argv) < 2:
        print(__doc__)
        return
    if not os.path.exists(os.path.join(package_dir, "PackFileLocators.bin")):
        raise SystemExit(f"Can't find PackFileLocators.bin in {package_dir}. Use --package <folder>.")
    pk = Packages(package_dir, codec_factory(package_dir))
    {"find": cmd_find, "patch": cmd_patch, "restore": cmd_restore}[argv[0]](argv[1:], pk)


if __name__ == "__main__":
    try:
        main()
    except PermissionError:
        raise SystemExit("Permission denied writing game files. Run the command prompt as Administrator, "
                         "and make sure the game is closed.")
