#!/usr/bin/env bash
# Refresh the vendored copies of three.js and three-vrm used by the 3D viewer.
# They're kept in the repo so the viewer works offline (e.g. on a home network)
# and watchers' browsers never contact a third-party CDN.
#
#   packages/viewer/vendor/update.sh            # the versions below
#   THREE=0.187.0 VRM=3.6.0 packages/viewer/vendor/update.sh
set -euo pipefail
THREE="${THREE:-0.186.1}"
VRM="${VRM:-3.5.5}"
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cd "$tmp"
npm pack --silent "three@$THREE" "@pixiv/three-vrm@$VRM" >/dev/null
mkdir three vrm
tar xzf "three-$THREE.tgz" -C three
tar xzf "pixiv-three-vrm-$VRM.tgz" -C vrm

rm -rf "$here/three" "$here/three-vrm"
mkdir -p "$here/three/build" "$here/three-vrm"
cp three/package/build/three.module.js three/package/build/three.core.js "$here/three/build/"
cp three/package/LICENSE "$here/three/"
for f in loaders/GLTFLoader.js utils/BufferGeometryUtils.js utils/SkeletonUtils.js \
         controls/OrbitControls.js renderers/CSS2DRenderer.js; do
  mkdir -p "$here/three/examples/jsm/$(dirname "$f")"
  cp "three/package/examples/jsm/$f" "$here/three/examples/jsm/$f"
done
cp vrm/package/lib/three-vrm.module.min.js "$here/three-vrm/"
cp vrm/package/LICENSE "$here/three-vrm/"
printf 'three %s\n@pixiv/three-vrm %s\n' "$THREE" "$VRM" > "$here/VERSIONS"
echo "vendored three $THREE and @pixiv/three-vrm $VRM"
