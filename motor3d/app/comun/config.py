"""Configuración del motor: todo sale de variables de entorno (ver .env.example)."""
import os


def _e(k, d=None):
    return os.environ.get(k, d)


DATABASE_URL = _e('DATABASE_URL', 'postgresql://motor:motor@postgres:5432/motor3d')
REDIS_URL = _e('REDIS_URL', 'redis://redis:6379/0')

S3_ENDPOINT = _e('S3_ENDPOINT', 'http://almacen:8333')
S3_REGION = _e('S3_REGION', 'us-east-1')
S3_ACCESS_KEY = _e('S3_ACCESS_KEY', '')
S3_SECRET_KEY = _e('S3_SECRET_KEY', '')
S3_BUCKET_PRIVADO = _e('S3_BUCKET_PRIVADO', 'motor3d-trabajos')   # fotos, registros, versión maestra
S3_BUCKET_PUBLICO = _e('S3_BUCKET_PUBLICO', 'motor3d-modelos')    # modelos publicados y miniaturas
URL_PUBLICA_MODELOS = _e('URL_PUBLICA_MODELOS', 'http://localhost:8080/modelos')  # cómo los ve el menú

# autenticación: el panel manda el token de sesión del restaurante (Supabase Auth)
SUPABASE_URL = _e('SUPABASE_URL', '').rstrip('/')
SUPABASE_ANON_KEY = _e('SUPABASE_ANON_KEY', '')
SUPABASE_JWT_SECRET = _e('SUPABASE_JWT_SECRET', '')  # solo si el proyecto usa claves HS256 viejas
MODO_PRUEBA = _e('MODO_PRUEBA', '0') == '1'           # SOLO para pruebas automáticas: tokens firmados con PRUEBA_SECRETO
PRUEBA_SECRETO = _e('PRUEBA_SECRETO', '')

ORIGENES_PERMITIDOS = [o.strip() for o in _e('ORIGENES_PERMITIDOS', 'http://localhost:8798').split(',') if o.strip()]

# límites
MAX_FOTOS = int(_e('MAX_FOTOS', '150'))
MIN_FOTOS = int(_e('MIN_FOTOS', '20'))
MAX_MB_FOTO = int(_e('MAX_MB_FOTO', '20'))
MAX_TRABAJOS_ACTIVOS = int(_e('MAX_TRABAJOS_ACTIVOS', '3'))   # por restaurante
MAX_PEDIDOS_MINUTO = int(_e('MAX_PEDIDOS_MINUTO', '240'))     # por usuario
DIAS_GUARDAR_FOTOS = int(_e('DIAS_GUARDAR_FOTOS', '7'))       # después de publicar o descartar
