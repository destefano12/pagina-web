#!/bin/sh
# Arranca SeaweedFS con API S3. Las claves salen del entorno; el bucket público queda solo de lectura para todos.
set -e
PUB="${S3_BUCKET_PUBLICO:-motor3d-modelos}"
printf '{"identities":[{"name":"motor","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]},{"name":"anonymous","actions":["Read:%s"]}]}' \
  "$S3_ACCESS_KEY" "$S3_SECRET_KEY" "$PUB" > /tmp/s3.json
exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/tmp/s3.json -master.volumeSizeLimitMB=1024 -volume.max=0
