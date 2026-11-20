import gzip
import hashlib
import io
import os
import sys
import time
import urllib.request
import zipfile

SILESIA_URL = "https://sun.aei.polsl.pl/~sdeor/corpus/silesia.zip"
SILESIA = {
    "dickens": ("english", "b24c37886142e11d0ee687db6ab06f936207aa7f2ea1fd1d9a36763c7a507e6a"),
    "mozilla": ("programs", "657fc3764b0c75ac9de9623125705831ebbfbe08fed248df73bc2dc66e2a963b"),
    "mr": ("mri", "68637ed52e3e4860174ed2dc0840ac77d5f1a60abbcb13770d5754e3774d53e6"),
    "nci": ("chemistry", "fc63a31770947b8c2062d3b19ca94c00485a232bb91b502021948fee983e1635"),
    "ooffice": ("dll", "e7ee013880d34dd5208283d0d3d91b07f442e067454276095ded14f322a656eb"),
    "osdb": ("database", "60f027179302ca3ad87c58ac90b6be72ec23588aaa7a3b7fe8ecc0f11def3fa3"),
    "reymont": ("polish", "0eac0114a3dfe6e2ee1f345a0f79d653cb26c3bc9f0ed79238af4933422b7578"),
    "samba": ("source", "93ba07bc44d8267789c1d911992f40b089ffa2140b4a160fac11ccae9a40e7b2"),
    "sao": ("stars", "c2d0ea2cc59d4c21b7fe43a71499342a00cbe530a1d5548770e91ecd6214adcc"),
    "webster": ("dictionary", "6a68f69b26daf09f9dd84f7470368553194a0b294fcfa80f1604efb11143a383"),
    "xml": ("xml", "0e82e54e695c1938e4193448022543845b33020c8be6bf3bf3ead2224903e08c"),
    "x-ray": ("xray", "7de9fce1405dc44ae5e6813ed21cd5751e761bd4265655a005d39b9685d1c9ad"),
}
SLICE_URL = "https://data.gharchive.org/2024-01-15-12.json.gz"
SLICE_BYTES = 1 << 20
SLICE_SHA256 = "9e2b2ea2ace9b51321e6e4abf095e51f171ad8d38aff3c07598c84bc43ae3f82"
PUBLIC = "/data/public"
SEALED = "/var/lib/compressor-sprint/sealed"
SEALED_BYTES = 3 << 19


def fetch(url, read):
    error = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "compressor-sprint-build"})
            with urllib.request.urlopen(request, timeout=300) as response:
                return read(response)
        except (OSError, EOFError, zipfile.BadZipFile) as e:
            error = e
            time.sleep(5 * (attempt + 1))
    sys.exit(f"fetch_data: could not download {url}: {error}")


def keystream(key, n):
    return b"".join(hashlib.sha256(key + i.to_bytes(8, "big")).digest() for i in range((n + 31) // 32))[:n]


def seal(data, key):
    plain = len(data).to_bytes(8, "big") + data
    plain += bytes(SEALED_BYTES - len(plain))
    return (int.from_bytes(plain, "big") ^ int.from_bytes(keystream(key, len(plain)), "big")).to_bytes(len(plain), "big")


def public():
    archive = fetch(SILESIA_URL, lambda r: zipfile.ZipFile(io.BytesIO(r.read())))
    os.makedirs(PUBLIC, exist_ok=True)
    for name, (ours, sha256) in SILESIA.items():
        data = archive.read(name)
        if hashlib.sha256(data).hexdigest() != sha256:
            sys.exit(f"fetch_data: {name} of {SILESIA_URL} does not match its pinned SHA-256")
        with open(os.path.join(PUBLIC, ours), "wb") as f:
            f.write(data)


def hidden():
    data = fetch(SLICE_URL, lambda r: gzip.GzipFile(fileobj=r).read(SLICE_BYTES))
    if hashlib.sha256(data).hexdigest() != SLICE_SHA256:
        sys.exit(f"fetch_data: the first {SLICE_BYTES} bytes of {SLICE_URL} do not match their pinned SHA-256")
    os.makedirs(os.path.dirname(SEALED), exist_ok=True)
    with open(SEALED, "wb") as f:
        f.write(seal(data, bytes.fromhex(SLICE_SHA256)))
    os.chmod(SEALED, 0o400)


if __name__ == "__main__":
    public()
    hidden()
