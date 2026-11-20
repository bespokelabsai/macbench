Build a general-purpose lossless compressor that matches or beats the best compressors in the world on real data of all kinds (some samples and SOTA statistics provided). 

You have 4 hours for this task. You can use all of it to keep improving the compressor. Check the time with `date` as you go, keep the compressor building and correct at all times, and stop before the 4 hours are up.

Don't start subagents or other agents to help you.

## What gets graded

The `compressor/` folder on `main`. `compressor/build.sh` builds it and must produce two programs:

- `compressor/bin/compress IN OUT`
- `compressor/bin/decompress IN OUT`

`build.sh` runs inside the `compressor/` folder and must put everything it makes in `compressor/bin/`. That folder is deleted before we check, so `build.sh` has to build all of it.

Main is graded at intervals while you work and again at the end, so keep it building and correct.

## Data

`/data` has example data of twelve kinds:

| name | what it is |
|---|---|
| english | English novels, plain text |
| polish | Polish books, text |
| dictionary | dictionary, marked-up text |
| xml | XML |
| source | source code in a tar |
| programs | program files in a tar, mostly executable code |
| dll | Windows DLL, 32-bit x86 code |
| mri | MRI scan, 16-bit pixels |
| xray | X-ray image, 16-bit pixels |
| chemistry | chemical structure database, text |
| stars | star catalog, binary records |
| database | database file, binary |

- `/data/public/<name>`: a large example file of each kind, from the Silesia corpus.
- `/data/samples/<name>`: a practice file of each kind, from a different source.
- `/data/samples.json`: for each practice file, the size from the best standard tool and the state-of-the-art size.

The compressor is graded on hidden test data of many different types. Aim for a general-purpose compressor that comes close to the state of the art on any kind of data.

## Targets

On each hidden file:

- the worst expected result is the size from `lpaq1`, a basic context-mixing compressor, or from the best of `xz -9e`, `zstd --ultra -22 --long=27`, `brotli -q 11 -w 24` and `bzip2 -9` if that is smaller;
- the best expected result is the state of the art: the smallest size reached on that file, within the limits below, by paq8px (v217, level -9 with its LSTM and text models), one of the strongest general-purpose compressors in the world, or, for images, by JPEG XL in lossless mode at maximum effort.

Beating the state of the art across all the hidden data is the real goal.

To check yourself as you go, compare your sizes on the practice files with `/data/samples.json`, which gives each one's worst expected size (`baseline_bytes`) and best expected size (`sota_bytes`).

## Rules

- The decompressed file must match the original byte for byte, or that file counts as a failure.
- Each compress or decompress run gets at most 15 minutes of CPU time, 20 minutes of wall time, 8 GiB of memory and 16 MiB of output. A run over any limit counts as a failure for that file.
- Runs happen as an unprivileged user with no network. Each run starts in a fresh, empty folder with only its input file. Nothing is kept between compressing and decompressing.
- While graded, `build.sh` and both programs can read only their own copy of `compressor/` and the system's files: not `/data`, and not the rest of the repository.
- `build.sh` gets 15 minutes and no network. The machine has Python and its standard library, gcc/g++, make, perl, and the usual command-line tools, including `xz`, `bzip2` and `gzip`, but not `zstd` or `brotli`. There are no compression library headers (such as zlib or liblzma), and the machine is air-gapped from the internet.
- Everything in `compressor/` except `bin/` must fit in 1 MiB after `xz -9e`.
