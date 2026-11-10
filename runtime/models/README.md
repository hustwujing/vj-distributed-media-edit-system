# Runtime model assets

`u2net.onnx` is a local build asset for offline `rembg` CPU inference. It is not
committed because of its size. Prepare it before building runtime images:

```bash
mkdir -p runtime/models
curl -L --fail -o runtime/models/u2net.onnx \
  https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx
test "$(md5sum runtime/models/u2net.onnx | cut -d' ' -f1)" = \
  60024c5c889badc19c04ad937298a77b
```

The image stores the verified model at `/opt/models/rembg/u2net.onnx` and sets
`U2NET_HOME=/opt/models/rembg`. Runtime inference must not download models.
