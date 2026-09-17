# Unisoc / Spreadtrum Research Download protocol (BSL)

Companion to `src/cpp/protocols/spd/bsl.{h,cpp}`. The container format is a
separate document — see [spd-pac-format.md](spd-pac-format.md).

## Provenance

| Source | Licence | What it gave |
| --- | --- | --- |
| [Mani-Sadhasivam/uwpflash](https://github.com/Mani-Sadhasivam/uwpflash) `command.h` | Apache-2.0 | The complete BSL command and response enum, both directions, with the meaning of each value written next to it. |
| [iscle/sprdclient](https://github.com/iscle/sprdclient) `main.c` | GPL-3.0 | The HDLC framing, the USB hello, the payload-download sequence, and both checksums as a working client uses them. |
| [ajsb85/sprdflash-rs](https://github.com/ajsb85/sprdflash-rs) `bsl.rs`, `checksum.rs` | MIT | The same framing, and the chip-family checksum split named explicitly. |

No code was copied. What is reused is the protocol: command codes, framing,
escaping, and the checksums. Those are interoperability requirements.

## Framing

HDLC, on a bulk endpoint:

```
0x7E  <escaped body>  0x7E
```

The body is:

```
type    u16, big-endian
size    u16, big-endian
data    size bytes
check   u16, big-endian
```

**Escaping** replaces `0x7E` and `0x7D` with `0x7D` followed by the byte xor
`0x20`. Escaping applies to the body only — never to the flags that delimit it. A
frame whose body legitimately contains a flag is therefore longer than its size
field suggests, which is exactly why the escaping exists.

The chunk size for bulk data is **528 bytes**. That is not arbitrary: the body is
`4 + 528 + 2 = 534` bytes, and in the worst case every one of those bytes needs
escaping, giving 1068, plus two flags. That fits a 4096-byte buffer with room to
spare. Both reference clients use exactly this number.

## The handshake

Three steps, and all three are required:

1. **A class control transfer**, with no data stage:
   `bmRequestType 0x21`, `bRequest 0`, `wValue 1`, `wIndex 0`, length 0.
   Without it the device never accepts a bulk frame at all. Nothing errors — the
   link is simply dead, which is the worst possible failure mode and the reason
   this is called out.
2. **A lone `0x7E` byte** — a flag with no frame around it. The device answers
   with `BSL_REP_VER` (0x81) and its version string.
3. **`BSL_CMD_CONNECT`** (0x00), answered with `BSL_REP_ACK` (0x80).

After step 3 the link is up.

## Commands

Host to device, from uwpflash's own enum:

| Code | Name | Purpose |
| --- | --- | --- |
| 0x00 | `CONNECT` | Handshake |
| 0x01 | `START_DATA` | Begin a payload transfer |
| 0x02 | `MIDST_DATA` | A payload chunk |
| 0x03 | `END_DATA` | End the transfer |
| 0x04 | `EXEC_DATA` | Run what was sent |
| 0x05 | `NORMAL_RESET` | Leave download mode |
| 0x06 | `READ_FLASH` | Read flash contents |
| 0x07 | `READ_CHIP_TYPE` | Chip identity |
| 0x0A | `ERASE_FLASH` | Erase a region |
| 0x0C | `READ_FLASH_TYPE` | Flash type |
| 0x0D | `READ_FLASH_INFO` | Flash geometry |
| 0x0F | `READ_SECTOR_SIZE` | Sector size |
| 0x10/0x11/0x12 | `READ_START` / `READ_MIDST` / `READ_END` | A paged read |
| 0x17 | `POWER_OFF` | |
| 0x1A | `READ_CHIP_UID` | |
| 0x7F | `END_PROCESS` | |

Device to host: `0x80` `ACK`, `0x81` `VER`, then the failure codes `0x82`–`0x92`,
the result codes `0x93`–`0x9E`, and from `0xA0` a set of specific errors — the two
worth naming are `0xA3 CHIP_ID_NOT_MATCH` (the package is for a different device)
and `0xB3 FLASH_WRITTEN_PROTECTION`. `0xFF` is the agent's own log output, which
is text rather than a verdict and must not be mistaken for one; this
implementation forwards it to the log and keeps reading.

An unknown code is reported as a number. Guessing at one is worse than showing it.

## Checksums: two algorithms, and the host does not choose

**This is the trap in this protocol.** Classic Spreadtrum boot ROMs (SC65xx,
SC77xx, SC98xx) check their frames one way; the RDA8910/UIS8910 family checks them
another. They are not interchangeable, and a frame with the wrong one gets **no
reply at all** — which looks exactly like a dead device.

| Algorithm | Polynomial / rule | Used by |
| --- | --- | --- |
| CRC-16, poly 0x1021, MSB-first, **init 0** | | classic BootROM |
| Spreadtrum ones-complement sum | 16-bit big-endian words, carry folded twice, complemented | RDA8910 / UIS8910 |

Two details that cost time if missed:

- **The CRC's initial value is 0, not 0xFFFF.** That makes it the variant usually
  catalogued as *CRC-16/XMODEM* (check value `0x31C3` for `"123456789"`), not
  *CRC-16/CCITT-FALSE* (which initialises to 0xFFFF and gives `0x29B1`). The two
  names are commonly conflated and the difference is invisible until a device
  rejects every frame.
- **The Spreadtrum sum has two spellings that are the same function.** One
  reference sums little-endian words and swaps the result; another sums big-endian
  words and does not. Swapping *and* summing big-endian words — the obvious way to
  misread the pair — double-swaps and gives a different answer on every input.

So the host **detects** which one is in use, from the device's first reply: a frame
that verifies with `sprd_sum` is an RDA-family device, one that verifies with
CRC-16 is a classic one. That reply is also the first thing to arrive, so the
detection is free.

## Sending a loader

FDL1 and FDL2 are sent with the same four commands:

```
START_DATA   address (u32 BE), length (u32 BE)     → ACK
MIDST_DATA   up to 528 bytes per frame, repeatedly  → ACK per frame
END_DATA     nothing                                → ACK
EXEC_DATA    nothing                                → ACK
```

`EXEC_DATA` runs the payload at **its load address plus 0x200**, which is where
the loader's entry point sits after its header.

The two loaders are the same operation at different addresses:

- **FDL1** is sent to the boot ROM. It brings up USB and DRAM.
- **FDL2** is sent through the running FDL1, which speaks the same four commands.
  It is FDL2 that has the storage commands, so a package flashed without it stops
  as soon as the flash needs touching.

## Reading and erasing

`ERASE_FLASH` takes an address and a length, both big-endian.

A read is paged: `READ_START`, then `READ_MIDST` until the device answers with
either `READ_FLASH_RESULT` (more data) or a bare `ACK` (there is no more), then
`READ_END`. Both of those answers are normal and neither is an error.

## What is not verified

- **The layout of `READ_FLASH_INFO`'s reply.** The command and its response code
  are confirmed by the command enum, but the sources do not lay out the fields
  inside it. This implementation returns the bytes raw and says so, rather than
  slicing them into named fields that might be wrong.
- **The baud-rate renegotiation.** `CHANGE_BAUD` exists in the command set and SPD
  devices are known to renegotiate the link speed mid-session, but no source
  showed the sequence, so this build does not attempt it. The link runs at
  whatever speed it came up at.
- **`BSL_CMD_REPARTITION` and the NV-item commands** are in the enum but are not
  driven by this build: neither is needed to flash a package, and the NV write
  path has its own quirks (see the NV CRC below).
- **NV images.** The reference implementation refreshes a leading CRC-16/ARC in an
  NV image before writing it, because FDL2 validates it at `END_DATA`. That is
  implemented here only as far as exposing `crc16_arc`; the write path does not
  yet apply it.
- **The PDL pre-stage.** The RDA8910/UIS8910 family reaches BSL through a
  different pre-boot protocol (`AT*DOWNLOAD`, `0525:a4a7`, an 8-byte `0xAE`
  header). Only the BSL half is implemented here, which is the half that actually
  flashes.
- **No device has been flashed in this build.** The framing, escaping, both
  checksums, the detection, the chunking and the refusal paths are verified
  against a scripted device in `tests/cpp/test_spd.cpp`.
