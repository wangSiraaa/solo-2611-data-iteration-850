"""End-to-end tape-reuse demonstration.

Run:  python3 -m solo tapedemo

It builds four scenarios required by the task statement and writes
self-contained JSON artifacts under ``artifacts/tape/`` that the
independent reader ``tools/replay_order.py`` replays hole by hole
without importing anything from the ``solo`` package:

1. the statement's X=03 / Y=01 with blanks U=01 / V=02, including both
   "at least one new tape" variants;
2. a real revision: orders pinned at different versions of one original,
   with provenance walking the revision chain;
3. an order whose blank can only serve it flip-mounted;
4. two originals decoding to the same Unicode text but carrying different
   bytes, showing they are not interchangeable stock.

Re-stocking (a fresh set of blanks) re-plans without decoding any
version again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from .demo import build_small_demo
from .revision import RevisionLibrary, replace
from .tape import (
    MAX_BLANKS,
    MAX_ORDERS,
    MAX_PITCHES,
    MaterialPlanner,
    TapeBlank,
    TapeOrder,
    build_spectrum,
    holes_of_byte,
    plan_batch,
)

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "tape"
)
REPLAY_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "replay_order.py",
)


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _blanks_to_stock(blanks):
    return {"blanks": [{"blank_id": b.blank_id, "pitches_hex": b.pitches.hex()} for b in blanks]}


def _emit_scenario(name, orders, blanks, planner, log=print, extra_spectra=()):
    """Plan one scenario, write its spectra, stock, plan and machine orders."""
    scenario_dir = os.path.join(OUT_DIR, name)
    order_dir = os.path.join(scenario_dir, "machine_orders")
    spectrum_dir = os.path.join(scenario_dir, "spectra")
    os.makedirs(order_dir, exist_ok=True)
    os.makedirs(spectrum_dir, exist_ok=True)

    plan = plan_batch(orders, blanks)
    log(f"-- scenario {name} --")
    for line in plan.summary().splitlines():
        log("  " + line)

    _write(os.path.join(scenario_dir, "stock.json"), _blanks_to_stock(blanks))
    _write(os.path.join(scenario_dir, "plan.json"), plan.to_dict())

    emitted_spectra: set[int] = set()
    for machine in plan.machine_orders():
        machine.dump(os.path.join(order_dir, f"order_{machine.order_id}.json"))
        if machine.spectrum.version_id not in emitted_spectra:
            machine.spectrum.dump(
                os.path.join(
                    spectrum_dir, f"spectrum_order_{machine.order_id}.json"
                )
            )
            emitted_spectra.add(machine.spectrum.version_id)
    for spectrum in extra_spectra:
        if spectrum.version_id not in emitted_spectra:
            spectrum.dump(
                os.path.join(spectrum_dir, f"spectrum_v{spectrum.version_id}.json")
            )
    return plan

def _run_independent_replay(scenario_dir, log=print):
    """Invoke the zero-dependency reader as a separate process."""
    cmd = [sys.executable, REPLAY_TOOL, "replay-batch", scenario_dir]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        log("  [replay] " + line)
    if proc.returncode != 0:
        for line in proc.stderr.splitlines():
            log("  [replay] " + line)
        raise RuntimeError(f"independent replay failed for {scenario_dir}")


def demo(log=print):
    os.makedirs(OUT_DIR, exist_ok=True)
    planner = MaterialPlanner(RevisionLibrary())

    # ------------------------------------------------------------------
    # Scenario 1: the statement example and its two new-tape variants.
    # ------------------------------------------------------------------
    log("== scenario 1: statement example X=03 Y=01, blanks U=01 V=02 ==")
    lib_single = RevisionLibrary()
    spec_x = build_spectrum(lib_single, lib_single.add_original(bytes((0x03,))).version_id)
    lib_single2 = RevisionLibrary()
    spec_y = build_spectrum(lib_single2, lib_single2.add_original(bytes((0x01,))).version_id)
    lib_single3 = RevisionLibrary()
    spec_x0 = build_spectrum(lib_single3, lib_single3.add_original(bytes((0x00,))).version_id)

    U = TapeBlank("U", bytes((0x01,)))
    V = TapeBlank("V", bytes((0x02,)))
    orders = [TapeOrder("X", spec_x), TapeOrder("Y", spec_y)]
    plan = _emit_scenario("1_statement", orders, [U, V], planner)
    assert plan.objective == (0, 1, 0)
    assert plan.allocation_for("X").blank.blank_id == "V"
    assert plan.allocation_for("Y").blank.blank_id == "U"

    plan_x0 = _emit_scenario(
        "1a_X_is_00", [TapeOrder("X", spec_x0), TapeOrder("Y", spec_y)], [U, V], planner
    )
    assert plan_x0.new_tape_count >= 1
    plan_no_v = _emit_scenario(
        "1b_without_V", [TapeOrder("X", spec_x), TapeOrder("Y", spec_y)], [U], planner
    )
    assert plan_no_v.new_tape_count >= 1
    _run_independent_replay(os.path.join(OUT_DIR, "1_statement"), log)

    # ------------------------------------------------------------------
    # Scenario 2: a real revision chain, orders pinned at versions.
    # ------------------------------------------------------------------
    log("== scenario 2: real revisions, provenance across the chain ==")
    rev_lib = RevisionLibrary()
    small = build_small_demo(rev_lib)
    # A third version with an extra replacement so every origin kind exists.
    v3 = rev_lib.apply(small["versions"][1].version_id, [replace(0, 1, "Z")])
    rev_planner = MaterialPlanner(rev_lib)
    spec_v2 = rev_planner.spectrum(2)
    spec_v3 = rev_planner.spectrum(3)
    assert rev_planner.spectrum(2) is spec_v2  # cached: re-stocking never re-decodes

    # Blanks: one blank matches the leading 'Z' of v3 exactly; another spans
    # the whole v2 target with a subset of its holes (some pitches kept,
    # some punches required, plus spare pitches that get scrapped).
    z_blank = TapeBlank("Z1", bytes((0x5A,)) + bytes(3))  # 'Z' in the first pitch
    # v2 starts with "* DRAFT..." -> bytes 2a 20 44 52 41 46 54 ...; build a
    # blank as long as the target plus a two-pitch scrap tail, keeping only a
    # hole subset at each pitch so mounting stays feasible.
    partial = bytearray(spec_v2.length + 2)
    for pitch, wanted in enumerate(spec_v2.body):
        partial[pitch] = wanted & 0x0F  # low four channels may already be punched
    long_blank = TapeBlank("L1", bytes(partial))
    orders_rev = [
        TapeOrder("R-v3", spec_v3),
        TapeOrder("R-v2", spec_v2, allow_reverse=True),
    ]
    plan_rev = _emit_scenario(
        "2_revision", orders_rev, [z_blank, long_blank], rev_planner,
        extra_spectra=(rev_planner.spectrum(0), rev_planner.spectrum(1)),
    )
    assert plan_rev.new_tape_count == 1
    assert plan_rev.allocation_for("R-v3").uses_new_tape
    v2_alloc = plan_rev.allocation_for("R-v2")
    assert not v2_alloc.uses_new_tape and v2_alloc.placement.added_holes > 0
    # A kept byte walks the whole revision chain back to the v0 original.
    multi_hop = next(p for p, origin in enumerate(spec_v2.origins) if len(origin.via) == 2)
    log(f"  provenance of v2 kept pitch {multi_hop} (multi-hop):")
    for hop in spec_v2.trace(multi_hop):
        log(f"    {hop.source}: {hop.detail}")
    # Every origin kind is exercised somewhere in the chain.
    spec_v1 = rev_planner.spectrum(1)
    boundary_pitch = next(
        p for p, origin in enumerate(spec_v1.origins) if origin.kind == "boundary"
    )
    log(f"  boundary origin at v1 pitch {boundary_pitch}: "
        f"{spec_v1.origin_at(boundary_pitch).describe()}")
    new_text_pitch = next(
        p for p, origin in enumerate(spec_v2.origins) if origin.kind == "new_text"
    )
    log(f"  new-text origin at v2 pitch {new_text_pitch}: "
        f"{spec_v2.origin_at(new_text_pitch).describe()}")
    original_pitch = next(
        p for p, origin in enumerate(spec_v2.origins) if origin.kind == "original"
    )
    log(f"  original origin at v2 pitch {original_pitch}: "
        f"{spec_v2.origin_at(original_pitch).describe()}")
    _run_independent_replay(os.path.join(OUT_DIR, "2_revision"), log)

    # ------------------------------------------------------------------
    # Scenario 3: only flip mounting is usable.
    # ------------------------------------------------------------------
    log("== scenario 3: reverse mounting is the only option ==")
    lib_r = RevisionLibrary()
    spec_r = build_spectrum(
        lib_r, lib_r.add_original(bytes((0x03, 0x03))).version_id
    )
    # Blank holes 40 80: forward view never fits (0x40 has channel 6 the
    # target 03 lacks); flipped view is 01 02, a subset of 03 03.
    flip_blank = TapeBlank("F1", bytes((0x40, 0x80)))
    forward_only = plan_batch([TapeOrder("R", spec_r, allow_reverse=False)], [flip_blank])
    assert forward_only.new_tape_count == 1
    plan_flip = _emit_scenario(
        "3_reverse_only",
        [TapeOrder("R", spec_r, allow_reverse=True)],
        [flip_blank],
        planner,
    )
    placement = plan_flip.allocation_for("R").placement
    assert placement.orientation == "reverse"
    log(f"  flip placement punches={list(placement.punches)} "
        f"existing={list(placement.existing_holes)}")
    _run_independent_replay(os.path.join(OUT_DIR, "3_reverse_only"), log)

    # ------------------------------------------------------------------
    # Scenario 4: identical decoded text, different original bytes.
    # ------------------------------------------------------------------
    log("== scenario 4: same Unicode text, different pinned bytes ==")
    lib_a = RevisionLibrary()
    spec_plain = build_spectrum(
        lib_a, lib_a.add_original(bytes((0x41,))).version_id
    )
    lib_b = RevisionLibrary()
    spec_noop = build_spectrum(
        lib_b, lib_b.add_original(bytes((0xFF, 0x00, 0x41))).version_id
    )
    assert spec_plain.text == spec_noop.text == "A"
    assert spec_plain.body != spec_noop.body

    # P-blank is exactly one pitch with 'A': serves the plain version with
    # zero punches and cannot serve the three-pitch no-op version at all.
    p_blank = TapeBlank("P1", bytes((0x41,)))
    # N-blank is exactly the no-op body: serves the no-op version with zero
    # punches; the plain order taking it would scrap two pitches.
    n_blank = TapeBlank("N1", bytes((0xFF, 0x00, 0x41)))
    orders_text = [
        TapeOrder("T-plain", spec_plain),
        TapeOrder("T-noop", spec_noop, allow_reverse=True),
    ]
    plan_text = _emit_scenario("4_same_text", orders_text, [p_blank, n_blank], planner)
    alloc_plain = plan_text.allocation_for("T-plain")
    alloc_noop = plan_text.allocation_for("T-noop")
    assert alloc_plain.blank.blank_id == "P1"
    assert alloc_noop.blank.blank_id == "N1"
    assert alloc_plain.placement.added_holes == 0
    assert alloc_noop.placement.added_holes == 0
    # The decisive non-interchangeability evidence: the noop version's first
    # pitch demands all eight channels; no pitch of the plain one-pitch blank
    # can present that subset, and the plain target can never consume a
    # three-pitch blank without scrap.  Cross-assignment would lose on
    # objective 1 (an order forced onto a new tape) or objective 3.
    crossed = plan_batch(
        [TapeOrder("T-plain", spec_plain)], [TapeBlank("N1-only", bytes((0xFF, 0x00, 0x41)))]
    )
    assert crossed.objective[0] == 0 and crossed.allocation_for("T-plain").scrap == 2
    impossible = plan_batch(
        [TapeOrder("T-noop", spec_noop, True)], [TapeBlank("P1-only", bytes((0x41,)))]
    )
    assert impossible.new_tape_count == 1
    log("  same text 'A': plain bytes 41 vs noop bytes ff0041; blanks are not interchangeable")
    _run_independent_replay(os.path.join(OUT_DIR, "4_same_text"), log)

    # ------------------------------------------------------------------
    # Re-stock: same cached orders, a fresh blank batch, no re-decode.
    # ------------------------------------------------------------------
    log("== re-stocking: replan with fresh blanks, spectra stay cached ==")
    cached_before = len(rev_planner._cache)
    fresh = [TapeBlank("FRESH1", bytes((0x00, 0x5A))), TapeBlank("FRESH2", bytes((0x01,)))]
    restocked = plan_batch(orders_rev, fresh)
    assert len(rev_planner._cache) == cached_before
    log("  " + restocked.summary().replace("\n", "\n  "))
    _write(os.path.join(OUT_DIR, "restocked_plan.json"), restocked.to_dict())

    # Limits are enforced at the batch boundary.
    for bad_orders, bad_blanks, label in (
        ([TapeOrder(f"O{i}", spec_plain) for i in range(MAX_ORDERS + 1)], [], "orders"),
        ([TapeOrder("O0", spec_plain)], [TapeBlank(f"B{i}", b"\x00") for i in range(MAX_BLANKS + 1)], "blanks"),
    ):
        try:
            plan_batch(bad_orders, bad_blanks)
        except Exception as exc:
            log(f"  limit enforced ({label}): {type(exc).__name__}")
        else:
            raise AssertionError(f"{label} limit not enforced")
    try:
        TapeBlank("too-long", bytes(MAX_PITCHES + 1))
    except Exception as exc:
        log(f"  limit enforced (pitches): {type(exc).__name__}")

    log(f"== artifacts and replayable machine orders written under {OUT_DIR} ==")
    return OUT_DIR


def main():
    demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
