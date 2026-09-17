#!/usr/bin/env bash
# Configures and builds the Huaxin Tool native backend (huaxin_core).
#
#   ./scripts/build.sh              # Release
#   ./scripts/build.sh Debug        # Debug
#   PYTHON_EXE=python ./scripts/build.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build"
BUILD_TYPE="${1:-Release}"
PYTHON_EXE="${PYTHON_EXE:-python3}"

CMAKE_ARGS=(-S "${REPO_ROOT}" -B "${BUILD_DIR}" "-DPython3_EXECUTABLE=${PYTHON_EXE}")

# Prefer the pybind11 that pip installed for this exact interpreter, so the
# extension is built against the Python that will import it.
if PYBIND11_DIR="$("${PYTHON_EXE}" -c 'import pybind11, pathlib; print(pybind11.get_cmake_dir())' 2>/dev/null)"; then
    echo "pybind11 cmake dir: ${PYBIND11_DIR}"
    CMAKE_ARGS+=("-Dpybind11_DIR=${PYBIND11_DIR}")
else
    echo "warning: '${PYTHON_EXE}' has no pybind11 installed; falling back to FetchContent (needs git)." >&2
    echo "warning: install it with: ${PYTHON_EXE} -m pip install pybind11" >&2
fi

echo
echo "== Configuring =="
cmake "${CMAKE_ARGS[@]}"

echo
echo "== Building (${BUILD_TYPE}) =="
cmake --build "${BUILD_DIR}" --config "${BUILD_TYPE}" --parallel

echo
echo "== Done =="
echo "Verify the Python bridge with:  ${PYTHON_EXE} python/tools/test_bridge.py"
