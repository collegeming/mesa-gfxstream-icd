#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export PATH=/usr/local/bin:/usr/bin:/bin
cd /build
mkdir -p downloads kernel out
exec > >(tee -a /build/out/build.log) 2>&1
fetch() { aria2c --console-log-level=warn --summary-interval=0 --max-tries=5 --retry-wait=3 --allow-overwrite=false --auto-file-renaming=false -x 4 -s 4 --dir=/build/downloads --out="$2" "$1"; }
mirror=https://mirrors.tuna.tsinghua.edu.cn/archlinuxarm
printf 'Server = %s/$arch/$repo\n' "$mirror" > /etc/pacman.d/mirrorlist
# Package downloads also use aria2. No guest filesystem is mounted here.
sed -i '/^\[options\]$/a XferCommand = /usr/local/bin/aria2c --console-log-level=warn --summary-interval=0 --allow-overwrite=true --auto-file-renaming=false --out=%o %u' /etc/pacman.conf
pacman-key --init
pacman-key --populate archlinuxarm
pacman -Syu --noconfirm --needed base-devel bc openssl libelf kmod python git
# Refuse an unreviewed compiler change instead of silently drifting the kernel config.
test "$(gcc -dumpfullversion)" = 16.1.1
gcc --version
ld --version
ld --version | grep -q '2\.46'
commit=2b9d6582700fdec8ab92ed74a96c15e0c13a0435
fetch https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.tar.xz linux-7.3.tar.xz
printf '%s  %s\n' b38587333374ecce1141b9b5886c4fcf /build/downloads/linux-7.3.tar.xz | md5sum -c -
fetch https://cdn.kernel.org/pub/linux/kernel/v7.x/sha256sums.asc sha256sums.asc
(cd downloads; grep -E '^[0-9a-f]{64}  linux-7\.3\.tar\.xz$' sha256sums.asc > kernel.sha256; test -s kernel.sha256; sha256sum -c kernel.sha256)
tar -xJf downloads/linux-7.3.tar.xz -C kernel
patches=(
  0001-arm64-dts-rockchip-disable-pwm0-on-rk3399-firefly.patch
  0002-pps-Compatibility-hack-should-be-X86-specific.patch
  0003-Revert-arm64-dts-rockchip-Move-rk3568-PCIe3-MSI-to-u.patch
  0004-serial-amba-pl011-add-arm-pl011-axi-binding-for-RP1.patch
  0005-arm64-dts-rpi5-add-RP1-UART0-GPIO14-15-console.patch
)
for patch in "${patches[@]}"; do
  fetch "https://raw.githubusercontent.com/archlinuxarm/PKGBUILDs/$commit/core/linux-aarch64/$patch" "$patch"
  git -C kernel/linux-7.3 apply "/build/downloads/$patch"
done
cd kernel/linux-7.3
cp /build/control/guest.config .config
printf '%s\n' -1 > localversion.10-pkgrel
printf '%s\n' -aarch64 > localversion.20-pkgname
export ARCH=arm64
export CC=aarch64-unknown-linux-gnu-gcc
command -v "$CC"
make olddefconfig
# Compiler-derived comments may differ, but no real kernel option may change.
python -I - /build/control/guest.config .config <<'PY'
import sys
from pathlib import Path
def options(p):
    return {s.split('=',1)[0]: s for s in Path(p).read_text().splitlines() if s.startswith('CONFIG_') or s.startswith('# CONFIG_')}
a,b=map(options,sys.argv[1:])
delta=[(k,a.get(k),b.get(k)) for k in sorted(a.keys()|b.keys()) if a.get(k)!=b.get(k)]
if delta:
    print('Unexpected config drift:',delta)
    raise SystemExit(1)
PY
release=$(make -s kernelrelease)
test "$release" = 7.3.0-1-aarch64-ARCH
make -j"$(nproc)" vmlinux
test -s vmlinux.symvers
# All virtio-snd prerequisites are built in. This is the real modpost export
# table from the matching vmlinux, not a fabricated or mismatched symbol file.
cp vmlinux.symvers Module.symvers
cp -a sound/virtio /build/virtio-snd
make -j"$(nproc)" M=/build/virtio-snd CONFIG_SND_VIRTIO=m modules
install -m644 /build/virtio-snd/virtio_snd.ko /build/out/virtio_snd.ko
cp .config /build/out/build.config
cp /build/control/guest.config /build/out/guest.config
cp vmlinux.symvers /build/out/vmlinux.symvers
cp include/generated/utsrelease.h /build/out/utsrelease.h
modinfo /build/out/virtio_snd.ko | tee /build/out/modinfo.txt
test "$(modinfo -F vermagic /build/out/virtio_snd.ko)" = '7.3.0-1-aarch64-ARCH SMP preempt mod_unload aarch64'
test -z "$(modinfo -F depends /build/out/virtio_snd.ko)"
readelf -h /build/out/virtio_snd.ko | tee /build/out/elf-header.txt
readelf -h /build/out/virtio_snd.ko | grep -q 'Machine:.*AArch64'
nm -u /build/out/virtio_snd.ko > /build/out/undefined-symbols.txt
python -I - /build/out <<'PY'
import sys
from pathlib import Path
p=Path(sys.argv[1])
exports={s.split()[1] for s in (p/'vmlinux.symvers').read_text().splitlines()}
required={s.split()[-1] for s in (p/'undefined-symbols.txt').read_text().splitlines() if s.split()[-2]=='U'}
missing=required-exports
print('Module imports:',len(required),'missing builtin exports:',sorted(missing))
assert not missing
PY
printf 'kernel_release=%s\nalarm_commit=%s\nconfig_sha256=%s\n' "$release" "$commit" "$(sha256sum /build/control/guest.config | cut -d' ' -f1)" > /build/out/provenance.txt
sha256sum /build/downloads/* >> /build/out/provenance.txt
(cd /build/out; sha256sum virtio_snd.ko build.config guest.config vmlinux.symvers > SHA256SUMS)
# Do not package vmlinux/Image: deployment is module-only and controlled by the caller.
