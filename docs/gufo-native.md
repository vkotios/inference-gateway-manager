# Native Gufo on the gateway host

IGM manages Gufo as a native process. This page records a verified way to build and install
it from a pinned upstream commit, so the engine is reproducible and does not depend on
another engine's runtime. Upstream's [README](https://github.com/gufo-org/gufo) lists the
qualified toolchain; deviations below are noted.

## 1. Pin the source

```sh
git clone https://github.com/gufo-org/gufo.git gufo-src
git -C gufo-src checkout 7b27845   # release 0.19.0; use the commit you reviewed
```

## 2. Toolchain (isolated, no system ROCm needed)

The ROCm SDK, CMake and Ninja can live in a private virtual environment. Nothing is shared
with other engines.

```sh
uv venv gufo-toolchain --python 3.12
uv pip install --python gufo-toolchain/bin/python cmake ninja
uv pip install --python gufo-toolchain/bin/python \
  --index-url https://rocm.nightlies.amd.com/v2/gfx1151/ \
  rocm==VERSION rocm-sdk-core==VERSION rocm-sdk-devel==VERSION rocm-sdk-libraries-gfx1151==VERSION
gufo-toolchain/bin/rocm-sdk init   # expands the development files
```

Choose one exact `VERSION` and record it. Use only the AMD index for the `rocm*` packages.
Upstream qualifies ROCm 7.2.3; the gfx1151 index carries 7.14 nightlies, so run
`gufo diagnose` and your own checks. Host packages on Fedora:

```sh
sudo dnf install libicu-devel libcurl-devel libpng-devel libjpeg-turbo-devel libwebp-devel openssl-devel
```

## 3. Build and install

```sh
V=$PWD/gufo-toolchain; SP=$V/lib/python3.12/site-packages; SDK=$SP/_rocm_sdk_devel
LIBDIRS="$SDK/lib:$SDK/lib/llvm/lib:$SP/_rocm_sdk_libraries_gfx1151/lib:$SP/_rocm_sdk_core/lib"
export PATH=$V/bin:$PATH LD_LIBRARY_PATH=$LIBDIRS ROCM_PATH=$SDK HIP_PATH=$SDK HIP_PLATFORM=amd
cd gufo-src
cmake -S . -B build/native -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_TESTING=OFF -DENGINE_ENABLE_HIP=ON \
  -DCMAKE_PREFIX_PATH="$SDK;$SP/_rocm_sdk_libraries_gfx1151;$SP/_rocm_sdk_core" \
  -DCMAKE_HIP_COMPILER="$SDK/llvm/bin/clang++" -DCMAKE_HIP_COMPILER_ROCM_ROOT="$SDK" \
  -DCMAKE_HIP_FLAGS="--rocm-path=$SDK --rocm-device-lib-path=$SDK/lib/llvm/amdgcn/bitcode" \
  -DCMAKE_CXX_FLAGS="-include format -include chrono" \
  -DCMAKE_EXE_LINKER_FLAGS="-no-pie -Wl,-rpath,$LIBDIRS" \
  -DCMAKE_INSTALL_PREFIX=$HOME/gufo-native
cmake --build build/native --parallel 8 && cmake --install build/native
$HOME/gufo-native/bin/gufo diagnose
```

The two non-default flags were needed with GCC 16 and change no source: forcing `<format>`
and `<chrono>` first avoids libstdc++ 16's `[[__gnu__::__noinline__]]` clashing with HIP's
`__noinline__` macro, and `-no-pie` matches the non-PIE objects the ROCm linker would
otherwise reject. The `-rpath` entry bakes the private ROCm library directories into the
binary, so it runs without `LD_LIBRARY_PATH`.

## 4. Configure IGM

Use the `gufo` engine from `examples/config.example.json`, with `argv[0]` set to the
installed `gufo`. Keep `--sessions 1`. Before loading through the gateway, run the binary
once by hand with a small model to confirm it starts as the gateway user (GPU group access,
readable weights). Registration, loading, context and unload then work exactly as for the
other engines; native `/slots` occupancy is shown in status.
