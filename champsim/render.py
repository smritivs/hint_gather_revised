#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Renders the HINT.GATHER ChampSim prefetcher template.

ROLE
    Dependency-free (stdlib only) substitution of the literal ``@@KEY@@``
    placeholders in ``champsim/hint_gather_prefetcher.h.in`` with values taken
    from a genome (docs/DESIGN.md sec 3) and a hint-site list (docs/DESIGN.md
    sec 4.1 ``hint_sites.json``).  The result is a header-only ChampSim
    prefetcher module that CHIA's
    ``ChampSimNode.build_champsim(champsim_root, prefetcher_src, module_name,
    cache_level=...)`` writes to
    ``<champsim_root>/prefetcher/<module_name>/<module_name>.h``.

CONTRACT (imported by the CHIA loop -- do not change the signature)
    render(template_path, genome: dict, hint_sites: list[dict]) -> str

    * Every placeholder named in docs/DESIGN.md sec 4.3 is substituted.
    * Any ``@@KEY@@`` in the template that is not a known placeholder raises
      ``UnknownPlaceholderError``.
    * Any placeholder left unsubstituted in the output raises
      ``UnrenderedPlaceholderError``.
    * Out-of-domain genome values raise ``GenomeError``.
    There is deliberately no "best effort" mode: a mis-rendered module would
    silently model the wrong design point, which is far worse than a crash.

Normative spec: ../docs/DESIGN.md (sec 3 genome, sec 4.3 ChampSim contract).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# --- The placeholder set of docs/DESIGN.md sec 4.3 (and nothing else). ---------
PLACEHOLDERS = (
    "HINT_SITES",
    "HINT_DISTANCE",
    "FANOUT",
    "PHQ_ENTRIES",
    "DROPPABLE",
    "MSHR_PRESSURE_THRESHOLD",
    "PREFETCH_LEVEL",
    "VARIANT",
)

# Genome keys this node consumes, with their domains (docs/DESIGN.md sec 3).
_VARIANTS = ("value", "chase")
_LEVELS = ("L1D", "L2C")
_ELEM_SIZES = (1, 2, 4, 8)

_PLACEHOLDER_RE = re.compile(r"@@([A-Za-z0-9_]+)@@")


class RenderError(Exception):
    """Base class for every failure mode of this module."""


class UnknownPlaceholderError(RenderError):
    """The template contains a @@KEY@@ that this renderer does not define."""


class UnrenderedPlaceholderError(RenderError):
    """A @@KEY@@ survived substitution (should be impossible; a bug guard)."""


class GenomeError(RenderError):
    """A genome value is missing or outside the domain of docs/DESIGN.md sec 3."""


class HintSiteError(RenderError):
    """A hint-site record is missing a field or has an impossible value."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _as_int(value, what):
    """int(value) accepting ints and decimal/hex strings ('0x...')."""
    if isinstance(value, bool):
        raise GenomeError("%s: expected an integer, got a bool" % what)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        except ValueError:
            raise GenomeError("%s: cannot parse %r as an integer" % (what, value))
    raise GenomeError("%s: cannot parse %r as an integer" % (what, value))


def _require(genome, key):
    if key not in genome:
        raise GenomeError(
            "genome is missing required key %r (docs/DESIGN.md sec 3)" % key
        )
    return genome[key]


def _check_int_range(value, key, low, high):
    value = _as_int(value, "genome[%r]" % key)
    if not low <= value <= high:
        raise GenomeError(
            "genome[%r] = %d is outside the documented domain %d..%d"
            % (key, value, low, high)
        )
    return value


def _c_bool(value, key):
    if not isinstance(value, bool):
        raise GenomeError("genome[%r] must be a JSON bool, got %r" % (key, value))
    return "true" if value else "false"


def _c_double(value, key):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenomeError("genome[%r] must be a number, got %r" % (key, value))
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise GenomeError(
            "genome[%r] = %r is outside 0.0..1.0 (docs/DESIGN.md sec 3)" % (key, value)
        )
    # Always emit a decimal point so the C++ literal is a double.
    return repr(value) if "." in repr(value) or "e" in repr(value) else "%.1f" % value


def _c_uint64(value):
    return "0x%016xull" % (value & 0xFFFFFFFFFFFFFFFF)


# ---------------------------------------------------------------------------
# hint sites
# ---------------------------------------------------------------------------
def _site_pc(site, index):
    """PC of the demand load, resolved by the CHIA loop from ``pc_symbol``."""
    for key in ("pc", "pc_value", "pc_addr", "address"):
        if site.get(key) is not None:
            return _as_int(site[key], "hint_sites[%d][%r]" % (index, key))
    raise HintSiteError(
        "hint_sites[%d] has no resolved PC; the ChampSim node must resolve "
        "%r (docs/DESIGN.md sec 4.1) to an address with nm/objdump before "
        "rendering" % (index, site.get("pc_symbol", "<pc_symbol missing>"))
    )


def _format_hint_sites(hint_sites):
    """Builds the C++ initializers for `kSiteTable`.

    Each emitted line is a comma-terminated aggregate initializer
    ``{pc, base_reg_hint, elem_size, stride_bytes},`` -- the template appends
    an all-zero sentinel after them, so an empty site list is legal and yields
    a table with the sentinel only.
    """
    if hint_sites is None:
        hint_sites = []
    if isinstance(hint_sites, dict):  # tolerate a whole hint_sites.json blob
        hint_sites = hint_sites.get("sites", [])
    if not isinstance(hint_sites, (list, tuple)):
        raise HintSiteError("hint_sites must be a list of dicts")

    lines = []
    for index, site in enumerate(hint_sites):
        if not isinstance(site, dict):
            raise HintSiteError("hint_sites[%d] is not a dict" % index)
        # 'emitted': false means the pass considered but rejected the site.
        if site.get("emitted") is False:
            continue

        pc = _site_pc(site, index)
        elem_size = _as_int(
            site.get("elem_size", 8), "hint_sites[%d]['elem_size']" % index
        )
        if elem_size not in _ELEM_SIZES:
            raise HintSiteError(
                "hint_sites[%d]['elem_size'] = %d must be one of %s "
                "(the instruction encodes 1<<SHIFT, docs/DESIGN.md sec 1.1)"
                % (index, elem_size, list(_ELEM_SIZES))
            )
        stride = _as_int(
            site.get("stride_bytes", elem_size),
            "hint_sites[%d]['stride_bytes']" % index,
        )
        base = site.get("base_reg_hint", site.get("base_addr", 0))
        base = _as_int(base if base is not None else 0,
                       "hint_sites[%d]['base_reg_hint']" % index)
        if pc == 0:
            raise HintSiteError(
                "hint_sites[%d] resolved to PC 0, which the template uses as "
                "the table terminator" % index
            )

        comment = site.get("source") or site.get("function") or ""
        suffix = "  // site %s %s" % (site.get("site_id", index), comment)
        lines.append(
            "    {%s, %s, %du, %d},%s"
            % (_c_uint64(pc), _c_uint64(base), elem_size, stride, suffix.rstrip())
        )

    if not lines:
        return "    // (no hint sites: the model degenerates to a no-op)\n"
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------
def build_substitutions(genome, hint_sites):
    """Maps the genome + hint sites onto the sec 4.3 placeholder values."""
    if not isinstance(genome, dict):
        raise GenomeError("genome must be a dict (docs/DESIGN.md sec 3)")

    variant = _require(genome, "variant")
    if variant not in _VARIANTS:
        raise GenomeError(
            "genome['variant'] = %r must be one of %s" % (variant, list(_VARIANTS))
        )
    level = _require(genome, "prefetch_level")
    if level not in _LEVELS:
        raise GenomeError(
            "genome['prefetch_level'] = %r must be one of %s"
            % (level, list(_LEVELS))
        )

    return {
        "HINT_SITES": _format_hint_sites(hint_sites),
        "HINT_DISTANCE": str(
            _check_int_range(_require(genome, "hint_distance"), "hint_distance", 1, 512)
        ),
        "FANOUT": str(_check_int_range(_require(genome, "fanout"), "fanout", 1, 8)),
        "PHQ_ENTRIES": str(
            _check_int_range(_require(genome, "phq_entries"), "phq_entries", 2, 32)
        ),
        "DROPPABLE": _c_bool(_require(genome, "droppable"), "droppable"),
        "MSHR_PRESSURE_THRESHOLD": _c_double(
            _require(genome, "mshr_pressure_threshold"), "mshr_pressure_threshold"
        ),
        # Pasted verbatim: the template turns them into HG_LEVEL_<x> /
        # HG_VARIANT_<x>, so a typo becomes a compile error, not a silent
        # mis-model.
        "PREFETCH_LEVEL": level,
        "VARIANT": variant,
    }


def render(template_path, genome, hint_sites):
    """Renders `template_path` for `genome` + `hint_sites` and returns C++.

    Args:
      template_path: path to champsim/hint_gather_prefetcher.h.in.
      genome: dict following docs/DESIGN.md sec 3 (extra keys are ignored; they
        belong to the LLVM and gem5 nodes).
      hint_sites: list of dicts from hint_sites.json['sites'] with the PC
        already resolved (see `_site_pc`).

    Returns:
      The rendered, ready-to-compile header as a str.

    Raises:
      UnknownPlaceholderError, UnrenderedPlaceholderError, GenomeError,
      HintSiteError.
    """
    with open(template_path, "r") as handle:
        template = handle.read()

    found = set(_PLACEHOLDER_RE.findall(template))
    unknown = sorted(found - set(PLACEHOLDERS))
    if unknown:
        raise UnknownPlaceholderError(
            "template %s uses placeholder(s) %s which are not defined by "
            "docs/DESIGN.md sec 4.3 (known: %s)"
            % (template_path, unknown, list(PLACEHOLDERS))
        )

    values = build_substitutions(genome, hint_sites)
    rendered = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)

    leftover = sorted(set(_PLACEHOLDER_RE.findall(rendered)))
    if leftover:
        raise UnrenderedPlaceholderError(
            "placeholder(s) %s survived rendering of %s" % (leftover, template_path)
        )
    return rendered


def render_to_file(template_path, genome, hint_sites, out_path):
    """Convenience wrapper: render() then write, returning the text."""
    text = render(template_path, genome, hint_sites)
    with open(out_path, "w") as handle:
        handle.write(text)
    return text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_json(path):
    with open(path, "r") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the HINT.GATHER ChampSim prefetcher module."
    )
    parser.add_argument(
        "--template",
        default="hint_gather_prefetcher.h.in",
        help="path to hint_gather_prefetcher.h.in",
    )
    parser.add_argument("--genome", required=True, help="genome.json (sec 3)")
    parser.add_argument(
        "--hint-sites",
        default=None,
        help="hint_sites.json (sec 4.1); omitted => no sites, model is a no-op",
    )
    parser.add_argument(
        "--out", default=None, help="output header; default is stdout"
    )
    args = parser.parse_args(argv)

    genome = _load_json(args.genome)
    sites = _load_json(args.hint_sites) if args.hint_sites else []
    if isinstance(sites, dict):
        sites = sites.get("sites", [])

    try:
        text = render(args.template, genome, sites)
    except RenderError as error:
        sys.stderr.write("render.py: %s\n" % error)
        return 1

    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
