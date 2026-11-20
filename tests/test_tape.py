"""Tests for hole spectra, material planning and punch orders.

Run with:  python3 -m unittest discover -s tests -v

The optimiser is checked against an independent exhaustive search over
every order/blank assignment (small instances only), and every machine
order is replayed by the dependency-free reader in ``tools/`` as a
subprocess, proving the JSON alone is enough to rebuild the physical tape.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

from solo.codec import MODE_PAIR, MODE_SINGLE
from solo.demo import build_small_demo
from solo.revision import RevisionLibrary, replace
from solo.tape import (
    MAX_BLANKS,
    MAX_ORDERS,
    MAX_PITCHES,
    ORIENT_REVERSE,
    ORDER_FORMAT,
    SPECTRUM_FORMAT,
    Allocation,
    BatchPlan,
    HoleSpectrum,
    MachineOrder,
    MaterialPlanner,
    Segment,
    TapeBlank,
    TapeError,
    TapeOrder,
    build_spectrum,
    byte_of_holes,
    enumerate_placements,
    holes_of_byte,
    holes_string,
    plan_batch,
    reverse_byte,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAY_TOOL = os.path.join(ROOT, "tools", "replay_order.py")


# ---------------------------------------------------------------------------
# Helpers


def spectrum_of(body: bytes) -> HoleSpectrum:
    lib = RevisionLibrary()
    return build_spectrum(lib, lib.add_original(body).version_id)


def legal_body(rnd: random.Random, length: int) -> bytes:
    """Random but always legal SOLO body (SINGLE ASCII plus valid switches)."""
    out = bytearray()
    while len(out) < length:
        room = length - len(out)
        choice = rnd.random()
        if room < 6 or choice < 0.7:
            out.append(rnd.randrange(0x80))  # legal SINGLE byte
        elif choice < 0.85:
            out.extend((0xFF, 0x00))          # legal no-op switch
        else:
            value = rnd.randrange(0x1000)     # PAIR char between full switches
            out.extend((0xFF, 0x01, value >> 8, value & 0xFF, 0xFF, 0x00))
    return bytes(out[:length])


def brute_force_objective(orders, blanks):
    """Exhaustive optimum of (new tapes, punches, scrap) for small batches."""
    best = None

    def search(order_index, used, new_count, punches, scrap):
        nonlocal best
        if best is not None and (new_count, punches, scrap) >= best:
            return
        if order_index == len(orders):
            best = (new_count, punches, scrap)
            return
        order = orders[order_index]
        target_holes = sum(bin(b).count("1") for b in order.spectrum.body)
        # Option: draw a brand-new tape.
        search(
            order_index + 1,
            used,
            new_count + 1,
            punches + target_holes,
            scrap,
        )
        # Option: assign any unused blank at its best feasible placement.
        for j, blank in enumerate(blanks):
            if j in used or blank.length < order.length:
                continue
            placements = enumerate_placements(blank, order)
            if not placements:
                continue
            chosen = placements[0]  # already sorted by (punches, scrap, ...)
            search(
                order_index + 1,
                used | {j},
                new_count,
                punches + chosen.added_holes,
                scrap + chosen.scrap,
            )

    search(0, frozenset(), 0, 0, 0)
    return best


def replay_subprocess(order_path, spectrum_path=None, stock_path=None):
    cmd = [sys.executable, REPLAY_TOOL, "replay", order_path]
    if spectrum_path:
        cmd += ["--spectrum", spectrum_path]
    if stock_path:
        cmd += ["--stock", stock_path]
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Bit / channel geometry


class GeometryTests(unittest.TestCase):
    def test_holes_round_trip(self):
        for value in range(256):
            self.assertEqual(byte_of_holes(holes_of_byte(value)), value)
        self.assertEqual(holes_of_byte(0), ())
        self.assertEqual(holes_of_byte(1), (0,))
        self.assertEqual(holes_of_byte(0x80), (7,))
        self.assertEqual(holes_of_byte(0xFF), tuple(range(8)))

    def test_holes_string(self):
        self.assertEqual(holes_string(0x01), "00000001")
        self.assertEqual(holes_string(0xA5), "10100101")

    def test_reverse_byte(self):
        self.assertEqual(reverse_byte(0x01), 0x80)
        self.assertEqual(reverse_byte(0x80), 0x01)
        self.assertEqual(reverse_byte(0xFF), 0xFF)
        self.assertEqual(reverse_byte(0x00), 0x00)
        for value in (0x03, 0x5A, 0xA5, 0x40):
            self.assertEqual(reverse_byte(reverse_byte(value)), value)

    def test_invalid_channels_rejected(self):
        with self.assertRaises(TapeError):
            byte_of_holes([8])
        with self.assertRaises(TapeError):
            byte_of_holes([-1])
        with self.assertRaises(TapeError):
            byte_of_holes([0, 0])


# ---------------------------------------------------------------------------
# Spectrum segments and standalone (de)serialisation


class SpectrumTests(unittest.TestCase):
    BODY = (
        b"Ab"
        + bytes((0xFF, 0x01))
        + bytes((0x00, 0x05))
        + bytes((0xFF, 0x00))
        + b"z"
        + bytes((0xFF, 0x00))  # trailing no-op switch occupies real pitches
    )

    def setUp(self):
        self.spectrum = spectrum_of(self.BODY)

    def test_every_pitch_is_a_byte_and_hole_list(self):
        self.assertEqual(self.spectrum.length, len(self.BODY))
        for pitch, value in enumerate(self.BODY):
            self.assertEqual(self.spectrum.byte_at(pitch), value)
            self.assertEqual(self.spectrum.holes_at(pitch), holes_of_byte(value))

    def test_segments_partition_pitches(self):
        kinds = [s.kind for s in self.spectrum.segments]
        self.assertIn("switch", kinds)
        self.assertIn("char", kinds)
        covered = []
        for segment in self.spectrum.segments:
            covered.extend(range(segment.start, segment.end))
        self.assertEqual(sorted(covered), list(range(len(self.BODY))))
        # Both bytes of a two-byte switch point at the same switch segment.
        switch = next(s for s in self.spectrum.segments if s.kind == "switch")
        self.assertEqual(
            self.spectrum.segment_at(switch.start),
            self.spectrum.segment_at(switch.start + 1),
        )
        self.assertEqual(switch.raw, bytes((0xFF, 0x01)))
        self.assertEqual(switch.from_mode, MODE_SINGLE)
        self.assertEqual(switch.to_mode, MODE_PAIR)

    def test_lookup_pitch_identifies_inner_byte(self):
        switch = next(s for s in self.spectrum.segments if s.kind == "switch")
        record = self.spectrum.lookup_pitch(switch.start + 1)
        self.assertEqual(record["segment_inner_offset"], 1)
        self.assertEqual(record["byte"], 0x01)
        char_seg = next(s for s in self.spectrum.segments if s.kind == "char")
        record = self.spectrum.lookup_pitch(char_seg.start)
        self.assertEqual(record["segment_inner_offset"], 0)
        self.assertEqual(record["origin"].kind, "original")

    def test_visible_char_lookup(self):
        chars = [s for s in self.spectrum.segments if s.kind == "char"]
        pair = next(s for s in chars if s.mode == MODE_PAIR)
        self.assertEqual(pair.char, chr(0x4E05))
        self.assertEqual(pair.end - pair.start, 2)
        ascii_char = next(s for s in chars if s.mode == MODE_SINGLE)
        self.assertIn(ascii_char.char, "Abz")

    def test_standalone_round_trip_and_digest(self):
        data = self.spectrum.to_dict()
        self.assertEqual(data["format"], SPECTRUM_FORMAT)
        reloaded = HoleSpectrum.from_dict(data)
        self.assertEqual(reloaded.body, self.spectrum.body)
        self.assertEqual(reloaded.segments, self.spectrum.segments)
        self.assertEqual(reloaded.origins, self.spectrum.origins)
        self.assertEqual(reloaded.body_sha256, self.spectrum.body_sha256)

    def test_tampered_body_digest_rejected(self):
        data = self.spectrum.to_dict()
        data["pitches"][0]["byte"] = "ff"
        with self.assertRaises(TapeError):
            HoleSpectrum.from_dict(data)
        data = self.spectrum.to_dict()
        data["body_sha256"] = "00" * 32
        with self.assertRaises(TapeError):
            HoleSpectrum.from_dict(data)

    def test_tampered_hole_list_rejected(self):
        data = self.spectrum.to_dict()
        data["pitches"][0]["holes"] = [7]
        with self.assertRaises(TapeError):
            HoleSpectrum.from_dict(data)

    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spectrum.json")
            self.spectrum.dump(path)
            reloaded = HoleSpectrum.load(path)
            self.assertEqual(reloaded.body, self.spectrum.body)

    def test_control_chars_are_legal_spectra(self):
        for value in (0x00, 0x01, 0x02, 0x03, 0x7F):
            sp = spectrum_of(bytes((value,)))
            self.assertEqual(sp.length, 1)
            self.assertEqual(sp.byte_at(0), value)


# ---------------------------------------------------------------------------
# Provenance across a real revision chain


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.lib = RevisionLibrary()
        build_small_demo(self.lib)  # v0, v1, v2
        self.lib.apply(1, [replace(0, 1, "Z")])  # v3
        self.spectra = {vid: build_spectrum(self.lib, vid) for vid in range(4)}

    def test_original_terminal(self):
        sp0 = self.spectra[0]
        for pitch in range(sp0.length):
            origin = sp0.origin_at(pitch)
            self.assertEqual(origin.kind, "original")
            self.assertEqual((origin.version_id, origin.pitch), (0, pitch))
            self.assertEqual(origin.via, ())

    def test_kept_bytes_walk_the_chain(self):
        sp2 = self.spectra[2]
        multi = [p for p, o in enumerate(sp2.origins) if len(o.via) == 2]
        self.assertTrue(multi)
        pitch = multi[0]
        hops = sp2.trace(pitch)
        self.assertEqual([h.source for h in hops], ["kept", "kept", "original"])
        self.assertEqual(hops[-1].version_id, 0)
        # The terminal pitch is a genuine v0 byte, unchanged along the chain.
        terminal = hops[-1]
        self.assertEqual(
            self.lib.get(0).body[terminal.pitch], sp2.body[pitch]
        )

    def test_boundary_and_new_text_origins(self):
        sp1 = self.spectra[1]
        boundary = [p for p, o in enumerate(sp1.origins) if o.kind == "boundary"]
        new_text = [p for p, o in enumerate(sp1.origins) if o.kind == "new_text"]
        self.assertTrue(boundary and new_text)
        bp = boundary[0]
        origin = sp1.origin_at(bp)
        self.assertEqual(origin.batch_id, 0)
        self.assertIn(origin.role, ("splice", "boundary", "final"))
        self.assertEqual(
            sp1.body[bp],
            bytes((0xFF, 0x01 if origin.to_mode == MODE_PAIR else 0x00))[0],
        )
        np = new_text[0]
        nt_origin = sp1.origin_at(np)
        self.assertEqual(nt_origin.inserted_text, "DRAFT")
        self.assertIsInstance(nt_origin.inner_byte, int)

    def test_provenance_survives_standalone_load(self):
        sp3 = self.spectra[3]
        reloaded = HoleSpectrum.from_dict(sp3.to_dict())
        self.assertEqual(reloaded.origins, sp3.origins)
        for pitch in range(sp3.length):
            self.assertEqual(
                [h.source for h in reloaded.trace(pitch)],
                [h.source for h in sp3.trace(pitch)],
            )

    def test_every_pitch_accounted_for_each_version(self):
        for sp in self.spectra.values():
            self.assertEqual(len(sp.origins), sp.length)
            for origin in sp.origins:
                self.assertIn(origin.kind, ("original", "boundary", "new_text"))


# ---------------------------------------------------------------------------
# Placement feasibility and mounting geometry


class PlacementTests(unittest.TestCase):
    def test_forward_subset_and_punches(self):
        order = TapeOrder("O", spectrum_of(bytes((0x03,))))
        blank = TapeBlank("B", bytes((0x02, 0x00)))
        placements = enumerate_placements(blank, order)
        self.assertEqual(len(placements), 2)
        best = placements[0]
        self.assertEqual(best.window_start, 0)
        self.assertEqual(best.added_holes, 1)
        self.assertEqual(best.punches, ((0, 0),))
        self.assertEqual(best.existing_holes, ((0, 1),))
        self.assertEqual(best.scrap, 1)

    def test_conflicting_old_hole_rejects_window(self):
        order = TapeOrder("O", spectrum_of(bytes((0x00,))))
        blank = TapeBlank("B", bytes((0x01,)))
        self.assertEqual(enumerate_placements(blank, order), ())

    def test_blank_shorter_than_order(self):
        order = TapeOrder("O", spectrum_of(bytes((0x00, 0x00))))
        blank = TapeBlank("B", bytes((0x00,)))
        self.assertEqual(enumerate_placements(blank, order), ())

    def test_reverse_only_mounting(self):
        order = TapeOrder("O", spectrum_of(bytes((0x03, 0x03))), allow_reverse=True)
        blank = TapeBlank("B", bytes((0x40, 0x80)))
        self.assertEqual(
            enumerate_placements(
                blank, TapeOrder("O2", order.spectrum, allow_reverse=False)
            ),
            (),
        )
        placements = enumerate_placements(blank, order)
        self.assertEqual(len(placements), 1)
        placement = placements[0]
        self.assertEqual(placement.orientation, ORIENT_REVERSE)
        self.assertEqual(placement.added_holes, 2)
        self.assertEqual(set(placement.punches), {(0, 7), (1, 6)})
        self.assertEqual(set(placement.existing_holes), {(0, 6), (1, 7)})

    def test_reverse_reverses_pitch_and_channel_order(self):
        # Target 01 00 (legal SOLO control bytes).  Blank [00, 80]
        # flip-mounted presents [01, 00] -- the 0x80 hole moves from blank
        # pitch 1 channel 7 to target pitch 0 channel 0, proving both the
        # pitch-order and channel-order reversals in one move.
        order = TapeOrder("O", spectrum_of(bytes((0x01, 0x00))), allow_reverse=True)
        blank = TapeBlank("B", bytes((0x00, 0x80)))
        placement = enumerate_placements(blank, order)[0]
        self.assertEqual(placement.orientation, ORIENT_REVERSE)
        self.assertEqual(placement.added_holes, 0)
        self.assertEqual(placement.existing_holes, ((1, 7),))

    def test_machine_order_coordinate_mapping_forward_and_reverse(self):
        for allow_reverse in (False, True):
            spectrum = spectrum_of(bytes((0x03, 0x03)))
            order = TapeOrder("O", spectrum, allow_reverse=allow_reverse)
            blank = TapeBlank(
                "B", bytes((0x40, 0x80)) if allow_reverse else bytes((0x02, 0x02))
            )
            placement = enumerate_placements(blank, order)[0]
            machine = MachineOrder(
                "O", spectrum, allow_reverse, Allocation(order, blank, placement)
            )
            for bp, bc in placement.punches:
                loc = machine.locate_returned(bp, bc)
                self.assertIn(loc.target_channel, holes_of_byte(spectrum.body[loc.target_pitch]))
                self.assertIsInstance(loc.segment, Segment)
            with self.assertRaises(TapeError):
                machine.locate_returned(blank.length + 1, 0)
            with self.assertRaises(TapeError):
                machine.locate_returned(0, 8)


# ---------------------------------------------------------------------------
# Batch optimisation against an exhaustive search


class OptimiserTests(unittest.TestCase):
    def _random_instance(self, seed, order_count, blank_count):
        rnd = random.Random(seed)
        orders = []
        for i in range(order_count):
            length = rnd.randrange(1, 4)
            body = legal_body(rnd, length)
            orders.append(
                TapeOrder(
                    f"O{i}",
                    spectrum_of(body),
                    allow_reverse=(rnd.random() < 0.5),
                )
            )
        blanks = []
        for j in range(blank_count):
            length = rnd.randrange(1, 5)
            blanks.append(
                TapeBlank(
                    f"B{j}", bytes(rnd.randrange(256) for _ in range(length))
                )
            )
        return orders, blanks

    def test_matches_brute_force_on_random_small_instances(self):
        for seed in range(60):
            order_count = 1 + seed % 4
            blank_count = seed % 5
            orders, blanks = self._random_instance(seed, order_count, blank_count)
            plan = plan_batch(orders, blanks)
            expected = brute_force_objective(orders, blanks)
            self.assertEqual(
                plan.objective,
                expected,
                msg=f"seed={seed} plan={plan.objective} brute={expected}",
            )

    def test_statement_example_x03_y01(self):
        sX = spectrum_of(bytes((0x03,)))
        sY = spectrum_of(bytes((0x01,)))
        U = TapeBlank("U", bytes((0x01,)))
        V = TapeBlank("V", bytes((0x02,)))
        plan = plan_batch([TapeOrder("X", sX), TapeOrder("Y", sY)], [U, V])
        self.assertEqual(plan.objective, (0, 1, 0))
        self.assertEqual(plan.allocation_for("X").blank.blank_id, "V")
        self.assertEqual(plan.allocation_for("Y").blank.blank_id, "U")

    def test_statement_variant_x00_needs_new_tape(self):
        sX = spectrum_of(bytes((0x00,)))
        sY = spectrum_of(bytes((0x01,)))
        plan = plan_batch(
            [TapeOrder("X", sX), TapeOrder("Y", sY)],
            [TapeBlank("U", bytes((0x01,))), TapeBlank("V", bytes((0x02,)))],
        )
        self.assertGreaterEqual(plan.new_tape_count, 1)
        self.assertEqual(plan.new_tape_count, 1)
        self.assertTrue(plan.allocation_for("X").uses_new_tape)

    def test_statement_variant_without_v_needs_new_tape(self):
        sX = spectrum_of(bytes((0x03,)))
        sY = spectrum_of(bytes((0x01,)))
        plan = plan_batch(
            [TapeOrder("X", sX), TapeOrder("Y", sY)],
            [TapeBlank("U", bytes((0x01,)))],
        )
        self.assertEqual(plan.new_tape_count, 1)

    def test_priority_1_new_tapes_beats_punches(self):
        # One blank can serve either order with zero punches; without reuse
        # both orders would need new tapes, so reuse must win even if some
        # other assignment had even fewer punches on paper.
        sA = spectrum_of(bytes((0x01,)))
        sB = spectrum_of(bytes((0x01,)))
        blank = TapeBlank("B", bytes((0x01,)))
        plan = plan_batch([TapeOrder("A", sA), TapeOrder("B", sB)], [blank])
        self.assertEqual(plan.new_tape_count, 1)
        self.assertEqual(plan.reused_blank_count, 1)

    def test_priority_2_minimise_punches(self):
        # Blank B1 fits both orders but with different punch counts; the
        # optimiser must leave the better-fitting order for it when another
        # blank covers the remaining one.
        sX = spectrum_of(bytes((0x03,)))
        sY = spectrum_of(bytes((0x01,)))
        blank_u = TapeBlank("U", bytes((0x01,)))   # exact fit for Y, 1 punch for X
        blank_v = TapeBlank("V", bytes((0x02,)))   # 1 punch for X, impossible for Y
        plan = plan_batch([TapeOrder("X", sX), TapeOrder("Y", sY)], [blank_u, blank_v])
        self.assertEqual(plan.total_punches, 1)
        self.assertEqual(plan.allocation_for("X").blank.blank_id, "V")

    def test_priority_3_minimise_scrap(self):
        # Two blanks fit the order equally on punches; the shorter one wins.
        order = TapeOrder("O", spectrum_of(bytes((0x00,))))
        short_blank = TapeBlank("short", bytes((0x00,)))
        long_blank = TapeBlank("long", bytes((0x00, 0x00, 0x00)))
        plan = plan_batch([order], [short_blank, long_blank])
        self.assertEqual(plan.objective, (0, 0, 0))
        self.assertEqual(plan.allocation_for("O").blank.blank_id, "short")

    def test_each_blank_used_at_most_once(self):
        orders = [TapeOrder(f"O{i}", spectrum_of(bytes((0x00,)))) for i in range(3)]
        blanks = [TapeBlank("B", bytes((0x00,)))]
        plan = plan_batch(orders, blanks)
        reused = [a for a in plan.allocations if not a.uses_new_tape]
        self.assertEqual(len(reused), 1)
        self.assertEqual(plan.new_tape_count, 2)

    def test_duplicate_ids_and_limits_rejected(self):
        order = TapeOrder("O", spectrum_of(bytes((0x00,))))
        with self.assertRaises(TapeError):
            plan_batch([order, order], [])
        with self.assertRaises(TapeError):
            plan_batch([order], [TapeBlank("B", b"\x00"), TapeBlank("B", b"\x00")])
        too_many_orders = [
            TapeOrder(f"O{i}", spectrum_of(bytes((0x00,))))
            for i in range(MAX_ORDERS + 1)
        ]
        with self.assertRaises(TapeError):
            plan_batch(too_many_orders, [])
        too_many_blanks = [
            TapeBlank(f"B{i}", b"\x00") for i in range(MAX_BLANKS + 1)
        ]
        with self.assertRaises(TapeError):
            plan_batch([order], too_many_blanks)
        with self.assertRaises(TapeError):
            TapeBlank("long", bytes(MAX_PITCHES + 1))
        with self.assertRaises(TapeError):
            TapeBlank("empty", b"")


# ---------------------------------------------------------------------------
# Same text, different bytes


class SameTextDifferentBytesTests(unittest.TestCase):
    def test_not_interchangeable(self):
        plain = spectrum_of(bytes((0x41,)))
        noop = spectrum_of(bytes((0xFF, 0x00, 0x41)))
        self.assertEqual(plain.text, noop.text)
        self.assertNotEqual(plain.body, noop.body)

        p_blank = TapeBlank("P", bytes((0x41,)))
        n_blank = TapeBlank("N", bytes((0xFF, 0x00, 0x41)))
        plan = plan_batch(
            [
                TapeOrder("plain", plain),
                TapeOrder("noop", noop, allow_reverse=True),
            ],
            [p_blank, n_blank],
        )
        self.assertEqual(plan.allocation_for("plain").blank.blank_id, "P")
        self.assertEqual(plan.allocation_for("noop").blank.blank_id, "N")

        # The one-pitch blank can never serve the three-pitch order.
        impossible = plan_batch(
            [TapeOrder("noop", noop, True)], [TapeBlank("P2", bytes((0x41,)))]
        )
        self.assertEqual(impossible.new_tape_count, 1)
        # The ff pitch blocks the plain order whenever no spare window exists.
        crossed = plan_batch(
            [TapeOrder("plain", plain)],
            [TapeBlank("N2", bytes((0xFF, 0x00, 0x41)))],
        )
        self.assertEqual(crossed.allocation_for("plain").scrap, 2)


# ---------------------------------------------------------------------------
# Machine orders and the independent subprocess reader


class MachineOrderTests(unittest.TestCase):
    def _write_batch(self, tmp, plan, orders, blanks, include_spectrum=True):
        stock = {
            "blanks": [
                {"blank_id": b.blank_id, "pitches_hex": b.pitches.hex()} for b in blanks
            ]
        }
        with open(os.path.join(tmp, "stock.json"), "w", encoding="utf-8") as fh:
            json.dump(stock, fh)
        paths = []
        for machine in plan.machine_orders():
            order_path = os.path.join(tmp, f"order_{machine.order_id}.json")
            machine.dump(order_path)
            self.assertEqual(machine.to_dict()["format"], ORDER_FORMAT)
            spectrum_path = None
            if include_spectrum:
                spectrum_path = os.path.join(tmp, f"spectrum_{machine.order_id}.json")
                machine.spectrum.dump(spectrum_path)
            paths.append((order_path, spectrum_path))
        return paths

    def test_replay_old_blank_forward(self):
        spectrum = spectrum_of(bytes((0x03,)))
        order = TapeOrder("X", spectrum)
        blank = TapeBlank("V", bytes((0x02,)))
        plan = plan_batch([order], [blank])
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_batch(tmp, plan, [order], [blank])
            order_path, spectrum_path = paths[0]
            proc = replay_subprocess(
                order_path, spectrum_path, os.path.join(tmp, "stock.json")
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("replay ok", proc.stdout)
            self.assertIn("1 punches", proc.stdout)
            # Replay without stock still works from embedded existing holes.
            proc2 = replay_subprocess(order_path, spectrum_path)
            self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)

    def test_replay_new_tape(self):
        spectrum = spectrum_of(bytes((0x00, 0x01)))
        order = TapeOrder("N", spectrum)
        blank = TapeBlank("B", bytes((0x80, 0x80)))  # never a subset
        plan = plan_batch([order], [blank])
        self.assertTrue(plan.allocation_for("N").uses_new_tape)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_batch(tmp, plan, [order], [blank])
            order_path, spectrum_path = paths[0]
            proc = replay_subprocess(
                order_path, spectrum_path, os.path.join(tmp, "stock.json")
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("new blank tape", proc.stdout)

    def test_replay_reverse_order(self):
        spectrum = spectrum_of(bytes((0x03, 0x03)))
        order = TapeOrder("R", spectrum, allow_reverse=True)
        blank = TapeBlank("F", bytes((0x40, 0x80)))
        plan = plan_batch([order], [blank])
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_batch(tmp, plan, [order], [blank])
            order_path, spectrum_path = paths[0]
            proc = replay_subprocess(
                order_path, spectrum_path, os.path.join(tmp, "stock.json")
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("reverse", proc.stdout)
            self.assertIn("reused 2 existing", proc.stdout)

    def test_reader_rejects_tampered_order(self):
        spectrum = spectrum_of(bytes((0x03,)))
        order = TapeOrder("X", spectrum)
        plan = plan_batch([order], [TapeBlank("V", bytes((0x02,)))])
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_batch(tmp, plan, [order], [TapeBlank("V", bytes((0x02,)))])
            order_path, _ = paths[0]
            with open(order_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Add an illegal punch the target does not want.
            data["punches"].append([0, 7])
            tampered = os.path.join(tmp, "bad.json")
            with open(tampered, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            proc = replay_subprocess(tampered)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("REPLAY FAILURE", proc.stderr)

    def test_reader_rejects_spectrum_order_mismatch(self):
        s1 = spectrum_of(bytes((0x03,)))
        s2 = spectrum_of(bytes((0x01,)))
        order = TapeOrder("X", s1)
        plan = plan_batch([order], [])
        with tempfile.TemporaryDirectory() as tmp:
            order_path = os.path.join(tmp, "o.json")
            plan.machine_orders()[0].dump(order_path)
            spectrum_path = os.path.join(tmp, "s.json")
            s2.dump(spectrum_path)
            proc = replay_subprocess(order_path, spectrum_path)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("do not match", proc.stderr)

    def test_verify_spectrum_subcommand(self):
        spectrum = spectrum_of(
            b"Ab" + bytes((0xFF, 0x01)) + bytes((0x00, 0x05)) + bytes((0xFF, 0x00))
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            spectrum.dump(path)
            proc = subprocess.run(
                [sys.executable, REPLAY_TOOL, "verify-spectrum", path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("spectrum verified", proc.stdout)
            self.assertIn("switch", proc.stdout)


# ---------------------------------------------------------------------------
# Planner caching: re-stocking without re-decoding


class PlannerCachingTests(unittest.TestCase):
    def setUp(self):
        self.lib = RevisionLibrary()
        build_small_demo(self.lib)
        self.planner = MaterialPlanner(self.lib)

    def test_spectrum_is_cached(self):
        first = self.planner.spectrum(2)
        second = self.planner.spectrum(2)
        self.assertIs(first, second)

    def test_restock_replans_without_rebuild(self):
        order = self.planner.make_order("O", 1)
        first_body = self.planner.spectrum(1).body
        blanks_a = [TapeBlank("A", bytes((0x00,)))]
        plan_a = self.planner.plan([order], blanks_a)
        cached_ids = set(self.planner._cache)
        # A fresh stock batch whose window holes are a subset of the order
        # can reuse a blank, unlike the all-zero single-pitch blank above.
        subset = bytearray(len(first_body) + 2)
        subset[: len(first_body)] = first_body
        blanks_b = [
            TapeBlank("B", bytes(subset)),
            TapeBlank("C", bytes((0x7F, 0x7F))),
        ]
        plan_b = self.planner.plan([order], blanks_b)
        self.assertEqual(set(self.planner._cache), cached_ids)
        self.assertIsInstance(plan_a, BatchPlan)
        self.assertIsInstance(plan_b, BatchPlan)
        self.assertNotEqual(
            (plan_a.new_tape_count, plan_a.total_scrap),
            (plan_b.new_tape_count, plan_b.total_scrap),
        )
        self.assertEqual(plan_b.new_tape_count, 0)

    def test_order_construction_pins_version(self):
        order = self.planner.make_order("O", 0, allow_reverse=True)
        self.assertTrue(order.allow_reverse)
        self.assertEqual(order.spectrum.version_id, 0)
        self.assertEqual(order.spectrum.body, self.lib.get(0).body)


# ---------------------------------------------------------------------------
# Scale at the documented limits


class ScaleTests(unittest.TestCase):
    def test_max_size_batch(self):
        rnd = random.Random(7)
        orders = []
        for i in range(MAX_ORDERS):
            length = rnd.randrange(100, MAX_PITCHES + 1)
            body = legal_body(rnd, length)
            orders.append(
                TapeOrder(
                    f"O{i}",
                    spectrum_of(body),
                    allow_reverse=(i % 2 == 0),
                )
            )
        blanks = []
        for j in range(MAX_BLANKS):
            length = MAX_PITCHES
            blanks.append(
                TapeBlank(
                    f"B{j}", bytes(rnd.randrange(256) for _ in range(length))
                )
            )
        plan = plan_batch(orders, blanks)
        self.assertEqual(len(plan.allocations), MAX_ORDERS)
        # Replay one old and one new (if any) machine order through the reader.
        chosen = []
        for allocation in plan.allocations:
            if allocation.uses_new_tape and not any(a.uses_new_tape for a in chosen):
                chosen.append(allocation)
            elif not allocation.uses_new_tape and not any(
                not a.uses_new_tape for a in chosen
            ):
                chosen.append(allocation)
            if len(chosen) == 2:
                break
        with tempfile.TemporaryDirectory() as tmp:
            stock_path = os.path.join(tmp, "stock.json")
            with open(stock_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "blanks": [
                            {"blank_id": b.blank_id, "pitches_hex": b.pitches.hex()}
                            for b in blanks
                        ]
                    },
                    fh,
                )
            for allocation in chosen:
                machine = MachineOrder(
                    allocation.order.order_id,
                    allocation.order.spectrum,
                    allocation.order.allow_reverse,
                    allocation,
                )
                order_path = os.path.join(tmp, f"order_{machine.order_id}.json")
                spectrum_path = os.path.join(tmp, f"spectrum_{machine.order_id}.json")
                machine.dump(order_path)
                machine.spectrum.dump(spectrum_path)
                proc = replay_subprocess(order_path, spectrum_path, stock_path)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)


if __name__ == "__main__":
    unittest.main()
