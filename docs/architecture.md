# Architecture

How the pieces fit together, and why each boundary is where it is. Read this
before changing anything structural; the boundaries are load-bearing.

---

## The four layers

```
                    ┌──────────────────────────────────────────┐
                    │  python/huaxin/ui/                       │
                    │  PyQt6 widgets. No hardware, no blocking.│
                    └────────────────┬─────────────────────────┘
                                     │ signals and slots
                    ┌────────────────▼─────────────────────────┐
                    │  python/huaxin/core/backend.py           │
                    │  BackendService (Qt) + Backend (plain)   │
                    │  + the per-vendor Python wrappers        │
                    └────────────────┬─────────────────────────┘
                                     │ one worker thread
                    ┌────────────────▼─────────────────────────┐
                    │  src/cpp/bindings/                       │
                    │  pybind11 glue only. No protocol logic.  │
                    └────────────────┬─────────────────────────┘
                                     │ C++ calls
                    ┌────────────────▼─────────────────────────┐
                    │  src/cpp/core/    types, catalogue, log, │
                    │                   errors, progress       │
                    │  src/cpp/usb/     libusb transports      │
                    │  src/cpp/protocols/  one directory per   │
                    │                   vendor                 │
                    └──────────────────────────────────────────┘
```

**Dependencies point downward only.** `protocols/` may use `usb/` and `core/`;
`usb/` may use `core/`; `core/` uses neither. The one deliberate exception is
noted below. A change that points an arrow upward is a change to this design, not
a detail.

### Why the boundary between `usb/` and `protocols/` is `IByteTransport`

`protocols/qualcomm/sahara.h` declares:

```cpp
class IByteTransport {
    virtual void write(const std::uint8_t* data, std::size_t length) = 0;
    virtual std::size_t read(std::uint8_t* out, std::size_t capacity, unsigned timeout_ms) = 0;
    virtual void control_transfer(...);   // has a default that throws
};
```

Every protocol is written against that interface and never against libusb. That
is what makes the protocol layers testable without hardware: the test suites link
a `ScriptedTransport` that replays recorded bytes and checks what the host emits.
It is also what lets a protocol be exercised before a device that speaks it is
available - the entire Odin and BSL implementations were verified that way.

**The one upward arrow.** `core/flash_error.h` and `core/logger.h` include
`protocols/qualcomm/sahara.h`, because the unified error system classifies
`ProtocolError` and its derivatives. The alternative was to make every protocol
depend on the error system, which is worse: it would put `std::function`, threads
and the logger into the packet codecs. The include is called out in the header's
own comment so the next reader does not have to rediscover the reasoning.

---

## Threading

Three threads matter, and the rule about them is absolute.

| Thread | Owns | May touch |
|---|---|---|
| UI (Qt main) | every widget, every panel | `BackendService` signals and slots |
| Worker (one `QThread`) | `Backend`, the `HardwareBridge`, every native call | the native bridge |
| Qt internal | event delivery | — |

**The UI thread never calls into the native bridge.** Not "should not" - cannot.
`Backend._assert_owner()` compares `threading.get_ident()` against the thread that
opened it and raises a `RuntimeError` naming the rule. A violation is a loud
failure on the first call rather than a heisenbug that appears once a week.

Every backend call is wrapped in a `Job` and submitted to the worker. Results come
back as Qt signals, which Qt delivers as **queued connections** across the thread
boundary - so result handlers run on the UI thread and may touch widgets.

### The GIL

Native calls that touch USB release it:

```cpp
.def("get_device_list", [](HardwareBridge& self, bool a, bool b) {
    py::gil_scoped_release release;      // destroyed at the end of this lambda,
    return self.get_device_list(a, b);   // i.e. *before* pybind11 converts the
}, ...)                                  // return value - which is why returning
                                         // a std::vector is safe here
```

Without that, the worker thread would hold the GIL for the whole enumeration and
the Qt event loop would stall waiting to run Python slots: the UI would freeze
even though the work is genuinely on another thread.

Progress callbacks run on the worker thread and must reacquire the GIL:

```cpp
self.set_progress_callback([function](std::uint64_t done, std::uint64_t total) {
    py::gil_scoped_acquire acquire;
    try { function(done, total); }
    catch (py::error_already_set& error) { error.discard_as_unraisable("huaxin progress"); }
});
```

An exception escaping a Python callback inside C++ would otherwise terminate the
process. It is discarded as unraisable instead, which surfaces it in the log
without taking the tool down mid-flash.

### Why an exception in a slot is fatal

PyQt6 aborts the process (`0xC0000409`) when an exception escapes a slot - it
does not propagate it. So every slot connected to a signal goes through
`safe_slot()`, which catches, reports through the registered error reporter, and
returns. `huaxin/app.py` also installs `sys.excepthook`, but that alone is not
enough: by the time PyQt would reach it, the process is already gone.

---

## Error handling

Seven classifications, and the advice differs for each. See
`docs/errors.md` for the table; the design points are here.

* **Classification happens once**, in `core::classify()` (C++) and
  `huaxin.core.errors.describe()` (Python). Nothing else inspects an exception's
  message text to decide what to do.
* **A protocol exception classifies itself** through `error_kind()`, a virtual
  returning a short tag. `ProtocolError` returns `"protocol"`, which is
  deliberately *not* a short circuit: a `ProtocolError` whose message says
  `LIBUSB_ERROR_TIMEOUT` is a pipe problem and is classified as a USB error. A
  class that knows better - `UsbDisconnectedError` returns `"disconnect"` -
  takes precedence.
* **Retrying is a property of the classification, not of the call site.**
  Timeouts and protocol stumbles are retried; a failed write, a locked
  bootloader and a missing file are not. Retrying a write that already failed is
  how a recoverable device becomes an unrecoverable one.

---

## Progress

One struct, `core::FlashProgress`, and one update rule, `FlashProgressTracker`:
emit on every 1% or every second, plus the first and last events unconditionally.
A UI that misses the final event leaves its progress bar one tick short forever,
which reads as a hang.

Speed and ETA are suppressed until the average means something - a quarter of a
second and 64 KB. Dividing by the microseconds the first packet took produces a
figure in the gigabytes per second, which is worse than showing nothing.

Each vendor's own progress event is normalised into this struct at the Python
boundary, so the UI understands exactly one shape.

---

## Logging

One process-wide `Logger`, one mutex, called from both the worker and the UI
thread. Two rules make it safe:

* The sink is invoked **outside** the lock, so a sink that logs cannot
  self-deadlock on a non-recursive mutex.
* Every line is flushed. A crash mid-flash must not lose the last thing that
  happened, which is the entire point of the file.

Rotation uses a running byte count rather than a stat, so the file never
overshoots the limit by more than the one line that crossed it.

---

## Adding a vendor protocol

1. `src/cpp/protocols/<vendor>/` - the packet codec and the session state machine,
   written against `IByteTransport`. No libusb, no Qt, no logging.
2. `src/cpp/usb/<vendor>_transport.*` - a thin libusb implementation of
   `IByteTransport`, plus the VID/PID targets.
3. `src/cpp/bindings/<vendor>_bindings.cpp` - pybind11 only. Release the GIL
   around anything that blocks.
4. `python/huaxin/core/<vendor>.py` - job-shaped functions taking the job context
   first, matching `fn(ctx, ...)`.
5. `python/huaxin/ui/panels.py` - a `VendorPanel` subclass.
6. `tests/cpp/test_<vendor>.cpp` - drive the protocol through a scripted
   transport. Assert the bytes the host emits, not just that it did not throw.
7. `docs/<vendor>-protocol.md` - what is verified and, more importantly, what is
   not.

The rule that governs all of it: **transcribe protocol facts from a primary
source; never recall them.** Where a byte structure cannot be verified, the code
carries a `// TODO: Implement exact byte structure based on [Protocol Name]` and
the UI says the feature is not available, rather than shipping something that
compiles, looks plausible, and is wrong.

---

## Settings, and the rule about them

`python/huaxin/core/config.py` holds every operator-facing setting, as a JSON
file. One rule governs the whole of it:

**A setting must change something, or it must not be offered.**

That sounds obvious and is easy to get wrong. The first version of this module
offered nineteen switches and honoured six: the dialog read and wrote all of
them, saved them to a file, and nothing else ever looked at them. An operator
could set a command timeout to five minutes and watch a transfer give up after
thirty seconds. The settings file was a description of a tool that did not exist.

`tests/test_integration.py::test_no_decorative_settings` now enforces the rule. It
walks every field and asserts something outside the dialog reads it. The check is
deliberately crude - it cannot tell whether the wiring is *correct*, only whether
there is any - because the failure it exists to catch is the one where there is
none at all. What each setting actually does is covered by the tests around it.

### How the three process-wide settings reach the native layer

Most settings are read where they are used. Three of them cannot be: a timeout, a
retry policy and a speed cap all apply inside C++, at call sites that have no
access to a Python object. Those three are pushed into the native layer once, when
the settings change, and read from there:

| Setting | Native home | Shape |
|---|---|---|
| Max attempts, backoff delays, budget | `core::RetryPolicy` | A struct, copied in and read per retry |
| Command / transfer / connect timeouts | `core::timeout_ms(kind, fallback)` | A fallback-aware lookup |
| Speed cap | `core::RateLimiter` | A rate limiter the transports pace against |

All three are published from `Backend.apply_settings()`, which runs on the worker
thread, and all three are safe to change while an operation is in flight.

### Why the timeout override is a lookup and not a constant

The obvious design is one global timeout replacing every protocol's own. It is
wrong in a way that only shows up on real hardware: the MediaTek bootrom answers
in about a millisecond, and its one-second wait is deliberate - a dead cable there
should fail in one second, not thirty. Raising every constant to a single
"command timeout" makes that device hang ten times longer on exactly the failure
the operator is trying to diagnose.

So the override sits *behind* a fallback:

```cpp
read_exact(buffer, length, core::command_timeout_ms(1000));
```

With no override set this returns `1000`, so the shipped behaviour is identical to
what it was before the override existed and the protocol tests keep testing the
same thing. Set one and every default argument in every protocol picks it up
without a single call site changing. `tests/cpp/test_core.cpp` asserts both
halves of that.

### Why the speed cap lives in the transports

`IByteTransport::write` is the one place every byte passes through, so the rate
limiter goes there: one implementation, no protocol aware of it, and command
frames throttled alongside the data. That last part is deliberate - the point of
the setting is to slow the whole conversation down for a link that cannot take the
pace, not to slow only its bulk half.

It is off by default, and off means one relaxed atomic load and one add per write.
