# Contributing

The rules below are not style preferences. Each one exists because breaking it
has already produced a bug in this project, and in a tool that writes to flash
memory the cost of that kind of bug is a bricked device.

---

## The four rules

### 1. Do not invent protocol structures

Every byte laid out on the wire is transcribed from a primary source: the
vendor's own header, an open-source implementation with a compatible licence, or
a capture. Not from memory, not from a plausible-looking blog post, and not from
what would make the code tidy.

If a structure cannot be verified, the code says so:

```cpp
// TODO: Implement exact byte structure based on [Protocol Name].
// Blocked on: a sample file / a capture / vendor documentation.
```

and the feature reports itself as unavailable. A feature that is honestly
missing costs an operator one wasted attempt. A feature that is confidently wrong
costs them the device.

**This has already paid for itself.** Several task briefs contained claims that
primary sources contradicted, and each was flagged rather than implemented: a
MediaTek `SEND_SCATTER` wire command that does not exist; an "XOR checksum" that
belongs to the bootrom rather than the DA (the DA sums bytes); "CRC32 packet
verification" for BSL, whose packets carry 16-bit checksums; and an "AOLN magic"
in the Odin handshake, which is actually the literal string `ODIN` → `LOKE`.

### 2. Hardware communication runs off the UI thread

Non-negotiable. The UI thread never calls into the native bridge; `Backend`
enforces it with a thread-identity assertion that raises immediately.

### 3. Failures are caught, logged, and survived

A device that disconnects mid-flash must produce a classified error and a
log line, not a crash. Every slot is wrapped in `safe_slot()` because PyQt6
aborts the process when a slot raises.

### 4. Layers stay separated

`protocols/` knows nothing about libusb, Qt, or the UI. `bindings/` contains no
protocol logic. See `docs/architecture.md` for the dependency rules and the one
documented exception.

---

## Testing

Both suites must pass before anything is committed.

```bash
# Native: packet codecs, state machines, error system, logger
./scripts/build.ps1 -Test                 # or: cmake --build build && ctest --test-dir build
# or individually:
build/src/cpp/Debug/huaxin_protocol_tests.exe
build/src/cpp/Debug/huaxin_mediatek_tests.exe
build/src/cpp/Debug/huaxin_samsung_tests.exe
build/src/cpp/Debug/huaxin_spd_tests.exe
build/src/cpp/Debug/huaxin_core_tests.exe

# Python: wrappers, settings, device database, error layer
python tests/test_integration.py
python tools/test_bridge.py
QT_QPA_PLATFORM=offscreen python tests/startup_check.py
```

### What a test has to do

**Assert the bytes.** A test that checks a function did not throw proves almost
nothing about a protocol. The scripted transports exist so a test can state the
exact bytes the host emits and the exact bytes it consumes:

```cpp
ScriptedTransport transport;
transport.expect(handshake_reply());
transport.expect(response(0x64, 0x00020000));
OdinSession session(transport, quiet());
session.handshake();
const auto& written = transport.written();
check("the first packet was a session packet", written[0][0] == 0x64);
```

**Test the hostile cases.** Lengths, counts and offsets come off the wire and
drive allocations. The PIT parser is tested with a claimed 2 GB entry count; the
PAC parser is tested with an entry count that does not match the file length.

**Prefer a real finding to a green tick.** `tests/cpp/test_samsung.cpp` includes
a PIT round-trip test - build a table, serialise it, parse it back. It caught
`build_pit` writing the entry count at offset 12 where the parser reads it at
offset 4, which meant a rebuilt partition table parsed as *empty*: a repartition
would have silently wiped the table. That test exists because the round trip
seemed worth checking, not because a specification said to.

**Prove the checker can fail.** A test that cannot fail is worse than no test,
because it is trusted. When adding an assertion, break the code deliberately
once and watch it go red.

---

## Honesty in what is reported

State limitations in the code, the documentation and the UI. Concretely:

* Every catalogue entry carries a `verified` flag, and `false` means this project
  has not confirmed it on real hardware. The UI shows unverified targets
  differently.
* **No vendor download protocol in this project has been exercised against real
  hardware.** They are verified against recorded bytes, specifications and
  scripted transports. That is a real and useful level of verification, and it is
  not the same thing as a device on a bench, and the two must not be confused in
  anything written down.
* When a phase is incomplete, the tab says so. The Unisoc module is a placeholder
  that reports its blocker and does nothing else, on purpose.

A claim that is 90% true is the dangerous kind. If something is unverified, write
"unverified".

---

## Style

Match the surrounding code. Beyond that:

**Comments explain why, not what.** The code already says what it does.

```cpp
// Good: the reader cannot get this from the code.
// Flushed per line on purpose: a crash mid-flash must not lose the last thing
// that happened, which is the whole point of the file.
m_stream.flush();

// Bad: restates the line below.
// Flush the stream.
m_stream.flush();
```

A comment that says where the code came from, what the next line does, or why the
change is correct is noise the moment the change is merged. A comment that
records a constraint the code cannot express - a wire format quirk, a decision
between two options, a limitation - is the most valuable thing in the file.

**Correctness under failure beats tidiness.** When two options are equal in
clarity, choose the one that behaves better when the device disappears.

**Name things what they are.** `is_download_stated` exists next to `is_download`
because "the field says false" and "the field is absent" are different facts, and
collapsing them flipped a scatter entry to downloadable.

---

## Reporting a bug

Include the log. It is written next to the executable (`flash_log.txt`), it is
appended rather than truncated, and at debug level it records every packet:

**Help → Open log folder** reveals it. A protocol bug is usually diagnosed from
those lines alone, which is why the logger flushes per line and why the UI mirrors
into the same file.

Say which device, which mode, and which firmware package. "It failed" with a log
is a bug report; "it failed" without one is a guess.
