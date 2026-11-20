#!/usr/bin/env python3
"""Independent tiny reader for SOLO hole spectra and punch orders.

This script deliberately imports nothing from the ``solo`` package and
nothing outside the Python standard library.  It is the "independent
small reader" that replays machine orders hole by hole, re-derives every
coordinate mapping from the JSON alone, and verifies the embedded
SHA-256 summary of the pinned original bytes.

Subcommands
-----------
verify-spectrum SPECTRUM.json
    Re-hash the carried body bytes, re-derive each pitch's hole list from
    the byte, check segment coverage and print the full pitch table with
    segment kind and provenance.

replay ORDER.json [--spectrum SPECTRUM.json] [--stock STOCK.json]
    Rebuild the physical tape pitch by pitch.  Existing holes must be a
    subset of the target holes at the mapped position; punches must only
    add missing holes; the finished bytes must equal the pinned version
    bytes.  With --spectrum (whose digest must match the order) every
    machine-returned coordinate is mapped back onto the version's
    character/switch segment and provenance record.

replay-batch DIR
    Replay every machine_orders/*.json under DIR using DIR/stock.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

SPECTRUM_FORMAT = "solo-hole-spectrum-v1"
ORDER_FORMAT = "solo-punch-order-v1"
MODE_NAMES = {0: "SINGLE", 1: "PAIR"}
_REVERSE_BYTE = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


class ReplayError(Exception):
    pass


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def holes_of_byte(value):
    return tuple(c for c in range(8) if value & (1 << c))


def byte_of_holes(channels):
    value = 0
    for c in channels:
        value |= 1 << c
    return value


# ---------------------------------------------------------------------------
# Spectrum verification


def verify_spectrum(path, log=print):
    data = _load(path)
    if data.get("format") != SPECTRUM_FORMAT:
        raise ReplayError(f"{path}: not a {SPECTRUM_FORMAT} file")
    pitches = data["pitches"]
    body = bytes(int(p["byte"], 16) for p in pitches)
    digest = hashlib.sha256(body).hexdigest()
    if digest != data["body_sha256"]:
        raise ReplayError(f"{path}: body sha256 mismatch -- original bytes altered")
    if len(body) != data["length"]:
        raise ReplayError(f"{path}: length field disagrees with body")
    for index, item in enumerate(pitches):
        if item["pitch"] != index:
            raise ReplayError(f"{path}: pitch {index} out of order")
        if tuple(item["holes"]) != holes_of_byte(body[index]):
            raise ReplayError(f"{path}: pitch {index} hole list disagrees with byte")

    segments = data["segments"]
    covered = [False] * len(body)
    for segment in segments:
        for p in range(segment["start"], segment["end"]):
            if covered[p]:
                raise ReplayError(f"{path}: pitch {p} in two segments")
            covered[p] = True
    if body and not all(covered):
        raise ReplayError(f"{path}: segments do not cover every pitch")
    pitch_segment = data["pitch_segment"]
    if any(not 0 <= i < len(segments) for i in pitch_segment):
        raise ReplayError(f"{path}: pitch-to-segment table out of range")

    log(f"spectrum {os.path.basename(path)}: version v{data['version_id']} "
        f"pitches={len(body)} sha256={digest[:16]}... text={data['text']!r}")
    for index, item in enumerate(pitches):
        segment = segments[pitch_segment[index]]
        origin = item["origin"]
        hole_text = "".join(str(c) for c in item["holes"]) or "-"
        if segment["kind"] == "char":
            where = (f"char[{segment['char_index']}] {segment['char']!r} "
                     f"inner+{index - segment['start']} {MODE_NAMES[segment['mode']]}")
        else:
            where = (f"switch {MODE_NAMES[segment['from_mode']]}->"
                     f"{MODE_NAMES[segment['to_mode']]} inner+{index - segment['start']}")
        log(f"  pitch {index:4d} byte {item['byte']} holes[{hole_text}] {where} "
            f"origin={origin['t']}:v{origin['v']}@{origin['p']}")
    log(f"spectrum verified: {len(body)} pitches, {len(segments)} segments, digest ok")
    return data


# ---------------------------------------------------------------------------
# Order replay


def _load_stock(path):
    if path is None:
        return {}
    data = _load(path)
    return {b["blank_id"]: bytes.fromhex(b["pitches_hex"]) for b in data["blanks"]}


def _map_to_target(material, length, blank_pitch, blank_channel):
    """Invert the mounting geometry to get (target pitch, target channel)."""
    if material["type"] == "new":
        return blank_pitch, blank_channel
    window_start = material["window_start"]
    if material["orientation"] == "forward":
        return blank_pitch - window_start, blank_channel
    target_pitch = window_start + (length - 1) - blank_pitch
    return target_pitch, 7 - blank_channel


def _origin_describe(origin):
    kind = origin["t"]
    if kind == "original":
        return f"original byte of v{origin['v']} at pitch {origin['p']}"
    if kind == "boundary":
        return (f"boundary switch inserted in v{origin['v']} batch {origin['batch']} "
                f"at pitch {origin['p']} ({MODE_NAMES[origin['from_mode']]}->"
                f"{MODE_NAMES[origin['to_mode']]}, role={origin['role']})")
    return (f"new text encoded in v{origin['v']} batch {origin['batch']} at pitch "
            f"{origin['p']}: chars[{origin['old_char_start']},{origin['old_char_end']}) "
            f"text={origin['inserted_text']!r} inner byte {origin['inner_byte']}")


def replay_order(path, spectrum_path=None, stock_path=None, log=print):
    order = _load(path)
    if order.get("format") != ORDER_FORMAT:
        raise ReplayError(f"{path}: not a {ORDER_FORMAT} file")
    material = order["material"]
    length = order["length"]
    body = bytes.fromhex(order["body_hex"])
    digest = hashlib.sha256(body).hexdigest()
    if digest != order["body_sha256"]:
        raise ReplayError(f"{path}: embedded body digest does not match body_hex")
    if len(body) != length:
        raise ReplayError(f"{path}: length disagrees with body")

    spectrum = _load(spectrum_path) if spectrum_path else None
    if spectrum is not None:
        if spectrum.get("format") != SPECTRUM_FORMAT:
            raise ReplayError(f"{spectrum_path}: not a spectrum file")
        spec_body = bytes(int(p["byte"], 16) for p in spectrum["pitches"])
        if hashlib.sha256(spec_body).hexdigest() != order["body_sha256"] or spec_body != body:
            raise ReplayError(
                f"{path}: spectrum digest/bytes do not match the order's pinned version"
            )

    stock = _load_stock(stock_path)
    target_holes = {p: set(channels) for p, channels in order["target_holes"]}
    for p in range(length):
        if target_holes.get(p, set()) != set(holes_of_byte(body[p])):
            raise ReplayError(f"{path}: target_holes disagree with pinned byte at pitch {p}")

    # Reconstruct the starting physical tape, blank coordinates.
    if material["type"] == "old":
        blank_id = material["blank_id"]
        if material["orientation"] not in ("forward", "reverse"):
            raise ReplayError(f"{path}: unknown orientation {material['orientation']}")
        window_end = material["window_end"]
        if window_end - material["window_start"] != length:
            raise ReplayError(f"{path}: mounted window length is not {length}")
        starting = [set() for _ in range(length)]
        if blank_id in stock:
            blank_bytes = stock[blank_id]
            if len(blank_bytes) != material["blank_length"]:
                raise ReplayError(
                    f"{path}: stock blank {blank_id} length "
                    f"{len(blank_bytes)} != order {material['blank_length']}"
                )
            for bp, value in enumerate(blank_bytes):
                if not (material["window_start"] <= bp < window_end):
                    continue
                for channel in holes_of_byte(value):
                    tp, tc = _map_to_target(material, length, bp, channel)
                    starting[tp].add(tc)
        else:
            if stock:
                raise ReplayError(f"{path}: blank {blank_id} missing from stock file")
            # No stock: the order itself carries the existing-hole evidence.
            for bp, bc in order["existing_holes"]:
                tp, tc = _map_to_target(material, length, bp, bc)
                starting[tp].add(tc)
    else:
        if order["existing_holes"]:
            raise ReplayError(f"{path}: new tape must carry no existing holes")
        starting = [set() for _ in range(length)]

    # Existing holes must be a subset of the target holes at each pitch.
    for pitch in range(length):
        extra = starting[pitch] - target_holes[pitch]
        if extra:
            raise ReplayError(
                f"{path}: existing holes {sorted(extra)} at target pitch {pitch} "
                f"are not a subset of target holes {sorted(target_holes[pitch])}"
            )

    # Punch card sanity: no punch lands on an existing hole.
    punched = [set() for _ in range(length)]
    log(f"order {order['order_id']} version v{order['version_id']} length={length} "
        f"material={material['type']}"
        + (f" blank={material['blank_id']} {material['orientation']} "
           f"window[{material['window_start']},{material['window_end']})"
           if material["type"] == "old" else ""))
    for bp, bc in order["punches"]:
        tp, tc = _map_to_target(material, length, bp, bc)
        if not (0 <= tp < length and 0 <= tc <= 7):
            raise ReplayError(f"{path}: punch ({bp},{bc}) maps outside the target")
        if tc in starting[tp]:
            raise ReplayError(
                f"{path}: punch at blank ({bp},{bc}) hits an existing hole"
            )
        if tc not in target_holes[tp]:
            raise ReplayError(
                f"{path}: punch at blank ({bp},{bc}) adds a hole the target does not want"
            )
        if tc in punched[tp]:
            raise ReplayError(f"{path}: duplicate punch mapped to target ({tp},{tc})")
        punched[tp].add(tc)
        note = ""
        if spectrum is not None:
            segment = spectrum["segments"][spectrum["pitch_segment"][tp]]
            origin = spectrum["pitches"][tp]["origin"]
            if segment["kind"] == "char":
                note = (f" -> char[{segment['char_index']}] {segment['char']!r} "
                        f"inner+{tp - segment['start']}; {_origin_describe(origin)}")
            else:
                note = (f" -> {MODE_NAMES[segment['from_mode']]}->"
                        f"{MODE_NAMES[segment['to_mode']]} switch inner+"
                        f"{tp - segment['start']}; {_origin_describe(origin)}")
        log(f"  punch blank(pitch={bp}, channel={bc}) -> target(pitch={tp}, "
            f"channel={tc}){note}")

    # Finished tape must equal the pinned version byte for byte.
    for pitch in range(length):
        finished = starting[pitch] | punched[pitch]
        if finished != target_holes[pitch]:
            raise ReplayError(
                f"{path}: finished pitch {pitch} holes {sorted(finished)} != "
                f"target {sorted(target_holes[pitch])}"
            )
        if byte_of_holes(finished) != body[pitch]:
            raise ReplayError(f"{path}: finished byte mismatch at pitch {pitch}")

    if material["type"] == "old":
        kept = sum(len(s) for s in starting)
        log(f"replayed {sum(len(s) for s in punched)} punches, reused {kept} existing "
            f"holes, scrapped {material['scrap_pitches']} blank pitches; "
            f"finished bytes match v{order['version_id']} sha256={digest[:16]}...")
    else:
        log(f"replayed {sum(len(s) for s in punched)} punches on a new blank tape; "
            f"finished bytes match v{order['version_id']} sha256={digest[:16]}...")
    log("replay ok")
    return 0


def replay_batch(directory, log=print):
    stock_path = os.path.join(directory, "stock.json")
    if not os.path.exists(stock_path):
        stock_path = None
    order_dir = os.path.join(directory, "machine_orders")
    names = sorted(name for name in os.listdir(order_dir) if name.endswith(".json"))
    if not names:
        raise ReplayError(f"no machine orders under {order_dir}")
    for name in names:
        order_path = os.path.join(order_dir, name)
        spectrum_path = os.path.join(directory, "spectra",
                                     name.replace("order_", "spectrum_", 1))
        if not os.path.exists(spectrum_path):
            spectrum_path = None
        log(f"== {name} ==")
        replay_order(order_path, spectrum_path, stock_path, log=log)
    log(f"batch replay ok: {len(names)} orders")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify-spectrum")
    p.add_argument("spectrum")

    p = sub.add_parser("replay")
    p.add_argument("order")
    p.add_argument("--spectrum", default=None)
    p.add_argument("--stock", default=None)

    p = sub.add_parser("replay-batch")
    p.add_argument("directory")

    args = parser.parse_args(argv)
    try:
        if args.command == "verify-spectrum":
            verify_spectrum(args.spectrum)
        elif args.command == "replay":
            replay_order(args.order, args.spectrum, args.stock)
        else:
            replay_batch(args.directory)
    except ReplayError as exc:
        print(f"REPLAY FAILURE: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"REPLAY FAILURE: malformed input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
