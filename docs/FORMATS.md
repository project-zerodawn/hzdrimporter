# Horizon Zero Dawn Remastered file notes

This is what these tools rely on, worked out from the game's files. All values are little-endian.

## Package files (`package.XX.YY.core`, `package.XX.YY.core.stream`)

DSAR archives: one continuous "raw" address space, stored as LZ4-compressed chunks.

- **Header, 32 bytes:** `<4sHHIIQ` = magic `DSAR`, major, minor, chunk count, data start (end of the
  chunk table), raw total size, then 8 more bytes.
- **Chunk table:** one 32-byte row per chunk, `<QQIIB7s` = raw offset, compressed offset, raw size,
  compressed size, type (3 = LZ4 block, 0 = stored), padding.
- To read raw bytes, find the chunks that cover the range, decompress them and slice.
- To write, recompress the touched chunks. If a chunk no longer fits its old slot, append it at the
  end of the file and repoint its table row. To grow an entry, see `hzdr_grow.py`.

## `PackFileLocators.bin`

The game's only index of which package holds each file.

```
u32 package count
repeat:
  u32 name length, name (ASCII, e.g. "package.00.11.core")
  u32 record count
  records: <QII = 64-bit key, raw offset, size
```

- `.core` files and their `.core.stream` data are separate entries with their own keys.
- A file added only as a new package (with extra records added after the originals) was **not**
  picked up in testing. The game appears to use the first record it finds for a key.

## `.core` files

A `.core` file is a list of objects. Each object is a 64-bit type hash, a u32 data size, then that
many bytes of data. Type hashes used here:

| Hash | Object |
|---|---|
| `0x03773cc419d4989b` | mesh |
| `0x7811dcc411ba1288` | primitive |
| `0xc7f1a4abbe992e6c` | vertex buffer description |
| `0x0a2c463f56053f70` | index buffer description |
| `0x38f49cecd473a967` | draw part |

The `.core` holds descriptions, and the bulk data lives in the `.core.stream` entry. Each
description records sizes, so an edited `.core` must keep its size fields in step with the stream
entry.

## Geometry entries (`.core.stream`)

Each level of detail (La, Lb, Lc) is its own entry. It contains the primitives in order, and for
each primitive:

- its vertex streams, each aligned to its own stride;
- its index buffer, aligned to the index size.

Index buffer: the flag at `IB+24` is 1 for 32-bit indices, otherwise 16-bit. Stay at 16-bit (at
most 65,535 points per primitive). 32-bit indices load, but were linked to rendering problems in
testing.

Vertex streams seen on characters:

| Stream | Size | Contents |
|---|---|---|
| 0, skinned | 24 bytes | position (3×f16), `0x3c00`, 8 bone indices (u8, global), 8 weights (u8) |
| 0, rigid (props, headwear) | 12 bytes | position (3×f16), `0x3c00`, bone index (u8), padding |
| 1 | 16 bytes | normal and tangent (i16 ×8). Tangent sign is stored as 0 or 32767. |
| 2 | 12 bytes | colour (4×u8), UV0 (2×f16), UV1 (2×f16). On 4-byte UV streams, UV0 is at 0. |

The `.ascii` model format read by `hzdr_meshswap.py` uses the reverse of the stored triangle order.

## Textures

- A texture set's `.core` holds each texture's header and its smallest mips.
- The header records total size, embedded size, streamed size and the number of streamed mips.
- The streamed mips of **every texture in the set** share one `.core.stream` entry. They are stored
  as every mip level of every texture, sorted by size, largest first. Levels of equal size keep the
  textures' `.core` order (`stream_layout` in `hzdr_texpatch.py`).
- 4096×4096 textures did not work in testing (the game kept treating them as 2048). Keep the
  original size.
