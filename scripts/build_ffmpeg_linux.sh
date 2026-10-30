#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "This build recipe targets native Linux." >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Explicit source pins. Review upstream security releases before distribution.
FFMPEG_VERSION="${FFMPEG_VERSION:-7.1.1}"
OPUS_VERSION="${OPUS_VERSION:-1.5.2}"
JOBS="${JOBS:-$(nproc)}"

WORK="$ROOT/.build-ffmpeg"
DOWNLOADS="$WORK/downloads"
SOURCES="$WORK/sources"
PREFIX="$WORK/prefix"

BIN="$ROOT/src/audiomaster/bin"
VENDOR="$ROOT/src/audiomaster/vendor"

for tool in cc make pkg-config nasm curl tar xz strip sha256sum; do
    command -v "$tool" >/dev/null || {
        echo "Missing build tool: $tool" >&2
        exit 1
    }
done

mkdir -p "$DOWNLOADS" "$SOURCES" "$PREFIX" "$BIN" "$VENDOR"

fetch() {
    local url="$1"
    local destination="$2"

    if [[ ! -f "$destination" ]]; then
        curl \
            --fail \
            --location \
            --retry 3 \
            --proto '=https' \
            --tlsv1.2 \
            "$url" \
            -o "$destination.part"

        mv "$destination.part" "$destination"
    fi
}

FFMPEG_ARCHIVE="$DOWNLOADS/ffmpeg-$FFMPEG_VERSION.tar.xz"
OPUS_ARCHIVE="$DOWNLOADS/opus-$OPUS_VERSION.tar.gz"

fetch \
    "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz" \
    "$FFMPEG_ARCHIVE"

fetch \
    "https://downloads.xiph.org/releases/opus/opus-$OPUS_VERSION.tar.gz" \
    "$OPUS_ARCHIVE"

FFMPEG_SOURCE="$SOURCES/ffmpeg-$FFMPEG_VERSION"
OPUS_SOURCE="$SOURCES/opus-$OPUS_VERSION"

rm -rf "$FFMPEG_SOURCE" "$OPUS_SOURCE"

tar -xf "$FFMPEG_ARCHIVE" -C "$SOURCES"
tar -xf "$OPUS_ARCHIVE" -C "$SOURCES"

# Build only a static Opus library.
(
    cd "$OPUS_SOURCE"

    CFLAGS="${CFLAGS:--O2} -ffunction-sections -fdata-sections" \
    ./configure \
        --prefix="$PREFIX" \
        --libdir="$PREFIX/lib" \
        --disable-shared \
        --enable-static \
        --with-pic \
        --disable-doc \
        --disable-extra-programs

    make -j "$JOBS"
    make install
)

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"

DECODERS="mp3float,aac,alac,flac,libopus"
DECODERS+=",pcm_u8,pcm_s16le,pcm_s24le,pcm_s32le,pcm_s64le"
DECODERS+=",pcm_f32le,pcm_f64le"
DECODERS+=",pcm_s16be,pcm_s24be,pcm_s32be,pcm_s64be"
DECODERS+=",pcm_f32be,pcm_f64be"

ENCODERS="pcm_s24le,aac,alac,flac,libopus"

DEMUXERS="mp3,wav,mov,flac,ogg"
MUXERS="wav,ipod,flac,opus,null"
PARSERS="aac,flac,mpegaudio,opus"

# atrim/anull and the buffer filters support FFmpeg's audio graph machinery.
FILTERS="abuffer,abuffersink,anull,atrim,aformat,aresample"
FILTERS+=",volume,highpass,acompressor,loudnorm"

(
    cd "$FFMPEG_SOURCE"

    ./configure \
        --prefix="$PREFIX" \
        --disable-everything \
        --disable-autodetect \
        --disable-programs \
        --enable-ffmpeg \
        --disable-doc \
        --disable-debug \
        --disable-network \
        --disable-avdevice \
        --disable-postproc \
        --disable-swscale \
        --disable-shared \
        --enable-static \
        --disable-gpl \
        --disable-version3 \
        --disable-nonfree \
        --enable-small \
        --enable-pthreads \
        --enable-avcodec \
        --enable-avformat \
        --enable-avfilter \
        --enable-swresample \
        --enable-libopus \
        --pkg-config-flags="--static" \
        --extra-cflags="-I$PREFIX/include -ffunction-sections -fdata-sections" \
        --extra-ldflags="-L$PREFIX/lib -Wl,--gc-sections" \
        --enable-protocol=file,pipe \
        --enable-decoder="$DECODERS" \
        --enable-encoder="$ENCODERS" \
        --enable-demuxer="$DEMUXERS" \
        --enable-muxer="$MUXERS" \
        --enable-parser="$PARSERS" \
        --enable-filter="$FILTERS"

    make -j "$JOBS" ffmpeg
)

install -m 755 "$FFMPEG_SOURCE/ffmpeg" "$BIN/ffmpeg"
strip --strip-unneeded "$BIN/ffmpeg"

# Retain notices, exact source archives, and the build recipe.
# Keeping these in the package makes this example self-contained.
cp "$FFMPEG_SOURCE/COPYING.LGPLv2.1" "$VENDOR/FFmpeg-LGPL-2.1.txt"
cp "$FFMPEG_SOURCE/LICENSE.md" "$VENDOR/FFmpeg-LICENSE.md"
cp "$OPUS_SOURCE/COPYING" "$VENDOR/Opus-COPYING.txt"

cp "$FFMPEG_ARCHIVE" "$VENDOR/"
cp "$OPUS_ARCHIVE" "$VENDOR/"
cp "$ROOT/scripts/build_ffmpeg_linux.sh" "$VENDOR/"

{
    echo "FFmpeg source: $FFMPEG_VERSION"
    echo "Opus source: $OPUS_VERSION"
    echo
    "$BIN/ffmpeg" -version
    echo
    cc --version
} > "$VENDOR/BUILD-INFO.txt" 2>&1

(
    cd "$VENDOR"
    sha256sum \
        "ffmpeg-$FFMPEG_VERSION.tar.xz" \
        "opus-$OPUS_VERSION.tar.gz" \
        > SHA256SUMS
)

echo
echo "Bundled executable created:"
echo "  $BIN/ffmpeg"
echo
echo "Runtime system-library dependencies:"
ldd "$BIN/ffmpeg"