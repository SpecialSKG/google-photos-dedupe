# Google Photos Takeout Deduplicator

Herramienta para **detectar y consolidar fotos/videos duplicados** de múltiples exportaciones de **Google Takeout (Google Photos)**.  
Soporta detección por **hash exacto** (SHA256) y por **hash perceptual** (pHash) para encontrar duplicados “visualmente iguales”.

✅ Consolida en carpetas **UNIQUE** y **DUPLICATES**  
✅ (v3) Puede organizar la salida por **año de captura**  
✅ Genera reportes en **CSV / JSON / XLSX (Excel)** con hojas por año  
✅ Modo seguro **dry-run** para ver resultados sin copiar/mover archivos

---

## ¿Qué problema resuelve?

Si tenés varias cuentas de Google Photos o varios Takeouts (por ejemplo, cuentas viejas / nuevas), es común terminar con miles de fotos repetidas entre exportaciones.

Este proyecto:

1. **Escanea** tus Takeouts  
2. **Detecta** duplicados (exactos y/o perceptuales)  
3. **Decide un “winner”** por grupo (el que se conserva como principal)  
4. Copia (o mueve) a:
   - `UNIQUE/` → lo que te vas a quedar (únicos + winners)
   - `DUPLICATES/` → lo repetido
5. Crea reportes para auditar lo que pasó

---

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

En tu `out_dir` verás:

```
output_consolidado/
  UNIQUE/
  DUPLICATES/
  REPORTS/
  LOGS/
```

### Organización por año (v3)

Si activaste `group_by_year: true`, la salida queda así:

```
UNIQUE/
  2018/
  2019/
  2020/
  _UNKNOWN/
DUPLICATES/
  2018/
  2019/
  2020/
  _UNKNOWN/
```

✅ El año se obtiene con prioridad configurable:

1) JSON sidecar del Takeout  
2) EXIF  
3) mtime (fecha de modificación) como fallback

> **Importante:** para evitar que un mismo grupo se “parta”, el año del grupo se decide por el **winner** del grupo.

---

## Reportes

Se generan dentro de:

`<out_dir>/REPORTS/`

- `dedupe_report.csv` → tabla simple por duplicado
- `dedupe_report.json` → estructura más rica por grupo
- `dedupe_report.xlsx` → Excel con:
  - hoja `SUMMARY`
  - hojas por año (2018, 2019, …, _UNKNOWN)
- `run_summary.txt` → resumen de ejecución

---

## Logs

En:

`<out_dir>/LOGS/run.log`

Incluye logs detallados para auditoría y troubleshooting.

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

---

## Buenas prácticas

- Empezá siempre con `--action dry-run`
- Usá `copy` si querés conservar el backup original
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
