# HZDR modding tools

Command-line Python tools for replacing textures and meshes in **Horizon Zero Dawn Remastered**
(PC, Nixxes port). They read and write the game's own package files directly, check that every
original is exactly what they expect before changing anything, and back up everything they touch
so each change can be undone.

These tools contain no game data. You extract the originals you want to edit yourself, using
an extraction tool that supports the Remastered.

## How to read the examples

Anything written `<LIKE_THIS>` is a placeholder. Replace the whole thing, angle brackets
included, with your own value. Everything else in a command is typed exactly as shown.

| Placeholder | What to put there | Made-up example |
|---|---|---|
| `<PACKAGE_FOLDER>` | The game's package folder, the one that contains `PackFileLocators.bin`. | `D:\Games\Horizon Zero Dawn Remastered\LocalCacheDX12\package` |
| `<SET>` | A texture set's `.core` file name, without `.core`. | `characterhead_set` |
| `<SET>NN_original.dds` | An original texture from that set, exported unedited. `NN` is its two-digit number in the set (00, 01, 02 ...). | `characterhead_set00_original.dds` |
| `<SET>NN_edited.dds` | Your edited version of that texture. | `characterhead_set01_edited.dds` |
| `<MESH>` | A mesh's `.core` file name, without `.core`. | `characterhelmet_lx` |
| `<NEW_MODEL>` | Your replacement model in `.ascii` format, without `.ascii`. | `myhelmet` |
| `<MOD_FOLDER>` | A folder holding a `manifest.json` and the files it lists. | `mymod` |

## Requirements

- Windows with Python 3.10 or newer (`py` launcher). The tools themselves are plain Python.
- Optional but strongly recommended for speed: `py -m pip install lz4`
- Close the game before running anything. Run the command prompt as administrator if the game is
  installed under `Program Files`.
- Put all the `.py` files in one folder, and run the commands from that folder.

## Pointing the tools at your game

By default the tools look in
`C:\Program Files (x86)\Steam\steamapps\common\Horizon Zero Dawn Remastered\LocalCacheDX12\package`.
If your game is somewhere else, do either of these:

- add `--package "<PACKAGE_FOLDER>"` to any command, or
- create a text file called `horizon.ini` next to the scripts, with `<PACKAGE_FOLDER>` on its first line.

## The tools

| File | What it does |
|---|---|
| `hzdr_texpatch.py` | Find a texture set in the packages, write edited `.dds` files into it, undo. |
| `hzdr_meshswap.py` | Replace a rigid (single-bone) mesh such as headwear with an `.ascii` model, undo. |
| `hzdr_apply.py` | Apply a prepared mod from a `manifest.json` (replacement geometry entries and patched `.core` files), undo. |
| `hzdr_grow.py` | Used by the others; you never run it directly. Lets a replacement entry be larger than the original. |

Every tool prints its full help when run with no arguments.

### Textures: `hzdr_texpatch.py`

```
py hzdr_texpatch.py find    <SET>.core <SET>00_original.dds <SET>01_original.dds ...
py hzdr_texpatch.py patch   <SET>.map.json <SET>01_edited.dds ...
py hzdr_texpatch.py restore <SET>.map.json
```

1. **find**: give it the texture set's `.core`, then every original `.dds` exported from that set.
   It locates them in the packages and writes `<SET>.map.json` next to the `.core`. That file
   records where everything lives, and the next two steps use it. You only need to run find once.
2. **patch**: give it `<SET>.map.json`, then one or more edited `.dds` files. Each edited file must:
   - have a name that starts with the texture's name (`<SET>01...` for texture 01);
   - have the same width, height and compression format as the original;
   - include the full mip chain (export with "generate mip maps" on).

   Before writing, it saves a backup as `<SET>.map.backup.json`.
3. **restore**: give it `<SET>.map.json`. It puts back everything the backup recorded.

### Rigid meshes: `hzdr_meshswap.py`

```
py hzdr_meshswap.py find    <MESH>.core <MESH>_lod_setup.core
py hzdr_meshswap.py swap    <MESH>.mesh.json <NEW_MODEL>.ascii
py hzdr_meshswap.py restore <MESH>.mesh.json
```

1. **find**: give it the mesh's `.core` and, optionally, its LOD setup `.core`. It writes
   `<MESH>.mesh.json`. If the LOD setup was given, the full-quality model is then used at every distance.
2. **swap**: give it `<MESH>.mesh.json` and your `.ascii` model. Add `--keep N` to keep original part
   number `N` (counting from 0) instead of replacing it, for example hair that sits under headwear.
   You can repeat `--keep`. The `.ascii` must contain one part for each part that isn't kept.
3. **restore**: give it `<MESH>.mesh.json` to undo the swap.

### Prepared mods: `hzdr_apply.py`

```
py hzdr_apply.py apply   <MOD_FOLDER>\manifest.json
py hzdr_apply.py restore <MOD_FOLDER>\manifest.json
```

A manifest describes a finished mod: replacement geometry entries and patched `.core` files.
Nothing is written unless every original is found as expected. Before writing, a backup is saved
as `manifest.backup.json` next to the manifest, and `restore` uses it.

Start from `examples/manifest.template.json`. Replace every `<PLACEHOLDER>` and every `0` with your
own values. The two kinds of entry are explained below.

#### `entries`: replacement `.core.stream` entries (geometry data)

| Field | What it is |
|---|---|
| `name` | Any label you like. It's only used in messages. |
| `file` | Your new entry data, relative to the manifest. |
| `orig_size` | Size in bytes of the original entry. |
| `allow_grow` | Optional. `true` lets your file be larger than the original (see below). |

Then identify the original entry in **one** of two ways:

| Option | Fields | Use when |
|---|---|---|
| By key | `key`: the entry's 64-bit key from `PackFileLocators.bin`, in hex (16 characters).<br>`orig_sha1`: SHA-1 checksum of the original entry. | You know the key. |
| By search | `find.nv`: the original's vertex count.<br>`find.stride`: bytes per vertex in its first stream (24 for skinned meshes, 12 for rigid ones).<br>`find.bbox`: optional, the mesh's bounding box as `[min x, min y, min z, max x, max y, max z]`. | You don't know the key, e.g. for lower LODs. If several identical copies match, all of them are replaced. |

With `allow_grow`, a larger entry is added at the end of its package and its locator record is
pointed at the new data. The original bytes stay where they were, so `restore` can undo it.

#### `cores`: patched `.core` files

| Field | What it is |
|---|---|
| `name` | The original `.core` file name. It's only used in messages. |
| `size` | Size in bytes of the original `.core`. Your patched file must be **exactly** this size. |
| `orig_sha1` | SHA-1 checksum of the original `.core`. The tool finds the original by its size and this checksum. |
| `file` | Your patched `.core`, relative to the manifest. |

## Undoing everything

Every tool has `restore`. If backups are lost, use Steam's **Verify integrity of game files**,
which puts all package files back to the originals. Then delete any `*.backup.json` files.

## How the game files work

See [`docs/FORMATS.md`](docs/FORMATS.md) for the package, locator, texture and mesh layouts these
tools rely on.
