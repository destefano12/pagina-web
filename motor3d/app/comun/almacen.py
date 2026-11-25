"""Almacenamiento de objetos compatible con S3 (SeaweedFS autohospedado, o cualquier S3: Garage, R2, B2, AWS)."""
import hashlib
import json

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from . import config

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client('s3', endpoint_url=config.S3_ENDPOINT, region_name=config.S3_REGION,
                           aws_access_key_id=config.S3_ACCESS_KEY, aws_secret_access_key=config.S3_SECRET_KEY,
                           config=BotoConfig(signature_version='s3v4', s3={'addressing_style': 'path'}, retries={'max_attempts': 5}))
    return _s3


def preparar_buckets():
    """Crea los buckets si faltan. El público solo permite LEER (los modelos publicados que ve el menú)."""
    for b in (config.S3_BUCKET_PRIVADO, config.S3_BUCKET_PUBLICO):
        try:
            s3().head_bucket(Bucket=b)
        except ClientError:
            s3().create_bucket(Bucket=b)
    politica = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'AWS': ['*']}, 'Action': ['s3:GetObject'],
                                                        'Resource': [f'arn:aws:s3:::{config.S3_BUCKET_PUBLICO}/*']}]}
    try:
        s3().put_bucket_policy(Bucket=config.S3_BUCKET_PUBLICO, Policy=json.dumps(politica))
    except ClientError:
        pass  # SeaweedFS da la lectura pública por configuración (identidad "anonymous"), no por política


def subir_bytes(bucket, clave, datos: bytes, tipo='application/octet-stream', cache=None):
    extra = {'ContentType': tipo}
    if cache:
        extra['CacheControl'] = cache
    s3().put_object(Bucket=bucket, Key=clave, Body=datos, **extra)
    return len(datos), hashlib.sha256(datos).hexdigest()


def subir_archivo(bucket, clave, ruta, tipo='application/octet-stream', cache=None):
    with open(ruta, 'rb') as f:
        return subir_bytes(bucket, clave, f.read(), tipo, cache)


def bajar(bucket, clave, ruta):
    s3().download_file(bucket, clave, ruta)


def borrar(bucket, claves):
    claves = list(claves)
    for i in range(0, len(claves), 900):
        s3().delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': k} for k in claves[i:i + 900]], 'Quiet': True})


def leer(bucket, clave) -> bytes:
    return s3().get_object(Bucket=bucket, Key=clave)['Body'].read()


def url_firmada(bucket, clave, segundos=3600):
    return s3().generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': clave}, ExpiresIn=segundos)
