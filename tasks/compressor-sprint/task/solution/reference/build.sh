#!/bin/bash
set -e
cd "$(dirname "$0")"
rm -rf bin
mkdir -p bin/obj
J=$(nproc)
if [ "$J" -gt 8 ]; then J=8; fi
F="-DNDEBUG -Ipaq8px/zlib -O3 -fno-fast-math -ffp-contract=off -flto=$J -funroll-loops -ftree-vectorize"
for f in adler32 crc32 deflate gzlib inffast inflate inftrees trees zutil; do echo "paq8px/zlib/$f.c"; done |
  xargs -P "$J" -I{} sh -c 'gcc -c '"$F"' -fexceptions -include unistd.h "$1" -o "bin/obj/$(echo "$1" | tr / _).o"' _ {}
find paq8px/src -name '*.cpp' | sort |
  xargs -P "$J" -I{} sh -c 'g++ -c '"$F"' -fno-rtti -std=gnu++17 "$1" -o "bin/obj/$(echo "$1" | tr / _).o"' _ {}
g++ $F -s -static -fno-rtti -std=gnu++17 bin/obj/*.o -o bin/paq8px
rm -rf bin/obj
g++ -O2 -fno-fast-math -ffp-contract=off -s -static -std=gnu++17 g16.cpp -o bin/g16
cp paq8px/english.dic paq8px/english.exp paq8px/english.emb bin/
cat > bin/compress <<'W'
#!/bin/sh
set -e
d=$(dirname "$(readlink -f "$0")")
i=$(readlink -f "$1")
o=$(readlink -f "$2")
cd "$d"
./paq8px -9LT "$i" "$o.p" >/dev/null 2>&1
if ./g16 c "$i" "$o.g" >/dev/null 2>&1 && [ "$(stat -c %s "$o.g")" -lt "$(stat -c %s "$o.p")" ]; then
  mv "$o.g" "$o"
  rm -f "$o.p"
else
  mv "$o.p" "$o"
  rm -f "$o.g"
fi
W
cat > bin/decompress <<'W'
#!/bin/sh
set -e
d=$(dirname "$(readlink -f "$0")")
i=$(readlink -f "$1")
o=$(readlink -f "$2")
cd "$d"
if [ "$(head -c 3 "$i")" = G16 ]; then
  exec ./g16 d "$i" "$o" >/dev/null 2>&1
fi
exec ./paq8px -d "$i" "$o" >/dev/null 2>&1
W
chmod +x bin/compress bin/decompress
