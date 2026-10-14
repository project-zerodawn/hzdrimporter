"""
hzdr_grow.py - let a replacement stream entry be LARGER than the original.

A DSAR package is a table of chunks (raw offset, compressed offset, raw size, compressed size, type)
followed by the LZ4-compressed chunk data. PackFileLocators.bin gives each entry (key) its raw
offset and size inside one package. A larger entry can't go back into its old slot, so:

  1. new chunks are added to the END of the package's raw address space, holding the new data;
  2. the chunk table grows by those entries (chunks whose compressed bytes sat where the table
     grows into are first copied to the end of the file and repointed);
  3. the entry's locator record is pointed at the new raw offset and size.

The original chunks and the original entry bytes are left untouched, so 'restore' only has to put
back the header, the table, the few bytes the table grew over, and the locator record.
"""
import struct

import hzdr_texpatch as T

HEAD_FMT = "<4sHHIIQ"   # magic, major, minor, chunk count, field (table end / data start), raw total


def append_entry(arc, data, backup):
    """Append `data` as new chunks at the end of archive `arc`'s raw space. Returns the raw offset.
    `backup` (dict) receives what restore needs."""
    path = arc.path
    with open(path, "r+b") as f:
        head = bytearray(f.read(32))
        magic, major, minor, count, field5, total = struct.unpack_from(HEAD_FMT, head, 0)
        if magic != b"DSAR":
            raise SystemExit(f"{path}: not a DSAR archive")
        table = bytearray(f.read(32 * count))
        chunks = [list(struct.unpack_from(T.CHUNK_FMT, table, 32 * i)) for i in range(count)]
        raw_end = max(c[0] + c[2] for c in chunks)
        if raw_end != total:
            print(f"  note: raw total {total} differs from last chunk end {raw_end}; using the larger")
        raw_off = max(raw_end, total)
        raw_off = (raw_off + 15) // 16 * 16
        if raw_off + len(data) >= 1 << 32:
            raise SystemExit("Package raw space would pass 4 GB; can't relocate this entry here.")
        chunk_raw = max(c[2] for c in chunks)                      # same chunk size the archive uses
        pieces = [data[i:i + chunk_raw] for i in range(0, len(data), chunk_raw)]
        k = len(pieces)
        old_table_end, new_table_end = 32 + 32 * count, 32 + 32 * (count + k)

        f.seek(0, 2)
        end = f.tell()
        backup.update({"path": path, "head": bytes(head).hex(), "table": bytes(table).hex(),
                       "file_size": end, "grow_from": old_table_end, "grow_to": new_table_end})
        f.seek(old_table_end)
        backup["grown_over"] = f.read(new_table_end - old_table_end).hex()

        # 1. move every chunk whose compressed bytes lie where the table will grow
        def alloc(n):
            nonlocal end
            off = (end + 4095) // 4096 * 4096
            end = off + n
            return off
        moved = []
        for i, c in enumerate(chunks):
            if c[1] < new_table_end and c[1] + c[3] > old_table_end:
                before = arc.chunk_raw(i)                      # decompressed content, checked after the move
                f.seek(c[1]); comp = f.read(c[3])
                c[1] = alloc(len(comp)); f.seek(c[1]); f.write(comp); moved.append((i, before))
        # 2. write the new data chunks
        new = []
        pos = raw_off
        for p in pieces:
            comp = arc.codec.compress(p)
            off = alloc(len(comp)); f.seek(off); f.write(comp)
            new.append([pos, off, len(p), len(comp), 3, b"\x00" * 7])
            pos += len(p)
        # 3. new table + header
        allc = chunks + new
        f.seek(32)
        for c in allc:
            f.write(struct.pack(T.CHUNK_FMT, *c))
        new_total = pos
        new_field5 = field5
        if field5 == old_table_end:
            new_field5 = new_table_end
        elif field5 == 32 * count:
            new_field5 = 32 * (count + k)
        else:
            print(f"  note: header field {field5} left as is")
        struct.pack_into(HEAD_FMT, head, 0, magic, major, minor, count + k, new_field5, max(total, new_total))
        f.seek(0); f.write(head)
    print(f"  appended {len(data)} bytes as {k} new chunks at raw offset {raw_off} ({len(moved)} chunks moved)")
    arc.cache.clear(); arc.load_table()
    if arc.read(raw_off, len(data)) != data:
        raise SystemExit("Read-back of the appended entry failed. Run 'restore'.")
    # every chunk that had to move must still hold exactly what it held before
    for i, before in moved:
        if arc.chunk_raw(i) != before:
            raise SystemExit(f"Read-back of moved chunk {i} failed. Run 'restore'.")
    arc.cache.clear()
    return raw_off


def undo(backup):
    with open(backup["path"], "r+b") as f:
        f.seek(0); f.write(bytes.fromhex(backup["head"])); f.write(bytes.fromhex(backup["table"]))
        f.seek(backup["grow_from"]); f.write(bytes.fromhex(backup["grown_over"]))
        # appended data after the original end is left in place (harmless; never cut the file short)
