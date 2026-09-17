# Errors

Every failure in this tool is classified into one of seven kinds. The
classification decides what the operator is told, whether a retry is offered, and
whether the UI warns that the device may be left in an unknown state.

The types live in `src/cpp/core/flash_error.h` and are exposed to Python as
`huaxin_core.FlashError`, with the same names.

---

## The seven kinds

| Kind | Meaning | Retried? | Device at risk? | What the operator is told |
|---|---|---|---|---|
| `SUCCESS` | Nothing went wrong. | — | — | Nothing to do. |
| `USB_ERROR` | Device not found, disconnected, or timed out. | **Yes** | No | Reconnect with a known-good cable, into a rear USB port directly on the motherboard, then scan again. |
| `PROTOCOL_ERROR` | The device answered, and the answer did not fit the protocol: a bad checksum, a frame that will not parse, a response to something else. | **Yes** | **Yes** | Disconnect, put the device back into the correct mode, and start the operation again from the beginning. A partially written device must be reflashed in full. |
| `FILE_ERROR` | A missing file, a corrupt package, a format this build does not read. | No | No | Check the firmware package: it may be missing, truncated, or in a format this build does not read. Verify the download first. |
| `FLASH_ERROR` | The write, erase or verification itself failed on the device. | **No** | **Yes** | Do not unplug it. Retry once; if it fails again, reflash the full package rather than a single partition. |
| `AUTH_ERROR` | The device's security policy refused: secure boot, SLA, a locked bootloader, an anti-rollback check. | No | No | A signed firmware build or an authorised account is required. A different package will not help while the bootloader stays locked. |
| `INTERNAL_ERROR` | A bug in the tool, an allocation failure, an impossible state. | No | No | This is a bug in the tool, not the device. Save the log and report it with the device model. |
| `CANCELLED` | The operator stopped it. | No | **Yes** | The device may be left in a partial state - rescan and reflash before using it. |

**Why a failed write is not retried.** Retrying blindly is how a device that was
merely mid-write becomes a device with a half-written bootloader. The operator is
told to retry *once*, deliberately, and to reflash the whole package if that
fails. The tool does not make that decision for them.

**Why a protocol error *is* retried, and is flagged as dangerous.** A checksum
mismatch or a desynchronised stream usually means the link stumbled, and a fresh
attempt from a known state succeeds. But it can equally mean a write was cut
short, so the retry is allowed and the risk is stated.

---

## How an exception is classified

`core::classify()` in C++, or `huaxin.core.errors.describe()` in Python, which
delegates to it. Nothing else inspects an exception's text to decide what to do.

1. **The exception's own tag.** A `ProtocolError` subclass that knows something
   more specific returns it from `error_kind()`:

   ```cpp
   class UsbDisconnectedError : public ProtocolError {
       const char* error_kind() const noexcept override { return "disconnect"; }
   };
   ```

   The generic tag `"protocol"` is deliberately **not** a short circuit. A
   `ProtocolError` whose message reads `USB failure while writing:
   LIBUSB_ERROR_TIMEOUT` is a pipe problem, and reporting it as a protocol fault
   would tell the operator the device stopped speaking the protocol when the
   truth is that the transfer timed out. A tag that is not specific falls through
   to the rules below.

2. **The exception's class**, for Python exceptions, which carry their category
   in their type rather than their message. `TimeoutError` and `ConnectionError`
   are checked before `OSError` - in Python 3 both derive from it, and a timeout
   that fell through to the file rule would tell the operator to check their
   firmware package.

3. **The message**, for exceptions that carry nothing else. Ordered most specific
   first.

4. **Anything left is `PROTOCOL_ERROR`.** In this codebase an unclassified
   `runtime_error` comes from a protocol layer, and calling it a protocol error
   keeps it retryable and marks the device as possibly mid-write - which is safer
   than declaring a bug and skipping the retry.

`std::bad_alloc`, `std::logic_error` and unrecognised exceptions outside the
protocol tree become `INTERNAL_ERROR`: a bug report, not a confident wrong guess.

---

## Using it

### Python

```python
import huaxin_core

try:
    backend.flash_partition(...)
except huaxin_core.FlashException as error:
    print(error.error_name)     # "USB_ERROR"
    print(error.vendor_name)    # "Qualcomm"
    print(error.advice)         # one imperative sentence
    print(error.retryable)      # bool
    print(error.dangerous)      # bool
    print(error.context)        # [("flashing boot.img", "SYSTEM")]
    print(error.report_text)    # multi-line block for the log
```

Every attribute is already computed - none of them is a method. `report_text` is
named to say so.

A vendor failure that reached Python unclassified is converted through
`huaxin.core.errors`:

```python
from huaxin.core import errors

failure = errors.describe(error, vendor="MediaTek", operation="erasing userdata")
print(failure.kind, failure.retryable, failure.advice)
```

`errors.guard(vendor, operation)` is a decorator that does this on the way out of
a wrapper function, so a caller can write one `except FlashException` block and
know it catches everything.

### Retrying

```python
from huaxin.core import errors

policy = huaxin_core.default_retry_policy()      # or from Settings
result = errors.run_with_retry(
    lambda: do_the_thing(),
    policy,
    on_retry=lambda attempt, failure: log(f"attempt {attempt} failed: {failure.summary}"),
    should_stop=lambda: cancel_requested,
)
```

Only `retryable` failures are repeated. `should_stop` is consulted before every
wait, so a cancellation does not have to sit out a backoff.

### C++

```cpp
#include "core/flash_error.h"

using namespace huaxin::core;

auto outcome = guard(Vendor::Qualcomm, "flashing boot.img", [&] {
    return session.flash_partition(...);          // throws ProtocolError or its own
});

auto result = with_retry(default_retry_policy(), [&] {
    return session.flash_partition(...);
}, [&](unsigned int attempt, const FlashException& error) {
    logger.log_exception(error);
});
```

`guard` translates; `with_retry` paces. The retry rule lives in exactly one place
so the two languages cannot disagree about what "three attempts" means - and
`tests/test_integration.py` asserts that the C++ and Python backoff sequences are
identical, element for element.

---

## Backoff

Attempts, with the wait doubling between them and capped:

| Attempt | Wait (default policy) |
|---|---|
| 1 | — (first try) |
| 2 | 250 ms |
| 3 | 500 ms |
| 4 | 1000 ms |
| 5 | 2000 ms |
| 6+ | 5000 ms (the ceiling) |

The default policy is three attempts, a 250 ms first delay, a 5 s ceiling and a
60 s total budget. All four are editable in **File → Settings → Timeouts &
retries**, and the budget stops a retry even when attempts remain.

---

## Advice, and where it comes from

The advice text is not decorative. Each line names something the operator can
actually do; where the honest answer is "there is nothing to do but start over",
it says that instead of inventing a step. The strings live in
`advice_for()` in `core/flash_error.cpp`, which is the single source for both
languages - Python reads them back through `huaxin_core.advice_for()` rather than
keeping its own copy.
