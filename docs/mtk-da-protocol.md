# MediaTek download agent protocol (XFlash)

Companion to `src/cpp/protocols/mediatek/da.{h,cpp}`. The bootrom protocol is a
separate document — see [mediatek-notes.md](mediatek-notes.md).

## Provenance

Transcribed from [mtkclient](https://github.com/bkerler/mtkclient) (GPL-3.0) —
`Library/DA/xflash/xflash_lib.py`, `xflash_param.py`, `xflash_flash_param.py`,
`Library/DA/storage.py` and `Library/error.py`.

No mtkclient code was copied. What is reused is the protocol itself: command ids,
framing, parameter layouts, the checksum, and the status code table. Those are
interoperability requirements.

**mtkclient is GPL-3.0**, unlike the BSD-3-Clause sources used elsewhere in this
project. If this tool is to be distributed, that licensing deserves a real review
rather than an assumption.

## Which generation this is

mtkclient implements three DA protocol generations:

| Generation | Framing | Used by |
| --- | --- | --- |
| **xflash** | 12-byte little-endian header with a magic | **This implementation.** Current devices. |
| legacy | single command bytes, no length framing | Older chipsets |
| xmlflash | XML documents | Newest DAs |

The task's command names — `INIT_EXT_RAM`, `WRITE_DATA`, `READ_DATA`, `FORMAT` —
are xflash's own names, which is why xflash is the one implemented. A device
whose agent speaks only another dialect will answer `GET_DA_VERSION` with
nothing, and the log says so.

## Framing

Every message in **both** directions is a 12-byte header followed by `length`
bytes:

```
magic      0xFEEEEEEF   little-endian
data type  1 = protocol flow, 2 = message
length                 little-endian
```

This is the single most expensive thing to get wrong here: the bootrom layer a
second earlier is **big-endian** with an echo and no magic. A frame built with
the bootrom's rules is 28 bytes of nonsense.

A **status** is not a separate message type. It is a response frame whose payload
is 2 or 4 bytes:

- 2 bytes → a 16-bit status
- 4 bytes → a 32-bit status, where the protocol magic itself means success
- longer → read as 32-bit words, the first is the status

`0` means success. Everything else is a code from mtkclient's `ErrorCodes_XFlash`
table, and the codes this tool can act on are named in `describe_da_status()`.
An unknown code is reported as a number: guessing at one is worse than showing it.

## Two checksums, deliberately

| Where | Algorithm | Function |
| --- | --- | --- |
| Bootrom agent upload | XOR of 16-bit little-endian words | `compute_checksum()` in `brom.h` |
| Agent data transfers | Low 16 bits of the **sum** of the bytes | `compute_da_checksum()` in `da.h` |

Both belong to the same device. A build that used one where the other belongs is
rejected by the device with an unhelpful generic error, so the native tests
assert that the two produce different results on the same input.

## Commands

### Region parameter block

`READ_DATA`, `WRITE_DATA` and `FORMAT` all carry the same 56-byte parameter:

```
<IIQQ   storage, partition, address, length
<IIIIIIII  eight NAND extension words
```

The eight extension words are **zero for eMMC and UFS** — that is what
mtkclient's `NandExtension` defaults to. They exist for raw NAND, where they
select cell usage, address type and format level. `encode_region_param()` writes
them, `decode_region_param()` reads them back, and both are tested byte by byte.

### `WRITE_DATA` (0x010004)

```
host → WRITE_DATA                      device → status
host → 56-byte parameter               device → status
per chunk:
  host → 4-byte zero                   
  host → 4-byte checksum               (sum & 0xFFFF, little-endian)
  host → the bytes                     device → status, once per chunk
host → nothing                         device → status, the verdict on the whole write
host → CC_OPTIONAL_DOWNLOAD_ACT        device → status
```

Payloads are padded with zeros to a 512-byte boundary, because the protocol
writes whole sectors and a short write would leave the tail of the previous
contents in place.

The final status is separate from the per-chunk acknowledgements and is the one
that means "the flash actually happened". The post-download action's answer is
reported and **not** raised on: the bytes are already acknowledged by then.

### `READ_DATA` (0x010005)

```
host → READ_DATA                       device → status
host → 56-byte parameter               device → status
host → nothing                         device → status
loop:
  device → a frame
    payload > 4 bytes  → it is data; host acknowledges with a 4-byte zero
    payload == 4 bytes → end-of-data flag; zero means done, non-zero is a failure
    payload < 4 bytes  → neither, and the transfer is refused
host → nothing                         device → status
```

Reads are held in memory and capped at 512 MiB for that reason.

### `FORMAT` (0x010003)

```
host → FORMAT                          device → status
host → 56-byte parameter               device → status
host → nothing                         device → status   (Continue or Complete)
while the status is Continue (0x40040004):
    device → a delay in milliseconds
    host  → wait that long, then send a 4-byte zero
    device → the next status
Complete (0x40040005) means done.
```

The agent's delay is clamped to 5 seconds per round. A device that asked for an
hour would otherwise park the worker thread; the clamp is logged when it bites.

### Queries

A query is `DEVICE_CTRL` followed by a control code, and the answer is a data
frame followed by a status frame:

```
host → DEVICE_CTRL (0x010009)          device → status
host → the control code                device → status
                                       device → the data frame
                                       device → status
```

Control codes used here: `GET_EMMC_INFO`, `GET_NAND_INFO`, `GET_NOR_INFO`,
`GET_DA_VERSION`, `GET_PACKET_LENGTH`, `GET_RAM_INFO`, `GET_CONNECTION_AGENT`,
`GET_CHIP_ID`, `SLA_ENABLED_STATUS`, and the `SET_*` family for checksum level
and reset key.

`GET_CONNECTION_AGENT` answers `brom` or `preloader` — which stage the agent took
over from, which is how this tool knows both entry paths worked.

## Structures

`EmmcInfo` is 96 bytes, read positionally:

```
<II      type, block size
<QQQQQQQQ  boot1, boot2, rpmb, gp1..gp4, user
16 bytes   CID
<Q       firmware version
```

`RamInfo` is 24 bytes of 32-bit fields or 48 of 64-bit ones — the agent picks,
and a reply of neither width is refused rather than half-read.

**`NandInfo` and `NorInfo` are marked `TODO` in the source.** mtkclient parses
those two positionally without naming the layout, so the offsets this build uses
are inferred from the field widths it reads. They are flagged in the code and
should be checked against a recorded reply from a NAND or NOR device before being
trusted.

## Secure boot and error recovery

- **SLA required.** The bootrom says so with status `0x1D0D` and the agent with
  `SLA_ENABLED_STATUS`. Both are reported. Neither is worked around: this build
  does not implement the signed handshake and does not pretend to.
- **`DL forbidden` (`0xc0020004`)** — the device's security policy refuses the
  write. Reported with that text.
- **Anti-rollback (`0xc002002d`)** — the image is older than the device allows.
- **A refusal stops that operation immediately**, and no further data is sent: the
  native tests assert that only the first command frame went out.
- **A failed write leaves the partition in an unknown state.** The message says
  so, and says to rewrite it rather than resume.
- **A whole scatter run stops at the first failure by default**, reporting which
  partitions were written and which were not. `continue_on_error` overrides it.

## Verified how

`tests/cpp/test_mediatek.cpp` drives the protocol against a scripted agent and
asserts the exact bytes: the little-endian header byte by byte, the command ids,
the 56-byte parameter block, the per-chunk checksum, the chunk count, the read
acknowledgements, the erase flow-control loop, and the refusal paths.

That proves the host emits what an agent expects. It does **not** prove a real
agent completes the exchange: no MediaTek hardware was available.
