# Unisoc / Spreadtrum PAC format

A `.pac` is one file holding a whole firmware package: a header, a table of file
entries, and the payloads those entries point at. Research Download writes the
payloads, in entry order, to the addresses the entries name. **Nothing is
compressed** — the payloads are raw images, which is why a PAC is roughly as large
as the flash it writes.

## Provenance

This format was the reason the SPD phase was originally blocked. It is now
verified from four sources, all of which agree byte for byte:

| Source | Licence | What it gave |
| --- | --- | --- |
| [Mani-Sadhasivam/unisoc-dloader](https://github.com/Mani-Sadhasivam/unisoc-dloader) `include/BinPack.h` | — | **The vendor's own header.** `PAC_MAGIC`, `BIN_PACKET_HEADER_T`, `FILE_T`, and the documented `V1->V2` field changes. The authority the rest were checked against. |
| [ajsb85/sprdflash-rs](https://github.com/ajsb85/sprdflash-rs) `sprdflash-core/src/pac.rs` | MIT | Every field offset as a named constant, both CRC checks, the entry range validation. |
| [iscle/unpac](https://github.com/iscle/unpac) `main.c` | GPL-3.0 | The same two structures in C, and the UTF-16 field decoding. |
| [affggh/unpac_py](https://github.com/affggh/unpac_py) `unpac.py` | — | The same structures again in Python, plus the magic check. |

The vendor header is what settles the version question: it annotates the fields
`V1->V2` and says exactly which ones changed.

## Layout

```
offset 0                 header            2124 bytes
offset 2124              file entry 0      2580 bytes
                         file entry 1      2580 bytes
                         ...
                         payloads
```

Both the header and every entry are **fixed size**, and the entry table starts at
`dwFileOffset` — which is 2124 in every package seen, so the header is also the
table's start.

## Header

Offsets are identical in V1 and V2 except where noted. That is not a coincidence
and not an approximation: V2 took two words out of the version string and spent
them on a high half for the size, so everything after the first 52 bytes is in the
same place in both.

| Offset | Field | Notes |
| --- | --- | --- |
| 0 | `szVersion` | UTF-16LE. **48 bytes in V1, 44 in V2.** |
| 44 | `dwHiSize` | **V2 only.** High half of the file size. |
| 48 | `dwSize` / `dwLoSize` | The whole file's size. V1's only size field; V2's low half. |
| 52 | `szPrdName` | UTF-16LE, 512 bytes. The product this package is for. |
| 564 | `szPrdVersion` | UTF-16LE, 512 bytes. |
| 1076 | `nFileCount` | How many entries follow. |
| 1080 | `dwFileOffset` | Where the entry table starts. |
| 1084 | `dwMode` | |
| 1088 | `dwFlashType` | |
| 1092 | `dwNandStrategy` | |
| 1096 | `dwIsNvBackup` | |
| 1100 | `dwNandPageType` | |
| 1104 | `szPrdAlias` | UTF-16LE, 200 bytes. |
| 1304 | `dwOmaDmProductFlag` | |
| 1308 | `dwIsOmaDM` | |
| 1312 | `dwIsPreload` | |
| 1316 | `dwReserved[200]` | 800 bytes. |
| 2116 | `dwMagic` | `0xFFFAFFFA` |
| 2120 | `wCRC1` | Header CRC. |
| 2122 | `wCRC2` | Payload CRC. |

**The magic is `0xFFFAFFFA`**, which is the complement of `0x00050005` — that is
where the community's name "PAC 5" comes from. The vendor header defines it once,
as `PAC_MAGIC`, and does not vary it by version.

## File entries

| Offset | Field | Notes |
| --- | --- | --- |
| 0 | `dwSize` | The size of this entry, 2580. Checked, because it is what makes the offsets meaningful. |
| 4 | `szFileID` | UTF-16LE, 512 bytes. `HOST_FDL`, `FDL2`, `AP`, `NV`, `FMT_FSSYS`, ... |
| 516 | `szFileName` | UTF-16LE, 512 bytes. Often empty for a marker. |
| 1028 | `szFileVersion` | UTF-16LE. **512 bytes in V1, 504 in V2.** |
| 1532 | `dwHiFileSize` | **V2 only.** |
| 1536 | `dwHiDataOffset` | **V2 only.** |
| 1540 | `nFileSize` / `dwLoFileSize` | Payload size. Zero means there is no payload. |
| 1544 | `nFileFlag` | 1 = needs a file, 0 = an operation or a list of operations. |
| 1548 | `nCheckFlag` | 1 = must be downloaded, 0 = must not. |
| 1552 | `dwDataOffset` / `dwLoDataOffset` | Where the payload is. |
| 1556 | `dwCanOmitFlag` | 1 = "download all" may skip it. |
| 1560 | `dwAddrNum` | How many of the five address slots are used. |
| 1564 | `dwAddr[5]` | Load or flash addresses, 32-bit, 20 bytes total. |
| 1584 | `dwReserved[249]` | 996 bytes. |

Note that the fields a host actually needs — size, offset, flag, address — sit at
**the same offsets in both versions**, because the words V2 stole came from the
version string and the reserved tail. V1's `nFileSize` is V2's `dwLoFileSize`;
when the high half is zero the two files are indistinguishable for every field
this tool reads, which is why a sub-4-GiB V2 package is read as V1 without any
loss.

## What the fields mean

`nFileFlag` and `nCheckFlag` are documented by the vendor header in its own words:

> `nFileFlag`: if "0", means that it need not a file, and it is only an operation
> or a list of operations, such as file ID is "FLASH". if "1", means that it need
> a file.
>
> `nCheckFlag`: if "1", this file must be downloaded; if "0", this file can not be
> downloaded.

So an entry with `nFileFlag == 0` is an **operation**, not an image: a format, an
erase, or a phase marker. It has no payload and there is nothing to write.

### Loader identification

| Id | Role |
| --- | --- |
| `HOST_FDL`, `FDL`, `FDL1`, `FDL1*` | **FDL1** — the first-stage loader |
| `FDL2`, `FDL2*` | **FDL2** — the second-stage loader |
| `AP`, `NV`, `PREPACK`, `FMT_*`, ... | images |
| anything with no payload | marker |

**The order matters:** a marker wins first, then FDL2, then FDL1. Every FDL2 id
begins with `FDL`, so a classifier that tested the FDL1 prefix first would swallow
its own second-stage loader — and a package flashed without FDL2 stops at the
point where the storage commands should have started.

## Checksums

Two CRC-16/ARC values, both polynomial `0xA001` reflected, init 0:

- **`wCRC1`** covers the header from offset 0 up to offset 2120 — everything
  including the magic.
- **`wCRC2`** covers everything after the fixed header, i.e. the entry table and
  all the payloads.

Check value: `crc16_arc("123456789") == 0xBB3D`.

A payload CRC mismatch is **reported, not fatal**: the bytes are all there and the
operator is the one who decides whether to trust them. A header CRC mismatch, a
wrong magic, a size that disagrees with the file, or an entry that points outside
it are all fatal — those mean the package is not what it claims to be.

## Text fields

UTF-16LE, NUL-terminated, **one character per 16-bit unit**. A unit above 0x7F is
replaced with `?` rather than encoded, because a package that used real UTF-16
would not round-trip through this and silently substituting something else would
be worse than showing that it was not ASCII.

## What is not verified

- **`dwFlashType`, `dwMode`, `dwNandStrategy`, `dwNandPageType` and the OMA-DM
  flags are read but not interpreted.** The vendor header names the fields and
  says nothing about their value sets. They are reported raw.
- **The logical address boundary.** The reference implementation treats an
  address at or above `0x80000000` as an operation marker rather than a flash
  address. The vendor header does not name that boundary, so this tool exposes it
  as a hint (`PacEntry::is_logical_marker`) and **nothing acts on it**.
- **No PAC has been flashed against real hardware in this build.** The parser is
  tested against packages built byte for byte to the layouts above; that proves
  the offsets, not that a particular device accepts the result.
