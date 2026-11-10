#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_root=${VIDEO_USE_RUNTIME_ASSETS:-/data2/wujing03/runtime-assets}
target="$repo/runtime/local-assets"
manifest="$repo/runtime/assets-manifest.json"

test -d "$source_root/node_modules"
test -d "$source_root/ms-playwright"
test -x "$source_root/bin/node"
test -f "$source_root/rembg/u2net.onnx"

node - "$source_root" "$manifest" <<'JS'
const fs=require('node:fs'),path=require('node:path');
const [root,manifestFile]=process.argv.slice(2),manifest=JSON.parse(fs.readFileSync(manifestFile));
for(const [name,version] of Object.entries(manifest.node_modules)){
  const actual=JSON.parse(fs.readFileSync(path.join(root,'node_modules',name,'package.json'))).version;
  if(actual!==version)throw Error(`${name} version ${actual} != ${version}`);
}
JS

while IFS=$'\t' read -r relative digest; do
  test "$(sha256sum "$source_root/$relative" | cut -d' ' -f1)" = "$digest"
done < <(jq -r '.files|to_entries[]|[.key,.value]|@tsv' "$manifest")

mkdir -p "$target"
rsync -a --delete "$source_root/bin/" "$target/bin/"
rsync -a --delete "$source_root/node_modules/" "$target/node_modules/"
rsync -a --delete "$source_root/ms-playwright/" "$target/ms-playwright/"
rsync -a --delete "$source_root/rembg/" "$target/rembg/"
echo "Runtime assets staged and verified at $target"
