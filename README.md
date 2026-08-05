# Google Photos Takeout Deduplicator

Herramienta para **detectar y consolidar fotos/videos duplicados** de m�ltiples exportaciones de **Google Takeout (Google Photos)**.  
Soporta detecci�n por **hash exacto** (SHA256) y por **hash perceptual** (pHash) para encontrar duplicados "visualmente iguales".

- Consolida en **buckets seguros** (`UNIQUE` / `DUPLICATES_EXACT` / `REVIEW_PERCEPTUAL` / `REVIEW_DATE`)  
- (v3) Organiza la salida por **a�o de captura** con evidencia de fechas auditada (sidecars Takeout, EXIF, nombre, mtime)  
- Genera reportes en **CSV / JSON / XLSX (Excel)** + **auditor�a por archivo** (`all_files_audit.csv`)  
- Modo seguro **dry-run**: plan completo (destinos, colisiones, espacio real) sin copiar ni mover nada  
- **Metadata opcional**: modo `audit` (default, no requiere nada) o `write` (escribe DateTimeOriginal v�a ExifTool) sobre las copias

---

## ¿Qué problema resuelve?

Si tenés varias cuentas de Google Photos o varios Takeouts (por ejemplo, cuentas viejas / nuevas), es común terminar con miles de fotos repetidas entre exportaciones.

Este proyecto:

1. **Escanea** tus Takeouts  
2. **Detecta** duplicados (exactos y/o perceptuales)  
3. **Construye un plan inmutable** (Fase 5): por cada archivo decide clasificaci�n, bucket, a�o, destino y colisiones, con invariantes de seguridad validadas antes de ejecutar  
4. Copia (o mueve) seg�n el plan a:
   - `UNIQUE/` → lo que te vas a quedar (�nicos + winners con fecha s�lida)
   - `DUPLICATES_EXACT/` → copias byte-id�nticas (ahorro garantizado)
   - `REVIEW_PERCEPTUAL/` → candidatos perceptuales a revisar manualmente
   - `REVIEW_DATE/` → archivos sin evidencia de fecha s�lida (solo mtime, solo nombre, o conflicto)
5. Crea reportes + auditor�a por archivo para auditar lo que pas�

> **Fechas:** los grupos exactos usan la mejor evidencia de CUALQUIER copia (si una copia tiene sidecar con `photoTakenTime`, el grupo hereda esa fecha — no importa si el winner alfab�tico no la ten�a). Los grupos perceptuales NO propagan fechas entre miembros: cada uno conserva la suya y va a revisi�n.

## Requisitos

- Python 3.10+ recomendado
- Windows / macOS / Linux
- Espacio suficiente en disco (si usás `action: copy`, duplicará almacenamiento temporalmente)

---

## Instalación

### 1) Clonar repo e iniciar venv

```bash
git clone https://github.com/TU_USUARIO/google-photos-dedupe.git
cd google-photos-dedupe

python -m venv .venv
# Windows:
.\.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 2) Instalar dependencias

```bash
pip install -r requirements.txt
```

> Si querés reportes Excel (.xlsx), asegurate de tener:
```bash
pip install "openpyxl>=3.1.0"
```

---

## Preparar tus Google Takeouts

1. Descargá tu Takeout desde Google
2. Extraé el ZIP y verificá que exista algo como:

- `Takeout/Google Photos/` *(o “Google Fotos” según idioma)*

Ejemplo recomendado de estructura:

```
<PROYECTO>/
  exports/
    account_primary/
      Takeout/
        Google Photos/
          ...
    account_secondary_01/
      Takeout/
        Google Photos/
          ...
    account_secondary_02/
      Takeout/
        Google Photos/
          ...
```

> **IMPORTANTE:** no pongas `out_dir` dentro de `exports/` para evitar que el scanner re-escanee resultados.

---

## Configuración

### 1) Crear tu config real

Este repo trae un archivo de ejemplo:

- `config.example.yaml`

Copialo como `config.yaml` y editá tus rutas reales:

```bash
# Windows PowerShell
copy config.example.yaml config.yaml

# macOS/Linux
cp config.example.yaml config.yaml
```

### 2) Nota sobre inputs (orden importa)

En `inputs:` el **primer elemento** se considera **principal** (prioridad).  
Esto es útil para que, al decidir “winners”, tu cuenta principal sea la referencia cuando hay duplicados.

---

## Uso rápido

### 1) Primero probá con dry-run (RECOMENDADO)

No copia ni mueve nada, solo detecta y genera reportes:

```bash
python -m photos_dedupe --config config.yaml --action dry-run
```

### 2) Ejecutar copia (mantiene backup intacto)

```bash
python -m photos_dedupe --config config.yaml --action copy
```

### 3) Ejecutar move (⚠️ destructivo)

```bash
python -m photos_dedupe --config config.yaml --action move
```

⚠️ `move` moverá archivos desde tus exports al output.  
Si querés mantener el backup original intacto, usá `copy`.

---

## Salida generada

Cada ejecución crea su propio subdirectorio `run_<timestamp>` dentro de `out_dir` (las ejecuciones nunca se pisan):

```
output_consolidado/
  run_20260803_091413/
    UNIQUE/
    DUPLICATES_EXACT/
    REVIEW_PERCEPTUAL/
    REVIEW_DATE/
    REPORTS/
    MANIFESTS/
    LOGS/
```

### Organización por año (v3)

Con `group_by_year: true`, la salida queda así:

```
UNIQUE/
  2018/
  2019/
  2020/
  _UNKNOWN/
DUPLICATES_EXACT/
  2018/
  2019/
  2020/
REVIEW_PERCEPTUAL/
  group_000001/
  group_000002/
```

El año se obtiene con prioridad configurable:

1) `photoTakenTime` del sidecar JSON del Takeout  
2) EXIF `DateTimeOriginal`  
3) `creationTime` del sidecar (subida, no captura)  
4) fecha del nombre del archivo  
5) mtime como fallback (si `allow_mtime_as_capture_date: false`, solo mtime → `REVIEW_DATE/`)

> **Importante:** el año de un grupo exacto se decide por la fecha canónica del grupo (mejor evidencia de cualquier copia), así el grupo nunca se “parte”.

---

## Reportes

Se generan dentro de:

`<out_dir>/run_<timestamp>/REPORTS/`

- `dedupe_report.csv` → tabla por duplicado
- `dedupe_report.json` → estructura rica por grupo
- `dedupe_report.xlsx` → Excel con hoja `SUMMARY` y hojas por año
- `all_files_audit.csv` → auditoría por archivo: TODOS los escaneados, con bucket, año, fuente de fecha, confianza, revisión, destino planificado y estado
- `run_summary.txt` → resumen con buckets, estados de ejecución y **espacio real recuperable**

### Manifiestos (plan inmutable)

En `<out_dir>/run_<timestamp>/MANIFESTS/`:

- `processing_plan.jsonl` → el plan completo (una línea JSON por archivo)
- `run_state.json` → estado de la ejecución (errores, invariantes, resumen)

---

## Logs

En:

`<out_dir>/run_<timestamp>/LOGS/run.log`

Incluye logs detallados para auditoría y troubleshooting.

---

## Metadata (opcional)

`metadata_mode` controla la escritura de `DateTimeOriginal` sobre las **copias** (nunca toca los exports):

- `audit` (default): inspecciona y reporta si cada copia tiene `DateTimeOriginal` y si coincide con la fecha determinada. No requiere ExifTool.
- `write`: escribe `DateTimeOriginal` con **ExifTool** y verifica post-escritura. Si ExifTool no está instalado, no se escribe nada (el archivo no se pierde; se reporta `EXIFTOOL_NOT_AVAILABLE`).
- `disabled`: no hace nada.

**Instalar ExifTool (Windows, opcional pero recomendado para `write`):**

```powershell
winget install --id OliverBetz.ExifTool --exact --accept-source-agreements --accept-package-agreements
```

El binario se detecta automáticamente (PATH o rutas estándar de instalación). Si está en otro lado, usá `exiftool_path:` en el config.

---

## Caché de hashes (v4)

SHA-256 y pHash se persisten entre corridas en `<out_dir>/.photos_dedupe.hash_cache.json`. Cada entrada se valida con (tamaño, mtime) del archivo fuente; si un archivo cambió, se recalcula. La primera corrida sobre una librería grande sigue tardando (~3.5 min de pHash), pero las siguientes reutilizan los hashes y pasan esa etapa en segundos.

- Deshabilitar: `use_hash_cache: false` en el config.
- Ruta custom: `hash_cache_file: "C:/ruta/a/cache.json"`.
- Solo archivos inmutables entre corridas aprovechan el cache (al copiar/mover dentro de `output_*` no afecta los exports de origen).

---

## Estado de terminación

Cada ejecución termina con un estado claro en el log:

- `COMPLETED SUCCESSFULLY` (exit 0) — sin errores ni archivos en revisión.
- `COMPLETED WITH WARNINGS` (exit 0) — hay archivos en `REVIEW_DATE/` o `REVIEW_PERCEPTUAL/` que conviene auditar.
- `FAILED` (exit 1) — hubo operaciones que fallaron (revisá el log y `all_files_audit.csv`).

---

## Advertencias comunes (normal)

### “Truncated File Read” (PIL/TIFF)

Aparece cuando algunas imágenes están incompletas o corruptas (en general no rompe el proceso).  
Se reporta como warning, se continúa.

### “malformed MPO”

Algunos JPEG “MPO” o formatos raros se interpretan como JPEG normal.  
Normalmente no afecta el dedupe.

---

## Troubleshooting

### No genera XLSX y aparece “openpyxl not available”
Instalá openpyxl en el venv:

```bash
pip install "openpyxl>=3.1.0"
```

### El programa tarda mucho
La etapa lenta suele ser `STEP 2/4 - Detección de duplicados` (pHash sobre miles de imágenes).  
Podés:
- bajar `mode: exact` (más rápido)
- subir `workers`
- ejecutar primero en dry-run para medir

En la **segunda corrida** la caché persistente de hashes elimina casi todo el tiempo de la etapa lenta (solo se revalidan los archivos por tamaño/mtime).

---

## Buenas prácticas

- Empezá siempre con `--action dry-run` (genera el plan completo: destinos, colisiones y espacio real, sin tocar nada)
- Revisá `all_files_audit.csv` y `REVIEW_DATE/` antes de borrar nada de `DUPLICATES_EXACT/`
- Usá `copy` si querés conservar el backup original
- `perceptual_policy: review_all` es lo seguro: nada perceptual se mueve a `UNIQUE` sin revisión
- Subí al repo solo:
  - código
  - `config.example.yaml`
  - `README.md`
- Nunca subas:
  - `exports/`
  - `output*/`
  - `config.yaml` real

---

## Licencia
MIT License
