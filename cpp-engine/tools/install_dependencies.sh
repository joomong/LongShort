#!/usr/bin/env bash
set -euo pipefail

prefix="$(realpath -m -- "$1")"
jobs="${LONGSHORT_BUILD_JOBS:-8}"
mkdir -p "$prefix"
exec 9> "$prefix/.install.lock"
flock 9
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ ! -f "$prefix/include/uuid/uuid.h" ] || [ ! -f "$prefix/lib/x86_64-linux-gnu/cmake/jsoncpp/jsoncppConfig.cmake" ]; then
    mkdir "$work/packages"
    (cd "$work/packages" && apt-get download libjsoncpp-dev libjsoncpp25 uuid-dev libuuid1)
    for package in "$work/packages/"*.deb; do dpkg -x "$package" "$work/packages/root"; done
    cp -a "$work/packages/root/usr/." "$prefix/"
fi

if [ ! -f "$prefix/lib/cmake/Arrow/ArrowConfig.cmake" ] || [ ! -f "$prefix/lib/cmake/Parquet/ParquetConfig.cmake" ]; then
    archive=apache-arrow-18.1.0.tar.gz
    curl -fsSL "https://archive.apache.org/dist/arrow/arrow-18.1.0/$archive" -o "$work/$archive"
    (cd "$work" && echo "7249c03a6097bc64fb0092143e4d4aaef3227565147e6254f026ddd504177c8dd565a184a0df39743dc989070dc3785e5b66f738c8e310ed9c982b61c2ec4914  $archive" | sha512sum -c -)
    tar -xzf "$work/$archive" -C "$work"
    ARROW_THRIFT_URL=https://archive.apache.org/dist/thrift/0.20.0/thrift-0.20.0.tar.gz \
    cmake -S "$work/apache-arrow-18.1.0/cpp" -B "$work/build" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
        -DCMAKE_INSTALL_LIBDIR=lib '-DCMAKE_INSTALL_RPATH=$ORIGIN' \
        -DARROW_PARQUET=ON -DARROW_BUILD_SHARED=ON -DARROW_BUILD_STATIC=OFF \
        -DARROW_BUILD_TESTS=OFF -DARROW_BUILD_EXAMPLES=OFF -DARROW_BUILD_UTILITIES=OFF \
        -DARROW_DEPENDENCY_SOURCE=BUNDLED -DARROW_WITH_SNAPPY=ON \
        -DARROW_WITH_ZSTD=ON -DARROW_WITH_ZLIB=ON \
        -DARROW_JEMALLOC=OFF -DARROW_MIMALLOC=OFF
    cmake --build "$work/build" --parallel "$jobs"
    cmake --install "$work/build"
fi
chmod -R a+rX "$prefix"
