#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sdcheck.py - quickly checks an SD card or USB flash drive for fake capacity
and failing flash memory. A macOS-oriented alternative to Linux-only f3probe.

WARNING: This is a DESTRUCTIVE test. It overwrites data at random locations
across the device and also writes a small sequential speed-test sample.

Run:       sudo /usr/bin/python3 sdcheck.py /dev/rdisk6
Self-test: /usr/bin/python3 sdcheck.py --selftest  (does not access a device)

Method: write tagged 32 KiB blocks at random 4 KiB-aligned locations across
the device, then read them back. Every sector contains its absolute address:
  * wrap-around counterfeit: writes beyond the real capacity overwrite earlier
    sectors, so the read-back contains a different address;
  * limbo counterfeit: data beyond the real capacity is not retained;
  * failing flash: corrupted blocks appear throughout the device.
"""
import argparse
import collections
import errno
import fcntl
import hashlib
import math
import os
import plistlib
import random
import re
import struct
import subprocess
import sys
import tempfile
import time
from functools import reduce

SECTOR = 512
MAGIC = b"SDCHK001"
HDR = struct.Struct("<8sQQ")          # magic, block index, absolute sector number
BODY = SECTOR - HDR.size
ZERO = bytes(SECTOR)
FULL = b"\xff" * SECTOR
MiB = 1 << 20
GiB = 1 << 30


# ----------------------------------------------------------------- devices
class RawDev(object):
    def __init__(self, path, size_bytes):
        self.fd = os.open(path, os.O_RDWR)
        self.size = size_bytes

    def read(self, off, n):
        buf = b""
        while len(buf) < n:
            chunk = os.pread(self.fd, n - len(buf), off + len(buf))
            if not chunk:
                raise OSError(errno.EIO, "short read at offset %d" % (off + len(buf)))
            buf += chunk
        return buf

    def write(self, off, data):
        view = memoryview(data)
        done = 0
        while done < len(data):
            w = os.pwrite(self.fd, view[done:], off + done)
            if w <= 0:
                raise OSError(errno.EIO, "short write at offset %d" % (off + done))
            done += w

    def sync(self):
        try:
            fcntl.fcntl(self.fd, fcntl.F_FULLFSYNC)
        except OSError:
            pass

    def close(self):
        os.close(self.fd)


class SimDev(object):
    """File-backed card simulator for self-tests. Modes: good, wrap, limbo, limbo_noise, flaky."""
    BLK = 4096

    def __init__(self, size_bytes, mode, real_bytes=None):
        self.size = size_bytes
        self.mode = mode
        self.real = real_bytes or size_bytes
        self.f = tempfile.TemporaryFile()
        phys = self.real if mode in ("wrap", "limbo", "limbo_noise") else size_bytes
        self.f.truncate(phys)
        self.phys = phys

    def _bad_block(self, b):
        return hashlib.md5(b"flaky%d" % b).digest()[0] < 5     # ~2% of blocks

    def read(self, off, n):
        out = []
        for a in range(off, off + n, self.BLK):
            m = min(self.BLK, off + n - a)
            if self.mode == "wrap":
                self.f.seek(a % self.real)
                chunk = self.f.read(m)
            elif self.mode in ("limbo", "limbo_noise"):
                if a >= self.real:
                    chunk = os.urandom(m) if self.mode == "limbo_noise" else bytes(m)
                else:
                    self.f.seek(a)
                    chunk = self.f.read(m)
            else:
                self.f.seek(a)
                chunk = self.f.read(m)
                if self.mode == "flaky" and self._bad_block(a // self.BLK):
                    chunk = bytes(x ^ 0x5A for x in chunk)
            out.append(chunk.ljust(m, b"\0"))
        return b"".join(out)

    def write(self, off, data):
        for a in range(off, off + len(data), self.BLK):
            piece = data[a - off:a - off + self.BLK]
            if self.mode == "wrap":
                self.f.seek(a % self.real)
                self.f.write(piece)
            elif self.mode in ("limbo", "limbo_noise"):
                if a < self.real:
                    self.f.seek(a)
                    self.f.write(piece)
            else:
                self.f.seek(a)
                self.f.write(piece)

    def sync(self):
        pass

    def close(self):
        self.f.close()


# ----------------------------------------------------------------- test block data
def build_tag(seed, idx, start, nsec):
    filler = hashlib.shake_256(seed + struct.pack("<QQ", idx, start)).digest(nsec * BODY)
    parts = []
    for k in range(nsec):
        parts.append(HDR.pack(MAGIC, idx, start + k))
        parts.append(filler[k * BODY:(k + 1) * BODY])
    return b"".join(parts)


def pick_positions(total_sectors, nsec, n, rng, align=8):
    last_start = ((total_sectors - nsec) // align) * align
    slots = last_start // align + 1
    n = min(n, slots)
    picks = sorted(rng.sample(range(slots), n))
    pos, end = [], -1
    for b in picks:
        s = b * align
        if s >= end:
            pos.append(s)
            end = s + nsec
    if pos[-1] != last_start:
        if pos[-1] + nsec <= last_start:
            pos.append(last_start)
        else:
            pos[-1] = last_start          # The previous block ended no later than the old final position.
    return pos


def classify(got, exp, start, nsec):
    foreign = collections.Counter()
    blank = garbage = 0
    for k in range(nsec):
        a = k * SECTOR
        g = got[a:a + SECTOR]
        if g == exp[a:a + SECTOR]:
            continue
        magic, _idx, q = HDR.unpack_from(g, 0)
        if magic == MAGIC and q != start + k:
            foreign[abs(start + k - q) * SECTOR] += 1
        elif g == ZERO or g == FULL:
            blank += 1
        else:
            garbage += 1
    return {"pos": start, "foreign": foreign, "blank": blank, "garbage": garbage}


# ----------------------------------------------------------------- output
class Out(object):
    def __init__(self, echo=True):
        self.lines = []
        self.echo = echo

    def __call__(self, msg=""):
        if self.echo:
            print(msg)
            sys.stdout.flush()
        self.lines.append(msg)


def human(b):
    return "%.2f GiB (%.2f GB)" % (b / float(GiB), b / 1e9)


def eta(sec):
    sec = int(sec)
    return "%d:%02d" % (sec // 60, sec % 60)


def progress(label, done, total, t0):
    el = time.time() - t0
    rate = done / el if el > 0 else 0
    left = (total - done) / rate if rate > 0 else 0
    sys.stdout.write("\r  %s %d/%d  (%.0f ops/s, ~%s left)   " % (label, done, total, rate, eta(left)))
    sys.stdout.flush()


# ----------------------------------------------------------------- main test
def probe(dev, total_bytes, n_samples, tag_sectors, log):
    total_sectors = total_bytes // SECTOR
    seed = os.urandom(16)
    rng = random.Random(int.from_bytes(seed[:8], "little"))
    pos = pick_positions(total_sectors, tag_sectors, n_samples, rng)
    n = len(pos)
    order = list(range(n))
    rng.shuffle(order)
    log("Test A: writing %d blocks of %d KiB to random locations across the device (~%d MiB)..."
        % (n, tag_sectors * SECTOR // 1024, n * tag_sectors * SECTOR // MiB))

    written, write_errors = [], []
    t0 = time.time()
    last = t0
    try:
        for k, i in enumerate(order):
            try:
                dev.write(pos[i] * SECTOR, build_tag(seed, i, pos[i], tag_sectors))
                written.append(i)
            except OSError as e:
                write_errors.append((pos[i], e))
                if len(write_errors) >= 50 and len(write_errors) > len(written):
                    log("\n  Too many write errors; stopping.")
                    break
            now = time.time()
            if now - last >= 3:
                progress("write", k + 1, n, t0)
                last = now
    except KeyboardInterrupt:
        log("\n  Interrupted; checking the blocks written so far.")
    print("")
    dev.sync()
    log("  Write phase took %s" % eta(time.time() - t0))

    log("Test A: reading back and verifying...")
    written.sort(key=lambda i: pos[i])
    good_pos, bad, read_errors = [], [], []
    t0 = time.time()
    last = t0
    for k, i in enumerate(written):
        try:
            got = dev.read(pos[i] * SECTOR, tag_sectors * SECTOR)
        except OSError as e:
            read_errors.append((pos[i], e))
            continue
        exp = build_tag(seed, i, pos[i], tag_sectors)
        if got == exp:
            good_pos.append(pos[i])
        else:
            bad.append(classify(got, exp, pos[i], tag_sectors))
        now = time.time()
        if now - last >= 3:
            progress("read", k + 1, len(written), t0)
            last = now
    print("")
    return {"n": n, "written": len(written), "pos_all": sorted(pos[i] for i in written),
            "good": good_pos, "bad": bad, "write_errors": write_errors, "read_errors": read_errors}


def analyze(res, total_bytes, tag_sectors, log):
    """Return (verdict, estimated real capacity in bytes or None).
    Verdict is one of: OK, WRAP, LIMBO, SCATTER, or IOERR."""
    bad = res["bad"]
    log("")
    log("Blocks checked: %d | mismatched: %d | write errors: %d | read errors: %d"
        % (res["written"], len(bad), len(res["write_errors"]), len(res["read_errors"])))
    for title, errs in (("write", res["write_errors"]), ("read", res["read_errors"])):
        for p, e in errs[:5]:
            log("  %s error at %.2f GiB: %s" % (title, p * SECTOR / float(GiB), e))

    if not bad:
        if res["write_errors"] or res["read_errors"]:
            log("VERDICT: Written data matched, but I/O errors occurred. The card or reader may be faulty.")
            return "IOERR", None
        log("VERDICT: No evidence of fake capacity found (sampled test).")
        log("  This is not a guarantee. A full f3write/f3read pass over the entire card is more thorough and may take hours.")
        return "OK", None

    for b in bad:
        f = sum(c for d, c in b["foreign"].items())
        z, g = b["blank"], b["garbage"]
        b["cat"] = "alias" if f >= max(z, g) else ("blank" if z >= g else "garbage")
    n_alias = sum(1 for b in bad if b["cat"] == "alias")

    pos_sorted = sorted(b["pos"] for b in bad)
    first_bad = pos_sorted[0]
    above = [p for p in res["pos_all"] if p >= first_bad]
    frac_above = len(bad) / float(len(above)) if above else 0.0

    if n_alias * 2 >= len(bad):
        d_cnt = collections.Counter()
        for b in bad:
            for d in b["foreign"]:
                d_cnt[d] += 1
        total_hits = sum(d_cnt.values())
        solid = dict((d, c) for d, c in d_cnt.items() if c >= max(2, total_hits // 50)) or dict(d_cnt)
        c_min = min(solid)
        c_gcd = reduce(math.gcd, solid)
        log("VERDICT: COUNTERFEIT CAPACITY (wrap-around). Writes beyond the real capacity")
        log("  overwrite earlier areas of the device.")
        log("  Reported capacity:  %s" % human(total_bytes))
        log("  Estimated real size: ~%s (from the smallest address shift)" % human(c_min))
        if c_gcd != c_min:
            log("  (GCD of all shifts: %s)" % human(c_gcd))
        log("  Frequent shifts (GiB -> matches): " + ", ".join(
            "%.2f -> %d" % (d / float(GiB), c) for d, c in d_cnt.most_common(6)))
        return "WRAP", c_min

    if frac_above >= 0.9 and first_bad > 0:
        last_good = max([p for p in res["good"] if p < first_bad] or [0])
        lo, hi = last_good * SECTOR, first_bad * SECTOR
        log("VERDICT: COUNTERFEIT CAPACITY (limbo). Data beyond the real capacity is not retained.")
        log("  Reported capacity: %s" % human(total_bytes))
        log("  Estimated real size: ~%.2f GiB (between %.2f and %.2f GiB; %.0f%% of later samples failed)"
            % (hi / float(GiB), lo / float(GiB), hi / float(GiB), frac_above * 100))
        return "LIMBO", lo

    log("VERDICT: DATA ERRORS ARE SCATTERED across the device (%d of %d blocks, %.1f%%). Flash memory may be failing or"
        % (len(bad), res["written"], 100.0 * len(bad) / max(res["written"], 1)))
    log("  the card may be low quality. Do not trust it with data.")
    log("  First bad block at %.2f GiB; failed samples above it: %.0f%%" % (first_bad * SECTOR / float(GiB), frac_above * 100))
    return "SCATTER", None


def speed_test(dev, total_bytes, mb, log):
    blk = MiB
    base = (total_bytes // 2 // blk) * blk
    if base + mb * blk > total_bytes:
        base = 0
    pool = [os.urandom(blk) for _ in range(8)]

    def block(i):
        return struct.pack("<Q", i) + pool[i % 8][8:]

    log("Test B: sequential write/read of %d MiB (middle of the device)..." % mb)
    errors = 0
    t0 = time.time()
    for i in range(mb):
        try:
            dev.write(base + i * blk, block(i))
        except OSError as e:
            errors += 1
            if errors <= 3:
                log("  Write error: %s" % e)
    dev.sync()
    w = mb / max(time.time() - t0, 1e-6)
    t0 = time.time()
    mism = 0
    for i in range(mb):
        try:
            if dev.read(base + i * blk, blk) != block(i):
                mism += 1
        except OSError as e:
            errors += 1
            if errors <= 3:
                log("  Read error: %s" % e)
    r = mb / max(time.time() - t0, 1e-6)
    log("  Write: %.1f MB/s | read: %.1f MB/s | mismatches: %d | I/O errors: %d" % (w, r, mism, errors))
    if w < 5:
        log("  Write speed is very low (<5 MB/s), which is poor for a Steam Deck or camera card.")
    return w, r, mism, errors


# ----------------------------------------------------------------- startup
def selftest():
    ok = True
    cases = [
        ("good", 2 * GiB, None, "OK"),
        ("wrap", 2 * GiB, 300 * MiB, "WRAP"),
        ("wrap", 2 * GiB, 256 * MiB, "WRAP"),
        ("limbo", 2 * GiB, 512 * MiB, "LIMBO"),
        ("limbo_noise", 2 * GiB, 700 * MiB, "LIMBO"),
        ("flaky", 2 * GiB, None, "SCATTER"),
    ]
    for mode, size, real, want in cases:
        print("\n=== self-test: %s, reported %s, actual %s ===" % (
            mode, human(size), human(real) if real else "-"))
        dev = SimDev(size, mode, real)
        quiet = Out(echo=False)
        res = probe(dev, size, 2500, 64, quiet)
        verdict, _real = analyze(res, size, 64, quiet)
        print("\n".join(l for l in quiet.lines if l.startswith(("VERDICT", "  Estimated real", "  Reported", "  (GCD", "  Frequent"))))
        print("-> got %s, expected %s: %s" % (verdict, want, "OK" if verdict == want else "FAIL"))
        ok = ok and verdict == want
        dev.close()
    # RawDev (os.pread/os.pwrite) on a regular file
    print("\n=== self-test: RawDev on a regular file ===")
    with tempfile.NamedTemporaryFile() as tf:
        tf.truncate(512 * MiB)
        dev = RawDev(tf.name, 512 * MiB)
        quiet = Out(echo=False)
        res = probe(dev, 512 * MiB, 1500, 64, quiet)
        verdict, _real = analyze(res, 512 * MiB, 64, quiet)
        speed_test(dev, 512 * MiB, 16, quiet)
        print("-> got %s, expected OK: %s" % (verdict, "OK" if verdict == "OK" else "FAIL"))
        ok = ok and verdict == "OK"
        dev.close()
    print("\nSELF-TEST: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def target_info(path):
    m = re.fullmatch(r"/dev/(r?)(disk\d+)", path)
    if not m:
        sys.exit("Use a whole-disk device such as /dev/rdisk6 (do not include a partition suffix such as s1).")
    ident = m.group(2)
    r = subprocess.run(["diskutil", "info", "-plist", "/dev/" + ident], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        sys.exit("diskutil cannot find %s: %s" % (ident, r.stderr.decode(errors="replace")))
    info = plistlib.loads(r.stdout)
    if not info.get("WholeDisk"):
        sys.exit("%s is not a whole disk." % ident)
    if not (info.get("RemovableMedia") or info.get("Removable")) or info.get("OSInternalMedia"):
        sys.exit("%s does not appear to be removable media; refusing to modify it." % ident)
    return ident, info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", nargs="?", help="whole-disk device, e.g. /dev/rdisk6")
    ap.add_argument("--samples", type=int, default=20000, help="number of random blocks (default: 20000)")
    ap.add_argument("--tag-kib", type=int, default=32, help="block size in KiB (default: 32)")
    ap.add_argument("--speed-mb", type=int, default=128, help="sequential speed-test size in MiB (0 to skip; default: 128)")
    ap.add_argument("--selftest", action="store_true", help="run simulated-device tests without accessing a real device")
    a = ap.parse_args()

    if a.samples < 1:
        ap.error("--samples must be at least 1")
    if a.tag_kib < 4 or a.tag_kib % 4:
        ap.error("--tag-kib must be a positive multiple of 4")
    if a.speed_mb < 0:
        ap.error("--speed-mb cannot be negative")

    if a.selftest:
        return selftest()
    if not a.device:
        ap.error("specify a device, e.g. /dev/rdisk6")
    if os.geteuid() != 0:
        sys.exit("Run with sudo.")

    ident, info = target_info(a.device)
    total = int(info.get("TotalSize") or info.get("Size"))
    if a.speed_mb * MiB > total:
        ap.error("--speed-mb cannot exceed the device capacity")
    if a.tag_kib * 1024 >= total:
        ap.error("--tag-kib must be smaller than the device capacity")
    name = info.get("MediaName") or info.get("IORegistryEntryName") or "?"
    print("Device: %s | %s | %s | bus: %s" % (ident, name, human(total), info.get("BusProtocol")))
    print("WARNING: This test overwrites data on the device. Back up anything you need first.")
    if input("To confirm, enter the disk identifier (%s): " % ident).strip() != ident:
        sys.exit("Cancelled.")

    unmount = subprocess.run(["diskutil", "unmountDisk", "force", "/dev/" + ident])
    if unmount.returncode != 0:
        sys.exit("Could not unmount %s; refusing to write to the raw device." % ident)
    try:
        dev = RawDev("/dev/r" + ident, total)
    except PermissionError:
        sys.exit("Cannot access the device. In System Settings -> Privacy & Security -> Full Disk Access, "
                 "enable Terminal (or iTerm), restart it, and try again.")
    except OSError as e:
        sys.exit("Cannot open /dev/r%s: %s" % (ident, e))

    log = Out()
    log("sdcheck | %s | %s | %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), ident, human(total)))
    try:
        res = probe(dev, total, a.samples, a.tag_kib * 1024 // SECTOR, log)
        verdict, real = analyze(res, total, a.tag_kib * 1024 // SECTOR, log)
        if a.speed_mb:
            log("")
            speed_test(dev, total, a.speed_mb, log)
    finally:
        dev.close()

    log("")
    if verdict == "OK":
        log("The test overwrote data on the card. To use it again, format it (this erases all remaining data):")
        log("  diskutil eraseDisk ExFAT SD MBRFormat /dev/%s" % ident)
    elif verdict in ("WRAP", "LIMBO"):
        safe_gb = max(int(real * 0.93 / 1e9), 1)
        log("COUNTERFEIT CAPACITY: formatting the full reported size will still cause data loss when the card fills.")
        log("Estimated real size: ~%.0f GB. Use only the first ~%d GB:" % (real / 1e9, safe_gb))
        log("  This may help recover some space, but it does not make the card reliable.")
        log('  diskutil partitionDisk /dev/%s MBR ExFAT SD %dG "Free Space" X 0' % (ident, safe_gb))
        log("Do not store important data on this card, even if partitioned to the estimated real size.")
    else:
        log("The card appears faulty. Do not use it to store data.")
    home = os.path.expanduser("~" + os.environ["SUDO_USER"]) if os.environ.get("SUDO_USER") else os.path.expanduser("~")
    report = os.path.join(home, "sdcheck_report.txt")
    with open(report, "w") as f:
        f.write("\n".join(log.lines) + "\n")
    if os.environ.get("SUDO_UID"):
        try:
            os.chown(report, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", "20")))
        except OSError:
            pass
    print("Report saved: %s" % report)
    return 0 if verdict == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
