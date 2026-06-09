# Punto Rojo API

Servidor FastAPI que procesa facturas XML DIAN para la app Next.js.

## Endpoints

- `GET /` — Health check
- `POST /parse-invoice` — Recibe ZIP/XML, devuelve datos procesados
- `POST /save-invoice` — Guarda factura y productos en Supabase

## Subir a Railway (gratis)

1. Ve a railway.app y crea cuenta
2. New Project → Deploy from GitHub
3. Sube esta carpeta a un repo de GitHub
4. Agrega las variables de entorno:
   - SUPABASE_URL
   - SUPABASE_SERVICE_KEY
5. Railway te da un URL como: https://punto-rojo-api.up.railway.app

## Correr localmente

```bash
pip install -r requirements.txt
cp .env.example .env
# Llena el .env con tus claves
python main.py
```

Luego abre: http://localhost:8000/docs
