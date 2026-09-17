# MediaTek scatter file format

A scatter file (`MT6765_Android_scatter.txt` and similar) is the map of a
MediaTek firmware package: it lists the partitions, where each one starts, how
big it is, and which image file belongs in it. SP Flash Tool reads it, and so
does this tool's `Flash Firmware` action.

This document records what the format is, what this tool reads from it, and —
just as important — which parts are **not** verified.

## Provenance

The format has never been published as a specification. The two shapes below
were taken from two independent open-source parsers rather than from memory:

| Source | Licence | What it established |
| --- | --- | --- |
| [ersascape/mtk-fastboot-scatter](https://github.com/ersascape/mtk-fastboot-scatter) `tools/parse.py` | GPL-3.0 | The modern shape is a YAML sequence; element 0 is the general block; partitions carry `is_download`, `partition_name`, `file_name`. |
| [Rafyal/AutoFlash-Tool](https://github.com/Rafyal/AutoFlash-Tool) `src/backend/scatter_parser.py` | MIT | There are two shapes and how to tell them apart; the legacy shape is `key = value` split on `partition_index`; the modern one is `key: value` stanzas. Field names: `partition_index`, `partition_name`, `linear_start_addr` (legacy also `begin_address`), `partition_size`, `file_name`, `is_download`, `region`, `storage`. |

mtkclient was checked first and does **not** parse scatter files at all: it reads
the device's own partition table (GPT/PMT/MBR) instead, so it is not a source for
this format.

## Fields this tool reads

These are the fields with a confirmed meaning. Everything else a package carries
is preserved and shown as written — see "Unmodelled fields" below.

| Field | Meaning | Notes |
| --- | --- | --- |
| `partition_index` | The file's own key, e.g. `SYS0` | Opens an entry. Also the fallback name when the file has no `partition_name`. |
| `partition_name` | The partition's name | |
| `file_name` | The image to write there | May be `NONE` or empty; a partition with no image is one the package means to **erase**. |
| `is_download` | Whether to write it | When the key is absent it defaults to "a `file_name` was given", which is what both reference parsers do. An explicit `false` is honoured. |
| `linear_start_addr` | Start address, bytes | `begin_address` is accepted as the legacy spelling. |
| `partition_size` | Size, bytes | |
| `region` | eMMC hardware partition, e.g. `EMMC_USER`, `EMMC_BOOT_1` | Empty means the user area. |
| `storage` | e.g. `HW_STORAGE_EMMC`, `HW_STORAGE_UFS` | |
| `operation_type` | e.g. `BOOTLOADER`, `UPDATE` | **Passed through as text.** The value set is not verified, so nothing in this tool acts on it. |

Numbers are accepted in hexadecimal (`0x40000`) or decimal (`262144`). Values may
be quoted or not, and a trailing `# comment` is stripped.

### Unmodelled fields

`type`, `physical_start_addr`, `boundary_check`, `is_reserved`, `reserve` and any
vendor addition are **kept verbatim** in each entry's `extra` map and reported by
`unmodelled_keys()`. The UI shows them; the parser does not assign them meaning.

That is the deliberate choice. These keys appear in real packages, but a source
confirming what each one *means* was not found, and acting on a field whose
semantics are guessed is how a partition gets written to the wrong place. Keeping
them visible means nothing is lost and nothing is invented.

## The two shapes

### Modern (`V1.1.x`)

A YAML sequence. The first element is the general block, the rest are partitions,
usually tagged `!BitDesc`.

```yaml
############################################################################################################
#
#  General Setting
#
############################################################################################################
- general: MTK_PLATFORM_CFG
  info:
    - config_version: V1.1.2
      platform: MT6765
      project: k65v1_64_bsp
      storage: EMMC
      boot_channel: MSDC_0
      block_size: 0x20000

############################################################################################################
#
#  Layout Setting
#
############################################################################################################
- partition_index: SYS0
  partition_name: preloader
  file_name: preloader_k65v1_64_bsp.bin
  is_download: true
  type: SV5_BL_BIN
  linear_start_addr: 0x0
  physical_start_addr: 0x0
  partition_size: 0x40000
  region: EMMC_BOOT_1
  storage: HW_STORAGE_EMMC
  boundary_check: true
  is_reserved: false
  operation_type: BOOTLOADER
  reserve: 0x00

- partition_index: SYS1
  partition_name: boot
  file_name: boot.img
  is_download: true
  type: NORMAL_ROM
  linear_start_addr: 0x8000
  partition_size: 0x2000000
  region: EMMC_USER
  storage: HW_STORAGE_EMMC
  operation_type: UPDATE

- partition_index: SYS2
  partition_name: userdata
  file_name: NONE
  is_download: false
  linear_start_addr: 0x208000
  partition_size: 0x40000000
  region: EMMC_USER
  operation_type: UPDATE
```

The parser is line-oriented rather than a YAML library, so a package that is
almost-but-not-quite YAML still reads. It is not a YAML validator, and does not
pretend to be.

### Legacy (`V1.0.x`)

`key = value` blocks, with the general block first and each partition opened by
`partition_index`. There is usually no `region` or `storage` line, and the start
address is often spelled `begin_address`.

```
#
#  General Setting
#
- general: MTK_PLATFORM_CFG
  config_version = V1.0.0
  platform = MT6572
  project = k72v1
  storage = EMMC
  block_size = 0x20000

#
#  Layout Setting
#
- partition_index = SYS0
  partition_name = PRELOADER
  file_name = preloader_k72v1.bin
  is_download = true
  begin_address = 0x0
  partition_size = 0x40000

- partition_index = SYS1
  partition_name = MBR
  file_name = MBR
  is_download = true
  begin_address = 0x600000
  partition_size = 0x80000
```

## How this tool uses a scatter file

1. **Parse and show.** `Load Scatter File…` reads the file and fills the
   partition table. Nothing on the device is touched. Every row says what will
   happen to it: **write** from an image, or **erase** because the package lists
   it with no image.
2. **Check every image first.** Before the first byte is written, every named
   image file is looked up next to the scatter file. If any is missing, the run
   is refused outright and the log names the files. A package that is half
   written is a worse state to hand back than one that never started.
3. **Write in file order.** Partitions are written in the order the package lists
   them, which is the order the layout was designed for.
4. **Stop on failure, by default.** After a failed write the run stops and says
   which partition failed and which ones were already written. Carrying on would
   leave the device part new and part old, which is harder to recover than a run
   that failed early and said where. `continue_on_error` exists for the case
   where the operator wants the rest regardless, and the report says exactly what
   was and was not written either way.
5. **Report per partition.** The result carries one outcome per partition —
   written, skipped or failed, with the agent's own error text — because "it
   failed" is not actionable and "system failed with download forbidden" is.

## What is not verified

- **`operation_type` values** are not interpreted (see above).
- **`partition_size` sentinels.** Some packages use a marker to mean "whatever is
  left". This tool reads the size literally; a marker is not special-cased
  because no source confirming one was found.
- **Address units.** Addresses are treated as byte offsets, which is what the
  reference parsers and the agent's parameter block both use. A package that
  meant sectors would be wrong here, and there is no source to check against.
- **No scatter file has been replayed against real hardware in this build.** The
  parser is tested against both shapes and the edge cases in
  `tests/cpp/test_mediatek.cpp`; the DA protocol it feeds is tested against a
  scripted device. That proves the encoding, not that a particular phone accepts
  the result.
