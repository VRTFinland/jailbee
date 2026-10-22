#!/bin/sh
set -eu
VERSION=${1:?}
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends build-essential flex bison bc libssl-dev libelf-dev xz-utils cpio pkg-config curl ca-certificates >/dev/null
cd /var/tmp
curl -fsSL --retry 2 -o linux.tar.xz "https://cdn.kernel.org/pub/linux/kernel/v${VERSION%%.*}.x/linux-${VERSION}.tar.xz"
tar -xJf linux.tar.xz
cd "linux-${VERSION}"
zcat /proc/config.gz > .config
scripts/config --enable NF_CONNTRACK_BRIDGE --enable SECURITY_APPARMOR --enable BTRFS_FS --enable BTRFS_FS_POSIX_ACL \
  --enable DEBUG_INFO_NONE --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT --disable DEBUG_INFO_DWARF4 --disable DEBUG_INFO_DWARF5 --disable DEBUG_INFO_BTF \
  --set-str LOCALVERSION -jailbee --set-str LSM 'landlock,lockdown,yama,integrity,apparmor,bpf'
make olddefconfig >/dev/null
for o in NF_CONNTRACK_BRIDGE SECURITY_APPARMOR BTRFS_FS BTRFS_FS_POSIX_ACL; do grep -q "^CONFIG_$o=y" .config || { echo "option $o not =y"; exit 1; }; done
START=$(date +%s)
make -j"$(nproc)" Image >/dev/null 2>/var/tmp/make.err || { tail -20 /var/tmp/make.err; exit 1; }
echo "build seconds: $(( $(date +%s) - START ))"
cp arch/arm64/boot/Image "/out/vmlinux-${VERSION}-jailbee.partial"
sha256sum arch/arm64/boot/Image | cut -d' ' -f1 > "/out/vmlinux-${VERSION}-jailbee.sha256"
echo KBUILD_DONE
