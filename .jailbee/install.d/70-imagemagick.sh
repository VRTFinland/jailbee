#!/bin/bash
# 70-imagemagick — ImageMagick, for working on the website's images.
# Env: CONTAINER_USER, JAILBEE_USER_HOME
#
# Not needed to regenerate the link-preview card: scripts/make_og_card.py
# uses Pillow, declared inline with PEP 723, and pulls it on demand. This is
# for the one-off work around it — inspecting, cropping and converting the
# images in website/assets/img/ from a shell.
#
# The --path-exclude flag is the entire reason this is a snippet rather than
# one more line in golden.extra_apt_packages, which runs a plain apt-get:
#
#   ImageMagick depends on fonts-urw-base35, xfonts-encodings, xfonts-utils,
#   fonts-droid-fallback and poppler-data. All of them unpack into
#   /usr/share/fonts, which is a READ-ONLY bind mount of the host's fonts in
#   every jailbee container. dpkg cannot write there, errors out, and takes
#   the whole transaction down with it — leaving no ImageMagick at all.
#
# Excluding those paths costs nothing, because the host's fonts are already
# mounted at exactly that location: ImageMagick still sees ~900 of them and
# renders text normally. Verified by installing it this way and drawing a
# text label.
#
# 65-vhs.sh hit the same wall with Chrome and worked around it by extracting
# the .deb into /opt instead. That still works and is not worth changing, but
# --path-exclude is the cheaper fix and would have done the job there too.
#
# Deliberately NOT --no-install-recommends: the flag above is the one doing
# the work, and this combination is the one that was actually verified. If
# the golden image ever needs trimming, that flag is the first thing to try
# here — and the first suspect if an image format later turns up missing.
set -euo pipefail

echo "==> Installing ImageMagick (font paths excluded — see comment)"
apt-get install -y \
    -o Dpkg::Options::="--path-exclude=/usr/share/fonts/*" \
    imagemagick

magick -version | head -1

# Non-fatal: the count is a smoke test, not a gate. If the host's font mount
# is absent at build time the number is 0 and ImageMagick still installs
# fine — it just cannot draw text until a container mounts the fonts.
fonts="$(magick -list font 2>/dev/null | grep -c 'Font:' || true)"
echo "==> ImageMagick sees ${fonts} fonts from the host mount"
