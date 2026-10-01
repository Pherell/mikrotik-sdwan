#!/usr/bin/env bash
# Build the MikroTik CHR container image the lab runs on, and optionally push it.
#
#   labs/build_chr_image.sh                                   # build 7.14.3 locally
#   labs/build_chr_image.sh --version 7.16.2
#   labs/build_chr_image.sh --push ghcr.io/<owner>/mikrotik_ros
#
# CHR is a VM image, not a container. vrnetlab (https://github.com/srl-labs/vrnetlab)
# wraps it in a container that boots it under QEMU, which is what containerlab's
# mikrotik_ros kind expects. Nobody publishes that image, so it has to be built:
#
#   1. download chr-<version>.vmdk.zip from download.mikrotik.com;
#   2. clone vrnetlab at a pinned ref and run its routeros `make docker-image`;
#   3. tag the result as the name labs/hub-spoke.clab.yml uses
#      (vrnetlab/mikrotik_ros:<version>);
#   4. with --push, also tag and push <repo>:<version>.
#
# Building needs Docker, git, make, curl and unzip -- not KVM; only *running*
# the image needs /dev/kvm. Pushing needs a prior `docker login` (for GHCR, a
# token with write:packages: `echo "$TOKEN" | docker login ghcr.io -u <user>
# --password-stdin`). Once pushed, set the repository variable LAB_CHR_IMAGE to
# <repo>:<version> and the Lab workflow pulls it instead of building.
#
# The CHR free licence caps throughput at 1 Mbps. Redistributing the image
# publicly is between you and MikroTik's licence terms: push to a *private*
# package unless you have checked.

set -euo pipefail

VERSION="${CHR_VERSION:-7.14.3}"
URL="${CHR_URL:-}"
SHA256="${CHR_SHA256:-}"
VRNETLAB_REPO="${VRNETLAB_REPO:-https://github.com/srl-labs/vrnetlab.git}"
# Pinned: vrnetlab moves its directory layout between releases (routeros/ ->
# mikrotik/routeros/) and changes image names; a floating master would break
# this script without anyone touching it.
VRNETLAB_REF="${VRNETLAB_REF:-v0.21.0}"
LOCAL_REPO="vrnetlab/mikrotik_ros"
PUSH_REPO=""
KEEP_WORKDIR=0

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Options:
  --version V        RouterOS version (default: $VERSION, or \$CHR_VERSION)
  --url URL          CHR vmdk(.zip) URL (default: MikroTik's download for --version)
  --sha256 HEX       expected SHA-256 of the download (optional)
  --vrnetlab-ref R   vrnetlab git ref (default: $VRNETLAB_REF)
  --local-repo R     local image repository (default: $LOCAL_REPO)
  --push REPO        also tag and push REPO:<version>, e.g. ghcr.io/me/mikrotik_ros
  --keep             keep the work directory
  -h, --help         this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --sha256) SHA256="$2"; shift 2 ;;
    --vrnetlab-ref) VRNETLAB_REF="$2"; shift 2 ;;
    --local-repo) LOCAL_REPO="$2"; shift 2 ;;
    --push) PUSH_REPO="$2"; shift 2 ;;
    --keep) KEEP_WORKDIR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?(beta[0-9]+|rc[0-9]+)?$ ]]; then
  echo "error: '$VERSION' does not look like a RouterOS version" >&2
  exit 64
fi
URL="${URL:-https://download.mikrotik.com/routeros/${VERSION}/chr-${VERSION}.vmdk.zip}"
# Registries reject upper-case repository names, and GitHub owners often have them.
PUSH_REPO="$(printf '%s' "$PUSH_REPO" | tr '[:upper:]' '[:lower:]')"

for tool in docker git make curl unzip; do
  command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 69; }
done

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/chr-build.XXXXXX")"
cleanup() {
  if [[ "$KEEP_WORKDIR" == 1 ]]; then
    echo "work directory kept: $WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

echo "==> downloading $URL"
archive="$WORKDIR/$(basename "$URL")"
curl -fsSL --retry 3 -o "$archive" "$URL"
if [[ -n "$SHA256" ]]; then
  echo "$SHA256  $archive" | sha256sum -c -
fi

case "$archive" in
  *.zip) unzip -q -o "$archive" -d "$WORKDIR/disk" ;;
  *.vmdk) mkdir -p "$WORKDIR/disk" && mv "$archive" "$WORKDIR/disk/" ;;
  *) echo "error: expected a .vmdk or .vmdk.zip, got $archive" >&2; exit 65 ;;
esac
vmdk="$(find "$WORKDIR/disk" -name '*.vmdk' | head -n1)"
[[ -n "$vmdk" ]] || { echo "error: no .vmdk inside $archive" >&2; exit 65; }
# vrnetlab derives the image tag from the file name (chr-<version>.vmdk).
want="chr-${VERSION}.vmdk"
[[ "$(basename "$vmdk")" == "$want" ]] || mv "$vmdk" "$WORKDIR/disk/$want"

echo "==> cloning vrnetlab @ $VRNETLAB_REF"
git clone --quiet --depth 1 --branch "$VRNETLAB_REF" "$VRNETLAB_REPO" "$WORKDIR/vrnetlab"

builddir=""
for candidate in mikrotik/routeros routeros; do
  if [[ -f "$WORKDIR/vrnetlab/$candidate/Makefile" ]]; then
    builddir="$WORKDIR/vrnetlab/$candidate"
    break
  fi
done
[[ -n "$builddir" ]] || { echo "error: no routeros Makefile in vrnetlab $VRNETLAB_REF" >&2; exit 70; }
cp "$WORKDIR/disk/$want" "$builddir/"

echo "==> building with vrnetlab ($builddir)"
before="$(docker images --format '{{.Repository}}:{{.Tag}}' | sort)"
make -C "$builddir" docker-image
after="$(docker images --format '{{.Repository}}:{{.Tag}}' | sort)"

# vrnetlab has named this image vr-routeros, mikrotik_routeros and, on newer
# releases, <version>-amd64 as well; find what this ref produced rather than
# guessing.
built="$(comm -13 <(echo "$before") <(echo "$after") | grep -E "routeros:${VERSION}(-amd64)?$" | head -n1 || true)"
if [[ -z "$built" ]]; then
  built="$(echo "$after" | grep -E "/(vr-routeros|mikrotik_routeros):${VERSION}(-amd64)?$" | head -n1 || true)"
fi
[[ -n "$built" ]] || { echo "error: could not find the image vrnetlab built" >&2; exit 70; }
echo "==> built $built"

docker tag "$built" "${LOCAL_REPO}:${VERSION}"
echo "==> tagged ${LOCAL_REPO}:${VERSION}"

if [[ -n "$PUSH_REPO" ]]; then
  docker tag "$built" "${PUSH_REPO}:${VERSION}"
  docker push "${PUSH_REPO}:${VERSION}"
  echo "==> pushed ${PUSH_REPO}:${VERSION}"
  echo "    set the repository variable LAB_CHR_IMAGE=${PUSH_REPO}:${VERSION}"
fi
