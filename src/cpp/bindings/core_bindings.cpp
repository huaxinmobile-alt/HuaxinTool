// =============================================================================
//  pybind11 glue for the error system and the unified progress report.
//
//  THE IMPORTANT PART is the exception translation. C++ raises FlashException;
//  Python must be able to write
//
//      try:
//          backend.flash(...)
//      except huaxin_core.FlashException as error:
//          show(error.error, error.vendor, error.advice)
//
//  and read the classification off it, rather than parsing a message string.
//  py::register_exception does that: it installs a Python exception class that
//  derives from RuntimeError and translates the C++ type on the way out. The
//  attributes below (`error`, `vendor`, `advice`, ...) are then read off the
//  exception instance itself, so the handler gets the whole classified failure,
//  not just its text.
// =============================================================================

#include "bindings/core_bindings.h"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <string>
#include <vector>

#include "core/flash_error.h"
#include "core/flash_progress.h"
#include "core/flash_throttle.h"
#include "core/flash_timeouts.h"

namespace py = pybind11;

using huaxin::core::FlashError;
using huaxin::core::FlashException;
using huaxin::core::FlashProgress;
using huaxin::core::FlashProgressTracker;
using huaxin::core::OperationType;
using huaxin::core::RetryPolicy;
using huaxin::core::Vendor;

namespace huaxin::bindings {

namespace {

/// The Python class created for FlashException.
///
/// Held as a raw pointer and deliberately never released. The translator below
/// needs the class on every raise, and a `static py::object` would be destroyed
/// after the interpreter has been finalised, which is a crash on exit. Leaking
/// one reference to a type object for the life of the process is the standard
/// answer to that.
PyObject* g_flash_exception_class = nullptr;

}  // namespace

void register_core(py::module_& m) {
    // -- the seven error kinds ------------------------------------------------
    py::enum_<FlashError>(m, "FlashError",
                          "What kind of thing went wrong. This is what the UI switches on to "
                          "decide what to tell the operator, and what decides whether a retry "
                          "is offered.")
        .value("Success", FlashError::Success)
        .value("UsbError", FlashError::UsbError,
               "Device not found, disconnected, or timed out. Retryable.")
        .value("ProtocolError", FlashError::ProtocolError,
               "Invalid response, checksum failure, desynchronised stream. Retryable, but the "
               "device may be mid-write.")
        .value("FileError", FlashError::FileError,
               "File not found, corrupt, truncated, or an unsupported format. Not retryable.")
        .value("FlashError", FlashError::FlashError,
               "Write, erase or verify failed on the device. Not retried automatically: a "
               "blind retry is how a recoverable device becomes an unrecoverable one.")
        .value("AuthError", FlashError::AuthError,
               "Authentication or secure-boot refusal. Not retryable; needs signed firmware.")
        .value("InternalError", FlashError::InternalError,
               "A bug in the tool. Reported at CRITICAL and meant to be filed.")
        .value("Cancelled", FlashError::Cancelled, "The operator stopped it.");

    // to_string is overloaded for three enums, so the overload has to be picked
    // explicitly rather than left to deduction.
    m.def("flash_error_name",
          [](FlashError error) { return std::string(huaxin::core::to_string(error)); },
          py::arg("error"), "The upper-case name of an error kind, as it appears in the log.");
    m.def("advice_for", &huaxin::core::advice_for, py::arg("error"),
          "One imperative sentence telling the operator what to do about an error kind.");
    m.def("is_retryable", &huaxin::core::is_retryable, py::arg("error"),
          "True when trying the same operation again could plausibly work.");
    m.def("is_dangerous", &huaxin::core::is_dangerous, py::arg("error"),
          "True for the failures that can leave the device in an unknown state. The UI asks "
          "for confirmation before continuing when this is set.");
    m.def("classify_exception", [](const py::object& error) {
        // Two sources of truth, consulted in order.
        //
        // First the C++ identity: an exception that came out of a protocol layer
        // is still that same C++ object, and it classifies itself through its own
        // error_kind() tag. pybind11 preserves it across the boundary, which is
        // the whole point of register_exception.
        try {
            std::rethrow_exception(error.cast<std::exception_ptr>());
        } catch (const protocols::qualcomm::ProtocolError& protocol) {
            return huaxin::core::classify(protocol);
        } catch (const std::bad_alloc& allocation) {
            return huaxin::core::classify(allocation);
        } catch (const std::invalid_argument& argument) {
            return huaxin::core::classify(argument);
        } catch (const std::out_of_range& range) {
            return huaxin::core::classify(range);
        } catch (const std::logic_error& logic) {
            return huaxin::core::classify(logic);
        } catch (...) {
            // Not one of ours. Fall through to the message.
        }

        // Then the type. A Python exception carries its category in its class,
        // not in its message: a FileNotFoundError from os.stat says "no such
        // file" in English and nothing useful in a non-English locale, so
        // matching on the text would be both fragile and wrong. Mapping the class
        // onto the standard exception classify() already understands reuses those
        // rules rather than adding a second set.
        //
        // PyErr_GivenExceptionMatches is used rather than py::isinstance because
        // it accepts a class object and handles the subclass relationships
        // (FileNotFoundError is an OSError) exactly as the interpreter does.
        const auto is_a = [&error](PyObject* type) {
            return PyErr_GivenExceptionMatches(error.ptr(), type) != 0;
        };

        if (is_a(PyExc_MemoryError)) {
            return huaxin::core::classify(std::bad_alloc{});
        }
        if (is_a(PyExc_ValueError) || is_a(PyExc_TypeError) || is_a(PyExc_KeyError)) {
            return huaxin::core::classify(std::invalid_argument(py::str(error)));
        }
        if (is_a(PyExc_IndexError)) {
            return huaxin::core::classify(std::out_of_range(py::str(error)));
        }
        if (is_a(PyExc_KeyboardInterrupt)) {
            return FlashError::Cancelled;
        }
        // Before the OSError rule below, because in Python 3 TimeoutError and
        // ConnectionError both *derive* from OSError - it is OSError's catch-all
        // and FileNotFoundError that share a class name, not the other way
        // round. A timeout is the most common failure this tool sees, and
        // reporting it as a bad file would be the wrong advice entirely.
        if (is_a(PyExc_TimeoutError) || is_a(PyExc_ConnectionError)) {
            return FlashError::UsbError;
        }
        // OSError covers FileNotFoundError, PermissionError, IsADirectoryError
        // and the rest: all of them mean a file could not be read, which is a
        // FILE_ERROR and not a fault of the device.
        if (is_a(PyExc_OSError)) {
            return FlashError::FileError;
        }

        // Then the message, for the exceptions that carry nothing else. A
        // Python-only exception arrives as an error_already_set whose what() is
        // not the Python text, so re-raise the text as a runtime_error and
        // classify that - which is exactly what the message rules are for.
        return huaxin::core::classify(std::runtime_error(py::str(error)));
    }, py::arg("error"), "Classifies a Python exception object the same way the C++ layer does.");

    // The conversion point. The vendor protocol layers still raise their own
    // exceptions - ProtocolError and friends - because that is what their packet
    // code knows about. When one of those reaches Python it has no classification
    // on it, so Python's error helper asks the C++ classifier what it is and
    // re-raises it through here, getting a fully attributed FlashException that
    // the UI can act on. Without this, a handler higher up would have to guess
    // from the message text whether a retry is worth offering.
    m.def(
        "raise_flash_error",
        [](FlashError error, Vendor vendor, const std::string& message,
           const std::string& operation) -> void {
            FlashException failure(error, vendor, message);
            if (!operation.empty()) {
                failure.add_context(operation);
            }
            throw failure;
        },
        py::arg("error"), py::arg("vendor"), py::arg("message"), py::arg("operation") = "",
        "Raises a FlashException carrying the given classification. Python uses this to "
        "re-raise a vendor failure it has classified, so every failure the UI sees is the "
        "same type with the same attributes.");

    // -- vendors --------------------------------------------------------------
    py::enum_<Vendor>(m, "Vendor", "Which vendor's protocol was talking.")
        .value("Unknown", Vendor::Unknown)
        .value("Core", Vendor::Core)
        .value("Adb", Vendor::Adb)
        .value("Fastboot", Vendor::Fastboot)
        .value("Qualcomm", Vendor::Qualcomm)
        .value("MediaTek", Vendor::MediaTek)
        .value("Unisoc", Vendor::Unisoc)
        .value("Samsung", Vendor::Samsung);

    m.def("vendor_from_usb_id", &huaxin::core::vendor_from_usb_id, py::arg("vid"),
          "The vendor a USB vendor ID belongs to, for a failure that happened before a "
          "protocol was chosen.");

    // -- the exception --------------------------------------------------------
    //
    // pybind11's own translator copies only what() into the Python exception and
    // throws the C++ object away, so a handler would get the message and none of
    // the classification - it would have to parse the text to find out whether
    // the failure is retryable. The translator below instead builds the Python
    // exception itself and hangs the classified fields off it, so Python can
    // read `.error`, `.vendor`, `.advice` and the rest as ordinary attributes.
#ifdef HUAXIN_NO_EXCEPTION_TYPE
    // Diagnostic build: create the class without pybind11's own translator, so
    // the GIL assertion can be attributed to the translator or cleared of it.
    py::object created = py::reinterpret_steal<py::object>(
        PyErr_NewException("huaxin_core.FlashException", PyExc_RuntimeError, nullptr));
    m.attr("FlashException") = created;
    g_flash_exception_class = created.ptr();
    Py_INCREF(g_flash_exception_class);
    py::object& exception = created;
#else
    auto& exception = py::register_exception<FlashException>(m, "FlashException", PyExc_RuntimeError);
    {
        // Deliberately leaked. See the comment on g_flash_exception_class.
        g_flash_exception_class = exception.ptr();
        Py_INCREF(g_flash_exception_class);
    }
#endif
    exception.attr("__doc__") =
        "A classified failure: what went wrong, which vendor was talking, when, and what to do "
        "about it.\n\n"
        "Derives from RuntimeError, so handlers that already catch RuntimeError keep working.\n\n"
        "Attributes:\n"
        "    error         - a FlashError, the classification\n"
        "    error_name    - its upper-case name, as the log writes it\n"
        "    vendor        - a Vendor, whose protocol was talking\n"
        "    vendor_name   - its display name\n"
        "    message       - the failure without the [KIND] [Vendor] prefix\n"
        "    advice        - one imperative sentence for the operator\n"
        "    retryable     - whether trying again could plausibly work\n"
        "    dangerous     - whether the device may be left mid-write\n"
        "    timestamp     - seconds since the epoch\n"
        "    timestamp_text- the same in local time\n"
        "    context       - list of (operation, detail) frames, outermost first\n"
        "    report_text   - the whole thing as a multi-line block for a log\n"
        "\n"
        "Every attribute is already computed: none of them is a method, so nothing\n"
        "needs calling. `report_text` is named to say so - a plain `report` would\n"
        "read like a method and be called as one.";

    py::register_exception_translator([](std::exception_ptr failure) {
        if (failure == nullptr) {
            return;
        }
        try {
            std::rethrow_exception(failure);
        } catch (const FlashException& error) {
            if (g_flash_exception_class == nullptr) {
                // Not registered yet. Throwing hands it to the next translator;
                // returning would claim it was handled.
                throw;
            }
            py::object type = py::reinterpret_borrow<py::object>(g_flash_exception_class);
            // Constructing with the message makes str(exception) useful as well
            // as setting the fields, so a logger that only prints the exception
            // still gets the classified text.
            py::object instance = type(error.what());
            instance.attr("message") = error.message();
            instance.attr("error") = error.error();
            instance.attr("error_name") = std::string(huaxin::core::to_string(error.error()));
            instance.attr("vendor") = error.vendor();
            instance.attr("vendor_name") = std::string(huaxin::core::to_string(error.vendor()));
            instance.attr("advice") = error.advice();
            instance.attr("retryable") = error.retryable();
            instance.attr("dangerous") = huaxin::core::is_dangerous(error.error());
            instance.attr("timestamp") = error.timestamp();
            instance.attr("timestamp_text") = error.timestamp_text();
            instance.attr("report_text") = error.report();

            py::list context;
            for (const auto& frame : error.context()) {
                context.append(py::make_tuple(frame.operation, frame.detail));
            }
            instance.attr("context") = context;

            PyErr_SetObject(g_flash_exception_class, instance.ptr());
        } catch (...) {
            // Not a FlashException, so this translator has nothing to say about
            // it. It MUST throw to pass it on: pybind11 treats a normal return
            // from a translator as "handled" and stops trying the rest, so a
            // bare `return` here would swallow every ProtocolError and hand
            // Python "returned NULL without setting an exception" instead of the
            // device failure that actually happened. That is exactly what this
            // did until tests/test_bridge.py caught it.
            throw;
        }
    });

    // Plain data carrier, so the retry policy is configurable from Python.
    py::class_<RetryPolicy>(m, "RetryPolicy",
                            "How a retry should be paced. The defaults are the shipped ones: "
                            "three attempts, 250 ms doubling to a 5 s ceiling.")
        .def(py::init<>())
        .def_readwrite("attempts", &RetryPolicy::attempts,
                       "Total goes including the first. 1 disables retrying.")
        .def_readwrite("initial_delay_ms", &RetryPolicy::initial_delay_ms)
        .def_readwrite("maximum_delay_ms", &RetryPolicy::maximum_delay_ms)
        .def_readwrite("total_budget_ms", &RetryPolicy::total_budget_ms,
                       "Give up entirely after this. 0 disables the budget.")
        .def("delay_for_attempt", &huaxin::core::backoff_delay_ms, py::arg("attempt"),
             "The wait before a given attempt, with exponential backoff applied.")
        .def("__repr__", [](const RetryPolicy& self) {
            return "RetryPolicy(attempts=" + std::to_string(self.attempts)
                   + ", initial_delay_ms=" + std::to_string(self.initial_delay_ms)
                   + ", maximum_delay_ms=" + std::to_string(self.maximum_delay_ms) + ")";
        });

    m.def("default_retry_policy", &huaxin::core::default_retry_policy,
          "The retry policy the tool will use for the next operation.");
    m.def("set_default_retry_policy", &huaxin::core::set_default_retry_policy,
          py::arg("policy"),
          "Replaces the process-wide retry policy. Called from the settings dialog; "
          "read by any layer that runs its own retry loop, so a change here reaches "
          "operations that never see the settings object.");

    // -- timeouts -------------------------------------------------------------
    // Three numbers, in milliseconds, applied to the whole native layer at once.
    // Zero means "no override" for that kind, which restores each protocol's own
    // constant - see core/flash_timeouts.h for why the override sits behind a
    // fallback rather than replacing the constants outright.
    m.def("set_timeout_overrides", &huaxin::core::set_timeout_overrides,
          py::arg("command_ms"), py::arg("transfer_ms"), py::arg("connect_ms"),
          "Sets the command, transfer and connect timeouts in milliseconds. Zero for "
          "any of them means no override.");
    // -- speed cap ------------------------------------------------------------
    // Applied to the transports, which is the one place every byte passes
    // through, so one setting reaches every vendor without any protocol knowing
    // it exists. Zero means no cap, which is the shipped default.
    m.def("set_speed_limit", &huaxin::core::set_speed_limit, py::arg("bytes_per_second"),
          "Caps how fast data is sent, in bytes per second. Zero removes the cap.");
    m.def("speed_limit", &huaxin::core::speed_limit,
          "The current speed cap in bytes per second, or zero.");

    m.def("timeout_overrides", []() {
        unsigned int command = 0;
        unsigned int transfer = 0;
        unsigned int connect = 0;
        huaxin::core::timeout_overrides(command, transfer, connect);
        return py::make_tuple(command, transfer, connect);
    }, "The current overrides as (command_ms, transfer_ms, connect_ms), zero where "
       "none is set.");

    // -- progress -------------------------------------------------------------
    py::enum_<OperationType>(m, "OperationType",
                             "What the tool is doing, in the operator's words.")
        .value("Unknown", OperationType::Unknown)
        .value("Preparing", OperationType::Preparing)
        .value("Connecting", OperationType::Connecting)
        .value("Uploading", OperationType::Uploading)
        .value("Reading", OperationType::Reading)
        .value("Writing", OperationType::Writing)
        .value("Erasing", OperationType::Erasing)
        .value("Verifying", OperationType::Verifying)
        .value("Resetting", OperationType::Resetting);

    m.def("operation_type_name",
          [](OperationType type) { return std::string(huaxin::core::to_string(type)); },
          py::arg("operation"));
    m.def("parse_operation_type", &huaxin::core::parse_operation_type, py::arg("name"));

    py::class_<FlashProgress>(m, "FlashProgress",
                              "One progress report. Everything the UI needs and nothing it "
                              "has to compute. Read-only: this describes work in progress, so "
                              "mutating a copy would change nothing real.")
        .def_readonly("operation_type", &FlashProgress::operation_type)
        .def_readonly("current_partition", &FlashProgress::current_partition)
        .def_readonly("total_partitions", &FlashProgress::total_partitions)
        .def_readonly("current_partition_index", &FlashProgress::current_partition_index)
        .def_readonly("bytes_written", &FlashProgress::bytes_written)
        .def_readonly("total_bytes", &FlashProgress::total_bytes)
        .def_readonly("percentage", &FlashProgress::percentage,
                      "0-100, or -1 when the total is unknown. Deliberately not clamped to 0: "
                      "'we do not know' and 'nothing done' render differently.")
        .def_readonly("speed_mbps", &FlashProgress::speed_mbps)
        .def_readonly("eta_seconds", &FlashProgress::eta_seconds,
                      "Seconds remaining, or -1 when it cannot be estimated.")
        .def_readonly("status_message", &FlashProgress::status_message)
        .def_readonly("vendor", &FlashProgress::vendor)
        .def_readonly("running", &FlashProgress::running)
        .def_property_readonly("has_percentage", &FlashProgress::has_percentage)
        .def_property_readonly("has_eta", &FlashProgress::has_eta)
        .def_property_readonly("operation_name",
                               [](const FlashProgress& self) {
                                   return std::string(huaxin::core::to_string(
                                       self.operation_type));
                               })
        .def("describe", &FlashProgress::describe,
             "One line summarising the report, for a status bar or a log entry.")
        .def("__repr__", [](const FlashProgress& self) {
            return "FlashProgress(" + self.describe() + ")";
        });

    m.def("format_bytes", &huaxin::core::format_bytes, py::arg("bytes"),
          "Formats a byte count the way a person reads it: '512 MB', '1.4 GB'.");
    m.def("format_duration", &huaxin::core::format_duration, py::arg("seconds"),
          "Formats a duration as '45s', '1m 22s' or '1h 04m'.");

    // The tracker is exposed so the Python layer can drive the same update rule
    // for its own operations - ADB and Fastboot transfers happen entirely in
    // Python and would otherwise need a second, subtly different implementation
    // of the 1%-or-1-second contract.
    py::class_<FlashProgressTracker>(m, "FlashProgressTracker",
                                     "Computes speed and ETA and applies the update rule: emit on "
                                     "every 1% or every second, plus the first and last events.\n\n"
                                     "Not thread-safe by design: one tracker belongs to the one "
                                     "worker thread running the operation.")
        .def(py::init<OperationType, Vendor, std::string, std::uint64_t, std::size_t, std::size_t>(),
             py::arg("operation_type"), py::arg("vendor"), py::arg("partition"),
             py::arg("total_bytes"), py::arg("index") = 0, py::arg("count") = 0)
        .def("should_emit", &FlashProgressTracker::should_emit, py::arg("bytes"),
             "True when this event should be sent on.")
        .def("build", &FlashProgressTracker::build, py::arg("bytes"), py::arg("status") = "",
             "Builds the report. Also advances the clock, so call it only when the event is "
             "actually going out.")
        .def("flush", &FlashProgressTracker::flush,
             "Forces the next event through, for the end of an operation.")
        .def("set_percent_step", &FlashProgressTracker::set_percent_step, py::arg("step"))
        .def("set_interval_ms", &FlashProgressTracker::set_interval_ms, py::arg("millis"))
        .def_property_readonly("last", &FlashProgressTracker::last);
}

}  // namespace huaxin::bindings
