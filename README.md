# sdcheck

A small, dependency-free macOS command-line tool that samples an SD card or USB flash drive for counterfeit capacity and failing flash memory.

> [!CAUTION]
> sdcheck writes directly to the selected device. It unmounts the disk and overwrites data at random locations across the device, then writes a sequential speed-test sample (128 MiB by default). Existing files and filesystem metadata can be damaged or lost. Back up anything you need first. Never run it on a device containing data you want to keep.

## What it checks

- Fake capacity caused by address wrap-around: writes beyond the real capacity overwrite earlier areas.
- Fake capacity caused by data not being retained beyond the real capacity (sometimes called “limbo”).
- Scattered data mismatches and read/write I/O errors that may indicate failing flash or a faulty reader.
- A short sequential read/write speed sample. This is informational only; it is not a reliable benchmark and does not determine the capacity verdict.

The capacity test is sampled, not a full-device verification. An `OK` result means no evidence of counterfeit capacity was found in the sampled blocks; it is not a guarantee. For a more thorough check, use a full `f3write`/`f3read` pass on a mounted volume and allow time for the entire card to be written and read.

## Requirements

- macOS with `diskutil`.
- Python 3 (the script uses only the Python standard library).
- Administrator access (`sudo`) and Full Disk Access for the terminal app when macOS requires it.

This version is designed for macOS and is not a supported Linux utility. It uses `diskutil` and macOS raw disk devices.

## Quick start

1. Back up the card. The test is destructive.
2. Connect the card and identify its whole-disk identifier:

   ```sh
   diskutil list
   ```

   Choose the removable disk itself, such as `/dev/disk6`, not a partition such as `/dev/disk6s1`. Double-check the identifier and capacity. Device numbers can change after reconnecting a card.
3. If needed, grant Full Disk Access to Terminal (or your terminal app): System Settings → Privacy & Security → Full Disk Access. Quit and reopen the terminal after changing access.
4. Close Disk Utility and run the self-test first. It uses simulated devices and does not access a real disk:

   ```sh
   /usr/bin/python3 ./sdcheck.py --selftest
   ```
5. Run the destructive test using the raw whole-disk path (the `r` prefix):

   ```sh
   sudo /usr/bin/python3 ./sdcheck.py /dev/rdisk6
   ```

   Replace `rdisk6` with the correct identifier for your card. The tool displays the device details and requires you to type the disk identifier before it proceeds. If the displayed device is not the card, cancel.
6. The report is saved as `~/sdcheck_report.txt` for the invoking user. The card is left unmounted. If you want to use it again, reformat it only after reviewing the results; formatting erases data too.

The default run samples 20,000 32 KiB blocks spread across the device and writes an additional 128 MiB speed-test sample. Runtime depends on the card and reader.

## Options

```text
usage: sdcheck.py [-h] [--samples SAMPLES] [--tag-kib TAG_KIB]
                  [--speed-mb SPEED_MB] [--selftest] [device]
```

- `--samples N`: number of random sample blocks (default: `20000`). More samples increase coverage and runtime.
- `--tag-kib N`: tagged block size in KiB (default: `32`).
- `--speed-mb N`: size of the sequential write/read sample in MiB (default: `128`). Use `0` to skip this extra write. The destructive capacity test still runs.
- `--selftest`: exercise the detection logic with simulated good, counterfeit, and failing devices; no physical device is opened.

## Verdicts

- `OK`: no fake-capacity evidence in the sampled blocks. Not a guarantee of full-device health or genuine capacity.
- `WRAP`: address aliases indicate writes beyond the real capacity are wrapping around onto earlier sectors.
- `LIMBO`: data beyond an estimated boundary was not retained; the report includes an approximate capacity.
- `SCATTER`: mismatches are spread across the device, suggesting failing or low-quality flash. Do not trust it with data.
- `IOERR`: read/write errors occurred. The card or reader may be faulty.

A fake-capacity result does not make a card safe to use. The suggested usable-size partition command in the report is provided only as a recovery option; even a partitioned counterfeit card is not reliable storage.

## How it works

The tool generates tagged blocks whose sectors contain their absolute sector addresses, writes them at random 4 KiB-aligned locations, flushes, and reads them back. Address aliases help identify wrap-around; blank or corrupted data beyond a boundary can indicate data that was not retained; failures across the address range suggest scattered media errors. The sample is randomized for each run.

## Development / verification

Run the built-in self-test without sudo or a connected card:

```sh
python3 sdcheck.py --selftest
```

The self-test simulates good media, two wrap-around capacities, limbo media with blank and noisy reads, scattered failures, and raw file I/O. It should finish with `SELF-TEST: PASS`.

## License

MIT. See [LICENSE](LICENSE).