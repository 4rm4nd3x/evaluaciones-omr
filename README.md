# Evaluaciones OMR API

Servicio de evaluaciones con reconocimiento óptico de marcas (OMR) en Python.
Permite generar hojas de respuestas, escanearlas y evaluarlas automáticamente.

## Arquitectura

```
┌─────────────────────────────────────────────────────┐
│                    FastAPI Server                     │
├──────────┬──────────┬───────────┬───────────────────┤
│evaluar   │generar   │evaluacion │      app/main.py   │
│.py       │.py       │es.py      │                   │
│generica  │          │           │                   │
│.py       │          │           │                   │
├──────────┴──────────┴───────────┤                   │
│        services/                 │                   │
│  ┌─────────────┐ ┌────────────┐ │                   │
│  │omr_processor│ │sheet_gen.  │ │                   │
│  │(detección)  │ │(PDF+coords)│ │                   │
│  └─────────────┘ └────────────┘ │                   │
├─────────────────────────────────┤                   │
│        PostgreSQL + Storage      │                   │
└─────────────────────────────────┴───────────────────┘
```

## Flujo de trabajo

```
1. CREAR evaluación (banco de preguntas) → POST /api/v1/evaluacion
2. AGREGAR secciones (opcional) → POST /api/v1/evaluacion/{id}/secciones
3. AGREGAR preguntas → POST /api/v1/evaluacion/{id}/preguntas/batch  (con seccion_id)
4. GENERAR hojas PDF → POST /api/v1/generar/{id}  con selección (template / estructurada)
   → PDF burbujas + PDF preguntas + coordenadas.json + snapshot del orden
5. IMPRIMIR y LLENAR → El estudiante marca burbujas con lápiz
6. ESCANEAR → Tomar foto o escanear la hoja
7. EVALUAR → POST /api/v1/evaluar (sube PDF/imagen escaneada)
8. VER RESULTADOS → Retorna JSON + imagen anotada + PDF revisado

Alternativa (hoja genérica sin examen):
A. GENERAR → POST /api/v1/generar/generica  (solo burbujas N×M, sin banco)
B. SCANEAR / LLENAR → el estudiante marca solo burbujas
C. LEER → POST /api/v1/leer (devuelve lo llenado sin contrastar contra un banco)
```

## Instalación

### Requisitos
- Python 3.10+
- PostgreSQL 14+
- libzbar (para detección de QR)

### Instalación manual

```bash
# Clonar repositorio
git clone <repo-url>
cd evaluaciones

# Crear entorno virtual
python -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Instalar libzbar (Ubuntu/Debian)
sudo apt-get install libzbar0

# Configurar variables de entorno
cp .env.example .env
# Editar .env con tus datos

# Crear base de datos
createdb evaluaciones
python db_setup.py

# Ejecutar servidor
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Docker (recomendado)

PostgreSQL corre en un servidor externo: el compose **no incluye base de datos**.

```bash
# Clonar repositorio
git clone <repo-url>
cd evaluaciones

# Configurar la conexion al Postgres externo
cp .env.example .env
# Editar .env:
#   DATABASE_URL=postgresql://usuario:password@SERVIDOR_EXTERNO:5432/evaluaciones

# Ejecutar
docker-compose up -d

# Ver logs
docker-compose logs -f app

# Detener
docker-compose down
```

La app crea/migra las tablas automaticamente al arrancar (idempotente, no borra datos).
Los PDFs generados se conservan en el volumen `evaluaciones_data` (o bind mount `./storage`
si se descomenta esa linea en docker-compose.yml).

### Docker solo la app (sin compose)

```bash
# Construir imagen
docker build -t evaluaciones-omr .

# Ejecutar (necesita PostgreSQL externo)
docker run -p 8000:8000 \
  -e DATABASE_URL=postgresql://postgres:postgres@host:5432/evaluaciones \
  -e STORAGE_PATH=/data \
  -v evaluaciones_data:/data \
  evaluaciones-omr
```

## Variables de entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `DATABASE_URL` | URL de conexión PostgreSQL | `postgresql://postgres:postgres@localhost:5432/evaluaciones` |
| `STORAGE_PATH` | Directorio para archivos generados | `/home/armando/archivos` |
| `API_HOST` | Host del servidor | `0.0.0.0` |
| `API_PORT` | Puerto del servidor | `8000` |
| `DEBUG` | Modo debug | `false` |

## API Endpoints

### Evaluaciones

#### Crear evaluación
```bash
POST /api/v1/evaluacion
Content-Type: application/json

{
  "nombre": "Evaluación Matemáticas 2026",
  "descripcion": "Examen final - 50 preguntas"
}
```

**Response:**
```json
{
  "id": "uuid",
  "short_id": "a1b2c3d4",
  "nombre": "Evaluación Matemáticas 2026",
  "descripcion": "Examen final - 50 preguntas"
}
```

#### Listar evaluaciones
```bash
GET /api/v1/evaluaciones
```

#### Obtener evaluación
```bash
GET /api/v1/evaluacion/{evaluacion_id}
```

#### Eliminar evaluación
```bash
DELETE /api/v1/evaluacion/{evaluacion_id}
```

### Secciones (banco de preguntas)

Las secciones agrupan preguntas del banco y permiten seleccionar por cantidad/porcentaje
al generar una hoja.

```bash
# Crear secciones (batch)
POST /api/v1/evaluacion/{evaluacion_id}/secciones
{
  "secciones": [
    {"nombre": "Álgebra", "descripcion": "Sección 1", "orden": 1},
    {"nombre": "Geometría", "descripcion": "Sección 2", "orden": 2}
  ]
}

# Listar secciones
GET /api/v1/evaluacion/{evaluacion_id}/secciones

# Obtener una sección
GET /api/v1/seccion/{seccion_id}

# Actualizar sección (nombre / descripción / orden)
PUT /api/v1/seccion/{seccion_id}
{
  "nombre": "Álgebra lineal",
  "descripcion": "Actualizada",
  "orden": 1
}

# Eliminar sección (y sus preguntas, en cascada)
DELETE /api/v1/seccion/{seccion_id}
```

Cada pregunta se asocia a una sección mediante `seccion_id` (preferido) o,
en su defecto, con el texto legacy `seccion` (se crea/vincula la sección
automáticamente). El script `python db_migrate.py` convierte los datos legacy
(string `seccion` → fila en la tabla `secciones`) de forma idempotente.

### Preguntas

#### Crear preguntas (batch)
```bash
POST /api/v1/evaluacion/{evaluacion_id}/preguntas/batch
Content-Type: application/json

{
  "preguntas": [
    {
      "nombre": "pregunta_1",
      "tipo": "single",
      "enunciado": "¿Cuánto es 2+2?",
      "seccion_id": "uuid-de-la-seccion",  # opcional: FK a la tabla secciones
      "opciones": [
        {"key": "A", "label": "3", "es_correcta": false},
        {"key": "B", "label": "4", "es_correcta": true},
        {"key": "C", "label": "5", "es_correcta": false},
        {"key": "D", "label": "6", "es_correcta": false}
      ],
      "orden": 1,
      "puntos": 1.0,
      "requerida": true,
      "seccion": "Aritmética"
    },
    {
      "nombre": "pregunta_2",
      "tipo": "multiple",
      "enunciado": "¿Cuáles son números primos?",
      "opciones": [
        {"key": "A", "label": "2", "es_correcta": true},
        {"key": "B", "label": "4", "es_correcta": false},
        {"key": "C", "label": "7", "es_correcta": true},
        {"key": "D", "label": "9", "es_correcta": false}
      ],
      "orden": 2,
      "puntos": 2.0
    }
  ]
}
```

> Cada pregunta debe tener entre **2 y 5 opciones** (error 400 en caso contrario).

```bash
# Listar preguntas de un banco (ordenadas por sección → pregunta)
GET /api/v1/evaluacion/{evaluacion_id}/preguntas

# Eliminar una pregunta (junto con sus opciones)
DELETE /api/v1/pregunta/{pregunta_id}
```

### Generar hoja de respuestas

```bash
POST /api/v1/generar/{evaluacion_id}
Content-Type: application/json

{
  "seleccion": {
    "tipo": "template",
    "cantidad_preguntas": 30,
    "secciones": [
      {"seccion_id": "uuid", "cantidad": 10},
      {"seccion_id": "uuid", "porcentaje": 40.0}
    ],
    "aleatorio": true
  }
}
```

| Parámetro | Tipo | Default | Descripción |
|-----------|------|---------|-------------|
| `identificador` | string | auto-generado | Identificador de la hoja (D4); si falta se genera `{short_id}-{n}` |
| `seleccion` | object | — | Selección de preguntas (template o estructurada, ver abajo) |
| `seleccion.tipo` | `"template"` \| `"estructurada"` | `"estructurada"` | Modo de selección |
| `seleccion.cantidad_preguntas` | int | — | (template) Total de preguntas; no puede exceder el banco (error 400) |
| `seleccion.secciones` | list | `null` | (template) Reparto por sección: cada item lleva `seccion_id` + `cantidad` **o** `porcentaje` |
| `seleccion.aleatorio` | bool | `false` | (template) Selección y orden aleatorios; las secciones quedan intercaladas |
| `seleccion.preguntas_orden` | list[UUID] | `null` | (estructurada) Subconjunto ordenado explícito; si falta, se usa el banco completo |
| `cantidad_preguntas` | int | `null` | **Legacy/back-compat:** recorte del banco (equivale a `template`) |
| `aleatorio` | bool | `false` | **Legacy/back-compat** |

**Template (caso 1):** se define `cantidad_preguntas` y opcionalmente el reparto por
secciones con `cantidad` (número exacto) o `porcentaje` (proporción del total). El
sistema respeta los topes por sección y redondea con el método del mayor resto, de modo
que el total siempre es exacto y **no sobrepasa las preguntas disponibles**.

**Estructurada (caso 2):** el banco completo se imprime en su orden (`seccion.orden`,
`pregunta.orden`), o solo las preguntas listadas en `preguntas_orden` si se indica.

**Response:**
```json
{
  "hoja_id": "uuid",
  "identificador": "a1b2c3d4-1",
  "qr_data": {"short_id": "a1b2c3d4", "identificador": "a1b2c3d4-1"},
  "pdf_base64": "JVBERi0xLjQg...",
  "hoja_preguntas_base64": "JVBERi0xLjQg...",
  "cantidad_preguntas": 30,
  "config_seleccion": {
    "tipo": "template",
    "cantidad_preguntas": 30,
    "secciones": [{"seccion_id": "uuid", "cantidad": 10, "porcentaje": null}],
    "aleatorio": true
  }
}
```

- `pdf_base64`: Hoja de **respuestas** (burbujas para marcar)
- `hoja_preguntas_base64`: Hoja de **preguntas** (texto completo con instrucciones)
- `qr_data.identificador`: el QR impreso embebe el identificador para resolver la variante al escanear
- `cantidad_preguntas`: preguntas efectivamente impresas en la hoja
- `config_seleccion`: snapshot de la selección (reparto por secciones, orden), no persistido en los PDFs

**Archivos generados en STORAGE_PATH:**
- `hojas/{hash}/hoja_respuestas.pdf` — PDF de la hoja de burbujas
- `hojas/{hash}/hoja_preguntas.pdf` — PDF de la hoja de preguntas
- `hojas/{hash}/hojas_resultado.pdf` — Hoja de resultados (clave): la hoja de respuestas con las burbujas correctas rellenas y el banner "CLAVE DE RESULTADOS". **Solo se entrega en base64** vía `GET /resultados/{identificador}`
- `hojas/{hash}/coordenadas.json` — Coordenadas de burbujas para detección OMR
- `hojas/{hash}/preguntas_orden.json` — Snapshot del orden de preguntas de esta variante

#### Variantes aleatorias (snapshot de orden)

Cada hoja registra en BD (`hojas_respuesta.preguntas_orden`) y en storage la lista exacta
de preguntas impresas y su orden:

- **Reimpresión estable**: llamar de nuevo a `/generar` con el mismo `identificador` y sin
  parámetros devuelve siempre la misma variante (mismo orden). Si se envían parámetros,
  se genera una variante nueva.
- **Evaluación coherente**: al escanear, el QR resuelve el `identificador` y las respuestas
  detectadas se mapean según el snapshot, aunque el orden sea aleatorio.
- También puede pasarse `identificador` explícito en el request de `/evaluar` como
  desambiguador manual.

```bash
# Listar las hojas generadas de una evaluación (recientes primero)
GET /api/v1/evaluacion/{evaluacion_id}/hojas
```
Cada item incluye `identificador`, `qr_data`, `cantidad_preguntas` y el `config_seleccion`
(snapshot de cómo se seleccionó esa variante).

### Hoja genérica (sin examen)

Genera una hoja de respuestas con solo burbujas (N preguntas × M opciones, 2–5),
sin asociarla a ninguna evaluación. El QR embebe `{"tipo": "generica", ...}`.

```bash
POST /api/v1/generar/generica
Content-Type: application/json

{
  "identificador": "GEN-001",
  "cantidad_preguntas": 50,
  "opciones": "ABCDE"        # o lista: ["A", "B", "C"] — en ambos casos decide num_opciones
}
```

**Response:** (`GenerarGenericaResponse`)
```json
{
  "hoja_id": "uuid",
  "identificador": "GEN-001",
  "num_opciones": 5,
  "qr_data": {"tipo": "generica", "identificador": "GEN-001"},
  "pdf_base64": "JVBERi0xLjQg..."
}
```

La reimpresión con el mismo `identificador` devuelve la misma hoja (estable).
Las páginas usan una grilla de 5 posiciones por fila; solo se dibujan las burbujas
reales de cada pregunta (2–5) y el encabezado de columnas se ajusta por pregunta.

```bash
# Recuperar una hoja genérica (PDF en base64)
GET /api/v1/generica/{identificador}

# Eliminar una hoja genérica (registro + archivos de storage)
DELETE /api/v1/generica/{identificador}
```

#### Leer hoja genérica escaneada

```bash
POST /api/v1/leer
Content-Type: application/json

{
  "imagen_escaneada_base64": "iVBORw0KGgo...",
  "identificador": "GEN-001"   # opcional: el QR la resuelve automáticamente
}
```

Devuelve **lo llenado sin contrastar contra ningún banco** (`LecturaGenericaLoteResponse`):
```json
{
  "total_paginas": 1,
  "paginas_leidas": 1,
  "paginas_ignoradas": 0,
  "lecturas": [
    {
      "identificador": "GEN-001",
      "identificador_persona": "6185178",
      "cantidad_preguntas": 50,
      "num_opciones": 5,
      "respuestas": [
        {"pregunta": 1, "opciones_seleccionadas": ["A"]},
        {"pregunta": 2, "opciones_seleccionadas": ["B", "D"]}
      ],
      "qr_codes": ["{\"tipo\": \"generica\", \"identificador\": \"GEN-001\"}"]
    }
  ],
  "errores": []
}
```

> Si se sube una hoja genérica a `/evaluar`, responde `400` indicando que debe
> usarse `/leer`, ya que no hay banco contra el cual calificar.

### Evaluar hoja escaneada

Procesa un PDF (todas las páginas) o una imagen. Cada página con un QR válido se
evalúa como una hoja independiente; las páginas sin QR se ignoran y se reportan.

```bash
POST /api/v1/evaluar
Content-Type: application/json

{
  "pdf_escaneado_base64": "JVBERi0xLjQg...",
  "identificador": "Eva-2026-001"
}
```

o con imagen:
```json
{
  "imagen_escaneada_base64": "iVBORw0KGgo..."
}
```

**Response** (`ResultadoLoteResponse`):
```json
{
  "total_paginas": 3,
  "paginas_evaluadas": 2,
  "paginas_ignoradas": 1,
  "resultados": [
    {
      "resultado_id": "uuid",
      "identificador": "Eva-2026-001",
      "identificador_persona": "6185178",
      "puntuacion_total": 8.5,
      "total_preguntas": 13,
      "respuestas_correctas": 11,
      "respuestas": [
        {
          "nombre": "pregunta_1",
          "pregunta_id": "uuid",
          "respuesta": "B",
          "es_correcta": true,
          "puntos_obtenidos": 1.0
        }
      ],
      "pdf_revisado_base64": "JVBERi0xLjQg...",
      "imagen_anotada_base64": "iVBORw0KGgo...",
      "qr_codes": ["d8de9d9c"],
      "storage_path": "/home/armando/archivos/{hash}"
    }
  ],
  "errores": [
    {"pagina": 1, "detalle": "Página sin código QR"}
  ]
}
```

Si **ninguna** página es evaluable responde `400` con el detalle por página.

Notas:
- El QR de la hoja de respuestas incluye el `identificador`; no hace falta pasarlo
  en el request salvo para hojas antiguas cuyo QR solo trae el `short_id`.
- Un PDF puede mezclar hojas de **evaluaciones distintas**: cada página se resuelve
  por su propio QR.
- Las marcas de esquina permiten enderezar escaneos inclinados (hasta ~±7°);
  la metadata en storage registra `enderezada`, `angulo` y `lectura_por_coords`.

### Resultados

#### Obtener hoja de resultados (clave)
```bash
GET /api/v1/resultados/{identificador}
GET /api/v1/resultados/{identificador}?short_id=a1b2c3d4
GET /api/v1/resultados/{identificador}?evaluacion_id=uuid
```

**Response:**
```json
{
  "identificador": "Eva-2026-001",
  "evaluacion_id": "uuid",
  "cantidad_preguntas": 30,
  "pdf_base64": "JVBERi0xLjQg..."
}
```

Muestra el llenado correcto de la hoja de respuestas (burbujas correctas rellenas).
Si el `identificador` existe en varias evaluaciones, desambiguar con `short_id` o
`evaluacion_id`. Si el archivo no existe en storage, se regenera automáticamente a
partir del snapshot de la hoja.

#### Obtener resultado
```bash
GET /api/v1/resultado/{resultado_id}
```
Devuelve el resultado completo: puntuación, detalle por pregunta (en el orden del banco)
y los archivos `pdf_revisado_base64` e `imagen_anotada_base64` (si existen en storage).

#### Listar resultados de una evaluación
```bash
GET /api/v1/evaluacion/{evaluacion_id}/resultados
```
Lista un resumen de los resultados (sin detalle), del más reciente al más antiguo.

#### Eliminar un resultado
```bash
DELETE /api/v1/resultado/{resultado_id}
```
Elimina el resultado y su detalle de la BD (los archivos en storage se conservan).

## Detección OMR

El motor OMR usa **3 niveles de detección** en orden de prioridad:

### 1. Coordenadas conocidas (JSON)
Cuando se genera la hoja con `POST /generar`, se guarda `coordenadas.json` con la posición exacta de cada burbuja. Si el escaneo es de la misma hoja, se usan estas coordenadas para detección perfecta.

### 2. HoughCircles (fallback principal)
Detección de círculos directa con `cv2.HoughCircles`. Funciona bien con escaneos reales (fotos, scans). Detecta burbujas por su forma circular sin depender de contornos.

### 3. Contornos (último fallback)
Detección de bordes con `cv2.findContours` + filtrado por circularidad. Funciona mejor con PDFs renderidos limpios.

### Detección de ID Persona
- Grid 10×10 (10 filas × 10 columnas para dígitos 0-9)
- Cada fila representa un dígito del ID
- Se marca la burbuja correspondiente al dígito deseado
- IDs cortos: las filas sin marca se omiten (trim automático)

### Detección de respuestas
- Layout de **4 columnas** de preguntas (~120 preguntas por hoja; el paso vertical se compacta automáticamente si hay más filas)
- Cada pregunta dibuja **entre 2 y 5 burbujas** en una grilla de hasta 5 posiciones
  (alineadas a la izquierda); el encabezado de columnas se ajusta por pregunta (D6)
- Preguntas **single**: solo una respuesta correcta
- Preguntas **multiple**: múltiples respuestas correctas

> Nota: las hojas generadas con **menos de 5 opciones** (p. ej. `ABC`) ya no dibujan
> burbujas vacías a la derecha; la calificación y las coordenadas mapean por posición
> (`option_index` 0=A, 1=B, ...).

### Imagen anotada
La imagen anotada muestra:
- 🟢 **V verde** — Respuesta marcada = correcta
- 🔴 **X rojo** — Respuesta marcada ≠ correcta
- 🟡 **Círculo amarillo** — Respuesta correcta pero no marcada

## Layout de la hoja de respuestas

```
┌─                                                            ─┐
│ L ┌────────┐  Evaluación 2026              ID PERSONA        │
│   │  QR    │  Descripción breve          ▓▓████████▓▓       │
│   └────────┘  ID: Eva-001 Fecha: ...      ┌──────────────┐   │
│                                           │ 0 1 2 3 4 ...│   │
│ ┌─ INSTRUCCIONES DE LLENADO ──────────┐   │ ● ○ ○ ○ ○ ...│   │
│ │ LÁPIZ: HB o #2...                   │   │ ○ ● ○ ○ ○ ...│   │
│ │ ID PERSONA: Una columna por dígito. │   └──────────────┘   │
│ │ SIMPLES / MÚLTIPLES: ...            │                      │
│ │           EJEMPLO - ID 45689 (grid) │                      │
│ └─────────────────────────────────────┘                      │
│ ───────────────────────────────────────────────────────────  │
│ ▓RESPUESTAS▓                                                 │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ A B C D E   A B C D E    A B C D E    A B C D E         │ │
│ │ 1.○○○○○     31.○○○○○     61.○○○○○     91.○○○○○          │ │
│ │ 2.○○○○○     32.○○○○○     62.○○○○○     92.○○○○○          │ │
│ │ ...                                                         │ │
│ │30.○○○○○     60.○○○○○     90.○○○○○    120.○○○○○          │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                            L ┘
```

- Encabezado con QR, nombre, descripción y fecha; rejilla **ID PERSONA** a la derecha.
- **Marcas de esquina (■)**: cuadrados negros en las 4 esquinas que permiten enderezar
  el escaneo; la lectura se hace sobre la imagen original mapeando las coordenadas
  conocidas con la transformación estimada.
- Caja de **instrucciones de llenado** debajo del QR, con ejemplo visual de marcado del ID
  (mini-rejilla con las burbujas del ID de ejemplo rellenas).
- Títulos de sección tipo *chip* (banda negra con texto blanco): `ID PERSONA`, `RESPUESTAS`.
- Respuestas en **4 columnas** dentro de un recuadro.

### Marcas de alineación
- **Esquinas**: Paréntesis L (líneas finas) para alineación general — solo en la hoja de respuestas
- **ID Persona**: Rectángulo sólido alrededor del grid
- **Respuestas**: Rectángulo sólido alrededor de las 4 columnas

> La hoja de preguntas no se escanea, por lo que no incluye marcas de esquina.

## Layout de la hoja de preguntas

- Encabezado con nombre, descripción, identificador y fecha.
- Caja de instrucciones con ejemplo visual del llenado del ID (igual que la hoja de respuestas).
- Preguntas en **2 columnas por página** (flujo izquierda → derecha → página siguiente);
  la última página se balancea entre ambas columnas y el margen superior de las páginas
  siguientes es compacto.
- Enunciados completos en **negrilla**; número de pregunta en su propio canal.
- Secciones como bandas grises con texto en mayúsculas, con separación respecto a las preguntas.
- Pie de página con numeración (`Página X de Y`).
- Los PDFs incluyen metadatos de título (`Hoja de preguntas - {nombre}`, etc.), evitando el "untitled" en los visores.

## Tecnologías

| Componente | Tecnología |
|------------|------------|
| Framework | FastAPI |
| ORM | SQLAlchemy |
| Base de datos | PostgreSQL |
| OCR/Detección QR | pyzbar |
| Procesamiento de imágenes | OpenCV (cv2) |
| Generación de PDFs | ReportLab |
| Conversión PDF | PyMuPDF (fitz) |
| Detección de círculos | OpenCV HoughCircles |

## Estructura del proyecto

```
evaluaciones/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app (routers + migraciones ligeras)
│   ├── config.py             # Variables de entorno
│   ├── database.py           # Conexión PostgreSQL
│   ├── models.py             # Modelos SQLAlchemy
│   ├── schemas.py            # Schemas Pydantic
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── evaluaciones.py   # CRUD evaluaciones, secciones y preguntas
│   │   ├── generar.py        # Generar hojas PDF (template / estructurada)
│   │   ├── generica.py       # Hoja genérica (solo burbujas) + /leer
│   │   └── evaluar.py        # Evaluar hojas escaneadas
│   └── services/
│       ├── __init__.py
│       ├── omr_processor.py  # Motor de detección OMR
│       └── sheet_generator.py # Generador de PDFs + coordenadas
├── demo/
│   ├── evaluacion_1_preguntas.csv
│   ├── evaluacion_2_preguntas.csv
│   └── output/               # Archivos generados por demos
├── docs/
│   └── openapi.json          # Documentación OpenAPI/Swagger generada
├── test_scan.py              # Test de escaneo OMR
├── test_coordenadas.py       # Test con coordenadas conocidas
├── test_ids_demo.py          # Test de IDs prellenados
├── demo.py                   # Demo completa
├── db_setup.py               # Setup de BD (drop + create)
├── db_migrate.py             # Migración idempotente a secciones
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env
```

## Troubleshooting

### La detección no funciona bien
- Verificar que el escaneo sea legible (buena resolución, sin sombras)
- El QR debe estar visible para identificar la evaluación
- Usar lápiz oscuro (HB o #2) para marcar burbujas
- La inclinación soportada es de hasta ~±7° (corrección por marcas de esquina);
  escaneos más torcidos o con perspectiva pueden requerir recolocar la hoja

### Hojas generadas antes del cambio del QR
- Si el QR solo contiene el `short_id` (hojas antiguas), pasar `identificador`
  en el request de `/evaluar` para que se resuelvan las coordenadas conocidas;
  o regenerar las hojas para que el QR incluya el identificador

### Error "No se detectaron códigos QR"
- Verificar que el QR esté bien impreso y visible
- La imagen debe tener al menos 300 DPI para QR confiables

### Docker no conecta a PostgreSQL
- Verificar que `DATABASE_URL` en `.env` apunte al servidor externo correcto
- El host debe ser alcanzable desde el contenedor (en Linux usar
  `--add-host=host.docker.internal:host-gateway` con docker run)
- Verificar que el Postgres externo acepte conexiones remotas (`listen_addresses`
  y `pg_hba.conf`)
