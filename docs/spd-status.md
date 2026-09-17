# Unisoc / Spreadtrum status

This phase was originally **blocked**, and the reason was recorded rather than
guessed around: the `.pac` container layout and the Research Download framing
could not be verified, and this project does not invent proprietary byte
structures. See the end of this document for what that blocker was.

**It is resolved.** Both formats are now implemented and tested.

## What the blocker was

The original note said, correctly at the time:

> Searches for an open-source implementation with a usable licence came up empty.
> There is no Unisoc equivalent of Heimdall (Samsung, MIT) or qdl (Qualcomm,
> BSD-3-Clause) that could be transcribed.

That search was **wrong**, and the correction came from looking harder in a
different place. Three things existed:

1. **The vendor's own header.** `Mani-Sadhasivam/unisoc-dloader` carries
   `include/BinPack.h` — Spreadtrum's own definition of the packet format and the
   file table, including `PAC_MAGIC` and the annotations that explain the V1 to V2
   field changes. A source more authoritative than any of the tools.
2. **Three independent clients** that agree byte for byte with it:
   `ajsb85/sprdflash-rs` (MIT), `iscle/sprdclient` (GPL-3.0) with its companion
   `iscle/unpac`, and `affggh/unpac_py`.
3. **A complete command enum**, in `Mani-Sadhasivam/uwpflash` (Apache-2.0)
   `command.h`, with the meaning of every BSL command and response written next
   to it.

The lesson is worth keeping: "no open-source implementation exists" is a claim
about a search, not about the world, and a second search with different terms
found four.

## What is implemented

| Piece | Where | Verified how |
| --- | --- | --- |
| PAC parser, both layouts | `protocols/spd/pac.{h,cpp}` | Packages built byte for byte to the vendor's layout, including every rejection path |
| The four checksums | `protocols/spd/checksum.{h,cpp}` | Published check values for each named algorithm |
| BSL protocol and framing | `protocols/spd/bsl.{h,cpp}` | A scripted device, frame by frame |
| FDL1 / FDL2 extraction | `PacLoaders` in `pac.h` | The same built packages |
| USB transport | `usb/spd_transport.{h,cpp}` | Enumeration only; the protocol is tested without USB |

`tests/cpp/test_spd.cpp` runs **122 checks** covering: the four checksums against
their published values, both PAC layouts, wrong magic, truncation, an implausible
entry count, an entry pointing outside the file, an unknown entry layout, a
missing FDL1, both CRCs, HDLC escaping and unescaping, the command and response
tables, the handshake including the control transfer, checksum detection for both
chip families, a three-chunk payload download with every frame decoded back, and
the refusal paths.

## What is not done

- **The Python wrapper and the tab's buttons.** `protocols/spd/` is not yet
  exposed through pybind11, so the SPD tab still reports that its actions are
  unavailable. It says *that*, and not "blocked", because blocked is no longer
  true.
- **`READ_FLASH_INFO`'s reply layout.** The command and its response code are
  confirmed; the fields inside the reply are not, so the bytes are returned raw
  rather than sliced into named fields that might be wrong.
- **The baud-rate renegotiation.** `CHANGE_BAUD` exists and SPD devices are known
  to use it, but no source showed the sequence. The link runs at the speed it came
  up at.
- **NV CRC refresh.** FDL2 validates a CRC-16/ARC at the head of an NV image at
  `END_DATA`; the checksum is implemented, the write path does not apply it yet.
- **The PDL pre-stage.** The RDA8910/UIS8910 family reaches BSL through a
  different pre-boot protocol. Only the BSL half — the half that flashes — is
  implemented.
- **Nothing has been run against real hardware.** Every check above is against a
  scripted device and packages this repository built itself. That proves the
  encoder, not that a particular phone accepts the result.

## Formats

- [spd-pac-format.md](spd-pac-format.md) — the container, both layouts, the
  checksums, and what is deliberately not interpreted.
- [spd-bsl-protocol.md](spd-bsl-protocol.md) — the wire protocol, the framing, the
  command set, and the checksum trap.
