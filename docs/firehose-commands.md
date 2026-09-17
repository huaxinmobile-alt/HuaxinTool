# Firehose XML commands — examples and provenance

Everything here is the wire form of the commands this tool sends. The attribute
names and the response shape were transcribed from Qualcomm's upstream Linux EDL
tool [linux-msm/qdl](https://github.com/linux-msm/qdl) (BSD-3-Clause), files
`src/firehose.c` and `src/qdl.h`. Where a value could not be verified from that
source it is marked **unverified** rather than guessed.

The builders that produce these strings are pure functions and are exposed to
Python (`build_configure_xml`, `build_read_xml`, `build_program_xml`,
`build_erase_xml`, `build_patch_xml`, `build_power_xml`, `build_get_storage_info_xml`,
`build_ping_xml`), so any of them can be inspected before it is sent. Everything
the tool sends is also logged at `debug` level as `FIREHOSE -> …`.

## Request and response shape

A request is a complete XML document on the bulk-out endpoint. The prolog is not
optional; qdl sends it and the programmer's parser expects it.

```
<?xml version="1.0" ?><data><ping/></data>
```

A response comes back unframed — the programmer does not say how long it is — so
the host reads until `</data>`:

```
<?xml version="1.0" ?><data>
  <log value="Calling handler for ping"/>
  <response value="ACK"/>
</data>
```

Three things can appear inside `<data>`:

| Element | Meaning |
| --- | --- |
| `<log value="…"/>` | Free text, zero or more. Often the only place a geometry or a reason appears. |
| `<response value="ACK\|NAK" rawmode="true"/>` | The verdict. `rawmode="true"` means raw bytes follow and XML parsing must stop until they are consumed. |
| `<response value="LOG"/>` | Informational, not a verdict. |

`ACK` is success, `NAK` is a refusal, and no `<response>` at all is neither —
`parse_firehose_response` reports that as `Unknown` rather than assuming success.

## Command reference

### `<ping/>`

The cheapest liveness check. Worth sending after `<configure>` if a later
command behaves oddly.

```
<?xml version="1.0" ?><data><ping/></data>
```

### `<configure>`

Must be the first command after the programmer loads. Everything downstream —
read, program, erase — depends on the payload size negotiated here.

```
<?xml version="1.0" ?><data>
  <configure MemoryName="UFS" Verbose="0" AlwaysValidate="0" MaxDigestTableSizeInBytes="8192"
             MaxPayloadSizeToTargetInBytes="1048576" ZlpAwareHost="1" SkipStorageInit="0"/>
</data>
```

| Attribute | Notes |
| --- | --- |
| `MaxPayloadSizeToTargetInBytes` | The ceiling the host will send in one write. This tool streams in 1 MiB chunks. |
| `ZlpAwareHost` | `1` when the host understands zero-length packets as a transfer end. |
| `SkipStorageInit` | `1` skips re-initialising the storage engine. |
| `MemoryName` | The storage type. **Unverified spelling** — see below. |

**About `MemoryName`.** The storage *types* (`eMMC`, `nand`, `UFS`, `NVMe`,
`spi`) come from qdl's `enum qdl_storage_type`, but the exact strings its
`encode_storage_type()` emits live in a file that is not vendored in this
project, so the spellings are not confirmed here. Rather than default to a
guess, this tool omits the attribute unless the operator picks a value — an
omitted attribute lets the programmer use its own default, which is the safe
choice when the storage type is unknown. The UI offers the candidates in an
editable field so a target that wants something else does not require editing
the code.

Both UFS and eMMC targets are handled the same way: nothing in this tool's read,
write, erase or GPT path depends on the storage label. What differs is the sector
geometry, and that comes from the device via `<getstorageinfo>` (see below), not
from the label.

### `<getstorageinfo>`

```
<?xml version="1.0" ?><data><getstorageinfo physical_partition_number="0"/></data>
```

The answer is not a dedicated element. The programmer puts the geometry in a
`<log>` value as an escaped JSON blob:

```
<log value="{&quot;storage_info&quot;:{&quot;total_blocks&quot;:61071360,&quot;block_size&quot;:512,&quot;storage_type&quot;:&quot;UFS&quot;}}"/>
<response value="ACK"/>
```

61071360 blocks × 512 bytes = 31.3 GB. Some firmware answers with a bare `ACK` and
no geometry; this tool then reports that it does not know the geometry rather than
assuming one, and falls back to a 512-byte sector with a warning in the log.

### `<read>`

```
<?xml version="1.0" ?><data>
  <read SECTOR_SIZE_IN_BYTES="4096" num_partition_sectors="1" physical_partition_number="0"
        start_sector="1" />
</data>
```

On `ACK` with `rawmode="true"` the device streams
`num_partition_sectors × SECTOR_SIZE_IN_BYTES` bytes of payload, then sends a
closing response. Reading more than 512 MiB in one go is refused by this tool
because the whole transfer is held in memory.

### `<program>`

```
<?xml version="1.0" ?><data>
  <program SECTOR_SIZE_IN_BYTES="4096" file_sector_offset="0" filename="boot.img"
           label="boot" num_partition_sectors="16384" physical_partition_number="0"
           start_sector="48" />
</data>
```

The sequence is: the command, an `ACK` confirming the setup, then the image bytes
as raw payload, then a final verdict. The last step is the one that takes as long
as the flash does, so it gets a 120-second timeout while the setup gets 10.

`filename` is informational — the programmer names the file in its own logs; the
bytes that land are the ones the host sends. This tool sends the file's contents
without padding, which is what qdl does. **Unverified:** whether any given
programmer requires the payload padded to a sector or payload boundary. If a
programmer does, a write of an unpadded image would be short; the log line for
each partition states the byte count so it can be compared against the file.

### `<erase>`

```
<?xml version="1.0" ?><data>
  <erase SECTOR_SIZE_IN_BYTES="4096" physical_partition_number="0"
         start_sector="48" num_partition_sectors="16384"/>
</data>
```

### `<patch>`

Writes a value into a partition that is already programmed — the step a
`patch0.xml` describes. It carries the value inline, as text:

```
<?xml version="1.0" ?><data>
  <patch SECTOR_SIZE_IN_BYTES="4096" byte_offset="8" filename="boot.img"
         physical_partition_number="0" size_in_bytes="12" start_sector="48"
         value="HUAXIN-TEST"/>
</data>
```

Because it modifies an image that must already be there, a patch runs *after* the
rawprogram, never before. `value` is escaped, so a quote in it cannot close the
attribute early.

### `<power>`

```
<?xml version="1.0" ?><data><power value="reset" DelayInSeconds="10"/></data>
```

`reset` restarts the device out of EDL and the session ends with it. The device
stops answering once it resets, so a missing response here is expected rather
than an error.

## `rawprogramN.xml` and `patchN.xml`

A firmware package ships a `rawprogram0.xml` listing what to write where, and
usually a `patch0.xml` with the values to stamp in afterwards. Both are lists of
the same elements above, wrapped in `<data>`.

```xml
<?xml version="1.0" ?>
<data>
  <program SECTOR_SIZE_IN_BYTES="4096" file_sector_offset="0" filename="gpt_main0.bin"
           label="PrimaryGPT" num_partition_sectors="6" physical_partition_number="0"
           start_sector="0" />
  <program SECTOR_SIZE_IN_BYTES="4096" file_sector_offset="0" filename="boot.img"
           label="boot" num_partition_sectors="16384" physical_partition_number="0"
           start_sector="48" />
</data>
```

```xml
<?xml version="1.0" ?>
<data>
  <patch SECTOR_SIZE_IN_BYTES="4096" byte_offset="8" filename="boot.img"
         physical_partition_number="0" size_in_bytes="12" start_sector="48"
         value="HUAXIN-TEST"/>
</data>
```

Image paths are resolved relative to the XML's own directory, which is how these
packages are laid out. Attributes the tool does not model (`file_sector_offset`,
and any vendor extras) are ignored rather than treated as errors: they do not
change where the bytes land.

Before anything is written, the whole file is parsed and shown to the operator —
every partition, its size and its start sector. If any named image file is
missing, the run is refused outright rather than half-completed, because a
partition written from the wrong file is worse than one not written at all.

## Ordering, which is not optional

```
connect
  └─ Sahara: read identity, upload programmer
       └─ <configure>
            ├─ <getstorageinfo>          (geometry; optional but useful)
            ├─ <read> LBA 1, then the entry array   ← Read GPT
            ├─ <program> … <erase> …     (per partition)
            └─ <patch>                   (after its partition is programmed)
       └─ <power value="reset">          (ends the session)
```

A Firehose command cannot be sent to a device that is still waiting for its
programmer. This tool enforces that at both layers: `QualcommEdl` refuses any
Firehose call before an upload, and the Python wrapper refuses any command when
no session is open, naming the missing step in the message.

The session is kept open across operations on purpose. Uploading a programmer
takes seconds and re-initialises the storage engine, so configure → read GPT →
flash has to happen inside one session; rebuilding it per click would make the
sequence impossible.

## What has not been verified

Stated plainly, because the difference matters when a flash fails:

- **No command on this page has been exercised against real hardware in this
  build.** The parsers and builders are tested against byte sequences built to
  the transcribed structures, and the Sahara handshake is tested against a
  scripted transport. That catches encoding mistakes; it does not prove a
  particular phone accepts the result.
- **`MemoryName` spellings** are unverified (see above).
- **Payload padding** for `<program>` is unverified (see above).
- **`<patch>` `value` encoding**: this tool sends the value as the text in the
  XML attribute. Whether every programmer expects that text, or expects it
  hex-encoded for binary payloads, is not confirmed from a primary source.
