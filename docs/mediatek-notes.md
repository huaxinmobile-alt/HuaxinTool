# MediaTek BROM protocol notes

Companion to `src/cpp/protocols/mediatek/`. These are the details that are easy
to get wrong and expensive to debug, kept next to the code that depends on them.

## Provenance and licensing

Transcribed from the bootrom protocol as implemented by
[mtkclient](https://github.com/bkerler/mtkclient) — `Library/Port.py`,
`Library/mtk_preloader.py`, `Library/Connection/devicehandler.py`.

No mtkclient code was copied. What is reused is the protocol itself: command
codes, framing rules, the checksum algorithm and the target-config bit
assignments. Those are interoperability requirements, not creative expression.

**mtkclient is GPL-3.0.** That differs from the BSD-3-Clause sources used for the
libusb build and the Qualcomm protocols. If this tool is to be distributed, this
is the one dependency whose licensing deserves a real review rather than an
assumption.

## The three things that break a naive implementation

### 1. It is big-endian

Every word the host writes and reads is big-endian. This is the **opposite** of
Sahara and Firehose, so an implementation that assumes the two vendor protocols
are consistent will send the bootrom a byte-reversed load address and a nonsense
length. It will not error; it will just do the wrong thing.

The one exception is the transfer checksum, which XORs the payload read as
*little-endian* 16-bit words. That inconsistency is in the original protocol.

### 2. Commands are acknowledged by echo

There is no status field on the command path. The host writes N bytes and reads
N bytes back; they must be identical. A mismatch means the stream has
desynchronised — and continuing would interpret a device's answer as command
data. The implementation refuses rather than continuing.

### 3. The handshake echo is a bitwise complement

The handshake word is `A0 0A 50 05`, sent **one byte at a time**. Each byte is
answered with its complement: `5F F5 AF FA`. Sending all four bytes as one write
also works on many chipsets, but the bootrom can then coalesce the echoes, so the
per-byte form is what the implementation relies on.

## Handshake and command sequence

| Step | Host sends | Device answers |
| ---- | ---------- | -------------- |
| 1 | `A0 0A 50 05`, one byte at a time | complement of each byte |
| 2 | `FD` (GET_HW_CODE) | `FD`, then a 32-bit word: hw code in the high half, hw version in the low half |
| 3 | `D8` (GET_TARGET_CONFIG) | `D8`, then 4 bytes of flags + 2 bytes of status |
| 4 | `D7` (SEND_DA) + address + length + signature length | each value echoed, then a 2-byte status |
| 5 | the agent payload | a 2-byte checksum, which must match ours |
| 6 | `D5` (JUMP_DA) + address | the address echoed back |

Status `0x1D0D` at step 4 means the bootrom wants SLA authentication before it
will accept an agent. That is a secure-boot device and this project does not
implement the signed handshake, so it reports the situation plainly instead of
retrying.

## Target configuration bits

From mtkclient's `get_target_config()`:

| Bit | Meaning |
| --- | ------- |
| 0x01 | secure boot |
| 0x02 | SLA required |
| 0x04 | DA authentication |
| 0x06 | SW JTAG — **overlaps the two bits above**, reproduced verbatim |
| 0x08 | EPP supported |
| 0x10 | certificate required |
| 0x20 | memory read allowed |
| 0x40 | memory write allowed |
| 0x80 | CMD_C8 supported |

The 0x06 mask for SW JTAG overlaps SLA (0x02) and DA authentication (0x04). That
is what upstream does, so it is copied rather than silently "corrected": if the
flag reads true, check the other two bits before believing it means JTAG.

## The download agent's protocol is a different protocol

The bootrom in this file can handshake, identify itself, receive an agent and
jump to it. That is the whole list — it has no storage commands at all. Flashing
is the *agent's* own protocol on the same wire, and it is a separate document's
worth of detail: see [the DA protocol notes](mtk-da-protocol.md) and
[the scatter file format](mtk-scatter-format.md).

The one thing worth repeating here: **the framing changes**. The bootrom layer is
big-endian with an echo and no magic; the agent layer is little-endian with a
`0xFEEEEEEF` magic on every frame. Same device, same cable, one second apart.
Assuming the two are consistent produces garbage addresses that do not error.

## What is deliberately not implemented

- **Hardware code to chip name.** `0x0672` is reported as a number, not as
  "MT6735". The mapping table has not been verified against a primary source, and
  a wrong chipset name would send an operator to the wrong download agent.
- **SLA authentication.** Reported when required by either layer, never faked.
- **The older DA protocol generations.** mtkclient implements three: legacy
  (byte commands), xflash (the framed binary protocol implemented here) and
  xmlflash (XML documents). Only xflash is implemented, because it is the one the
  task's command names match and the one current devices use. A device whose
  agent speaks only the legacy or XML dialect will not get past
  `GET_DA_VERSION` — which is exactly what that query reports, and the log says
  so rather than failing obscurely.
- **DRAM/EMI configuration blobs.** `InitExtRam` needs a chip-specific EMI
  setting. The command is implemented and takes the blob from the caller; this
  build does not ship or invent one.

## Verified how

`tests/cpp/test_mediatek.cpp` drives both protocols against a scripted device and
asserts the exact bytes the host emits — the complemented handshake, the
big-endian bootrom parameters, the XOR checksum, the little-endian agent frames,
the byte-sum checksum, the region parameter block, the chunked write and read
paths, and the refusal paths. That proves the host sends what a bootrom and an
agent expect. It does **not** prove real hardware completes the exchange: no
MediaTek device was available.
