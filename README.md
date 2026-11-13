# trace-lens

Hallazgos sobre las trazas, estilo "Watchdog": qué endpoint o dependencia empezó a fallar, a tardar o
a recibir menos tráfico que lo habitual, **desde cuándo**, **dónde nace el error** y **si coincide con
un deploy**.

## Cómo funciona

Cada 2 minutos:

1. **Pide los números a Prometheus** (las spanmetrics que el metrics-generator de Tempo escribe):
   por servicio, endpoint (spans de servidor) y dependencia (spans de cliente: AWS, Mongo, HTTP),
   requests, errores y p95 de los últimos 10 min, de las 24 h anteriores y de la misma ventana ayer.
   Prometheus hace las cuentas; acá solo se comparan.
2. **Detecta** (`detect.py`):
   - error muy por encima de lo habitual (o errores que no existían);
   - respuestas 4xx que no son error OTel (en el servidor nunca lo son) muy por encima de lo habitual,
     con el código que predomina y lo que suele querer decir (404: ruta o recurso que no existe; 401:
     token vencido; 403: sin permiso);
   - p95 1,5 veces lo habitual y al menos 100 ms más;
   - caída de tráfico de más del 70% contra ayer a esta hora (solo endpoints).
   Con un mínimo de tráfico, para no reportar ruido.
3. **Explica lo nuevo** (`engine.py`, `explain.py`), una vez y cada 15 min mientras siga:
   - desde cuándo, recorriendo la serie de las últimas 3 h;
   - de dónde nacen los errores: lee hasta 10 trazas con error en Tempo y agrupa el span más profundo
     con error de cada una (servicio, operación, mensaje normalizado);
   - qué deploy coincide: corridas de Lazy y builds/deploys de stack-lens (`/changes`), hasta 30 min
     antes del inicio;
   - en qué dato se concentran las fallas (errores y 4xx): lo mismo que `/examine`, con 50 trazas por
     muestra.
4. **Guarda** el hallazgo con una frase en español. Se cierra solo cuando vuelve a lo normal.

Las frases se arman con plantillas. Un LLM puede redactarlas mejor o sugerir búsquedas, pero recibe
estos hallazgos ya calculados, nunca datos crudos.

## API

Escucha en `127.0.0.1:7500`.

| Endpoint | Para qué |
|---|---|
| `GET /insights?state=active\|resolved\|all&service=&hours=24` | Hallazgos, abiertos primero y por severidad |
| `GET /examine?service=&endpoint=&kind=server\|client&minutes=60&limit=100` | Con qué valores reales se llamó al endpoint y en cuáles se concentran las fallas |
| `GET /health` | Cantidades y estado del detector (última corrida, último error, fuentes de deploys) |

`/examine` busca en Tempo hasta `limit` trazas que fallan (error o código ≥ 400) y otras tantas que
andan, solo con los atributos que hacen falta (`select`). Separa cada dato: los `{param}` de la ruta
(`http.route` contra `http.target`; sin plantilla, los segmentos que parecen ids), el query string
(los valores de claves como `token`, `key` o `password` se enmascaran) y algunos atributos (tabla de
DynamoDB, colección, host). Un valor "concentra" las fallas si está en la mitad o más de las que
fallan y al menos 30 puntos menos en las que andan.

Cada hallazgo trae `summary`, `started_at`, `value`/`baseline`, `causes`, `deploy` y `search` (los
filtros para abrir sus trazas en el módulo Trazas de CloudPanel).

## Despliegue

En monobuntu, desde la raíz del repo: `bash scripts/deploy.sh`.

Los deploys de Lazy son opcionales: `LAZY_URL` y `LAZY_TOKEN` en `~/monitoring_agent/poc/trace-lens.env`
(los carga el usuario). Sin eso se usan solo los de stack-lens.

| Variable | Valor por defecto |
|---|---|
| `PROMETHEUS_URL` | obligatoria |
| `TEMPO_URL` | obligatoria |
| `TEMPO_HOST_HEADER` | — (si Tempo está detrás de un ALB que enruta por nombre y se le pega por IP) |
| `STACK_LENS_URL` | — |
| `LAZY_URL`, `LAZY_TOKEN` | — |
| `POLL_SECONDS` | `120` |
| `WINDOW_SECONDS` | `600` |

## Tests

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]" && .venv/Scripts/python -m pytest
```
