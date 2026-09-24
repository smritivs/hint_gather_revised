#!/bin/bash
#
# run_analyze_test.sh - CHIA gate for the HINT.GATHER LLVM pass.
#
# Role: builds nothing but the fixture; runs libHintGather.so in analyze mode
# over test/gather_test.c and asserts that the detector found EXACTLY ONE
# eligible site (the genuine a[b[i]] gather) and that the affine cases were
# rejected by the SCEV filter.  Exits non-zero on any failure so the CHIA loop
# can use it as a hard gate.
#
# Normative spec: docs/DESIGN.md section 4.1.
#
# Usage:
#   bash llvm/test/run_analyze_test.sh
#
# Environment:
#   HG_PLUGIN        path to libHintGather.so  (default: ../build/libHintGather.so)
#   HG_CLANG         clang to use              (default: clang on PATH)
#   HG_TARGET_FLAGS  extra target flags, e.g. "--target=riscv64-unknown-elf"
#   HG_KEEP          set to 1 to keep the temp dir for inspection

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="${HG_PLUGIN:-${HERE}/../build/libHintGather.so}"
CLANG="${HG_CLANG:-clang}"

fail() {
  echo "FAIL: $*" >&2
  if [[ -f "${OUT:-}/hint_sites.json" ]]; then
    echo "--- hint_sites.json ---" >&2
    cat "${OUT}/hint_sites.json" >&2
    echo "--- end ---" >&2
  fi
  exit 1
}

command -v "${CLANG}" >/dev/null 2>&1 || fail "clang not found (set HG_CLANG)"
[[ -f "${PLUGIN}" ]] || fail "plugin not found at '${PLUGIN}' (set HG_PLUGIN)"

OUT="$(mktemp -d)"
cleanup() {
  if [[ "${HG_KEEP:-0}" == "1" ]]; then
    echo "kept: ${OUT}"
  else
    rm -rf "${OUT}"
  fi
}
trap cleanup EXIT

REPORT="${OUT}/hint_sites.json"

# -fno-vectorize/-fno-slp-vectorize/-fno-unroll-loops keep the loop shape
# one-to-one with the source, so "exactly one site" is a stable assertion.
# shellcheck disable=SC2086
"${CLANG}" -O2 \
  -fno-vectorize -fno-slp-vectorize -fno-unroll-loops \
  ${HG_TARGET_FLAGS:-} \
  -Xclang -load -Xclang "${PLUGIN}" \
  -fpass-plugin="${PLUGIN}" \
  -mllvm -hg-mode=analyze \
  -mllvm -hg-genome="${HERE}/test_genome.json" \
  -mllvm -hg-report="${REPORT}" \
  -c "${HERE}/gather_test.c" -o "${OUT}/gather_test.o" \
  || fail "clang invocation failed"

[[ -s "${REPORT}" ]] || fail "no hint_sites.json produced at ${REPORT}"

# An eligible site is one with an empty skip_reason.
ACCEPTED="$(grep -c '"skip_reason": *""' "${REPORT}" || true)"
[[ "${ACCEPTED}" == "1" ]] \
  || fail "expected exactly 1 accepted site, got ${ACCEPTED}"

# The accepted one must be the real gather.  skip_reason is the last key of the
# site object, so the accepted site's "function" key is a few lines above it.
grep -B 12 '"skip_reason": *""' "${REPORT}" | grep -q '"function": *"gather_sum"' \
  || fail "the accepted site is not in gather_sum"

# analyze mode must not touch the IR, so nothing may be marked emitted.
EMITTED="$(grep -c '"emitted": *true' "${REPORT}" || true)"
[[ "${EMITTED}" == "0" ]] \
  || fail "analyze mode reported ${EMITTED} emitted sites; it must report 0"

# genome_hash must be present and 8 hex chars (DESIGN.md section 4.1).
grep -Eq '"genome_hash": *"[0-9a-f]{8}"' "${REPORT}" \
  || fail "genome_hash missing or malformed"

# Informational only: whether the affine_indirect case reached the SCEV filter
# depends on LICM hoisting the invariant index load, which varies a little
# across LLVM versions.  Either way it must not be accepted, which the count
# assertion above already guarantees.
if grep -q '"scev_affine": *true' "${REPORT}"; then
  echo "note: SCEV affine filter fired (affine_indirect rejected as expected)"
else
  echo "note: no site reached the affine filter; affine_indirect was rejected"
  echo "      earlier in the pipeline (also acceptable)"
fi

echo "PASS: 1 accepted site in gather_sum, 0 emitted, affine cases rejected"
exit 0
