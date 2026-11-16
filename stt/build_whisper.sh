#!/usr/bin/env bash
# Build whisper.cpp's whisper-cli for the Steam Deck, on the dev machine (WSL).
#
#   bash stt/build_whisper.sh
#
# - Uses the source already fetched into stt/src/whisper.cpp (see README), pinned to the
#   same commit upstream Decktation builds (backend/Dockerfile).
# - CPU only, no GPU/Vulkan, no OpenMP, no network during the build. The
#   flags below keep the ARM-only KleidiAI download (FetchContent) disabled.
# - Targets the Deck's Zen 2 CPU (AVX2/FMA/F16C), not the build machine
#   (GGML_NATIVE=OFF), and links libstdc++/libgcc statically, so the Deck only
#   needs its own glibc.
# - Refuses to run as root.
set -euo pipefail

PIN=f24588a272ae8e23280d9c220536437164e6ed28
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src/whisper.cpp"
OUT="$HERE/out"

if [ "$(id -u)" -eq 0 ]; then echo "REFUSING: do not build as root" >&2; exit 1; fi
command -v cmake >/dev/null || { echo "BLOCKER: cmake not found (sudo apt install cmake)" >&2; exit 2; }
[ "$(git -C "$SRC" rev-parse HEAD)" = "$PIN" ] || { echo "BLOCKER: source is not at pinned commit $PIN" >&2; exit 2; }
[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || { echo "BLOCKER: source has local modifications" >&2; exit 2; }

cmake -S "$SRC" -B "$SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON \
  -DGGML_OPENMP=OFF -DGGML_CPU_KLEIDIAI=OFF \
  -DWHISPER_CURL=OFF -DWHISPER_SDL2=OFF \
  -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_BUILD_EXAMPLES=ON \
  -DCMAKE_EXE_LINKER_FLAGS="-static-libgcc -static-libstdc++"
cmake --build "$SRC/build" --config Release --target whisper-cli -j"$(nproc)"

mkdir -p "$OUT"
install -m 0755 "$SRC/build/bin/whisper-cli" "$OUT/whisper-cli"
cp "$SRC/LICENSE" "$OUT/whisper.cpp-LICENSE-MIT.txt"

echo "--- what the binary needs at run time ---"
ldd "$OUT/whisper-cli" || true
echo "--- highest glibc symbol version it uses ---"
objdump -T "$OUT/whisper-cli" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1
echo "--- file mode (must not be setuid/setgid) ---"
stat -c '%A %n' "$OUT/whisper-cli"
sha256sum "$OUT/whisper-cli"
