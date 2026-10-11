"""Professional OnlyOffice guidance shipped to chat agents.

Two pieces of text:

- :data:`RULES_SECTION` is appended to the chat ``CLAUDE.md``/``AGENTS.md`` (the
  agent reads it on every turn). It is deliberately short and opinionated: the
  *standard operating procedure* for producing documents that do not fall apart.
- :data:`GUIDE` is the full reference, written as ``ONLYOFFICE.md`` next to the
  rules. The agent reads it when it needs detail (formats, per-app specifics,
  the exact commands, pitfalls).

Everything here describes what this project actually does (``office.py`` and
``office_collab.py``), not a generic Office tutorial: the co-editor inserts
**plain text** (``Add_Text``), the Document Server saves through our callback,
and comments/chat bridge to the conversation.
"""
from __future__ import annotations

RULES_SECTION = """
## OnlyOffice: documentos impecables (léelo antes de tocar un artefacto)

OnlyOffice **no es Word/Excel/PowerPoint de Microsoft**: edita OOXML nativo.
Para que un documento salga bien y se pueda co-editar sin sorpresas:

1. **Elige el formato correcto desde el principio.**
   - Texto con formato → `.docx` (no `.md`/`.txt` si habrá títulos, negritas o listas).
   - Hoja de cálculo → `.xlsx` (usa `.csv` solo para datos planos sin formato).
   - Presentación → `.pptx`.
   - `.pdf` es **solo lectura** (el editor no lo modifica).
2. **Para crear o reestructurar con formato real** (títulos, tablas, estilos,
   listas, negritas), **escribe el archivo con una librería** dentro de
   `$CREWHALL_OUTPUTS` y déjalo ahí: `python-docx` (Word), `openpyxl` (Excel),
   `python-pptx` (Slides), o `pandoc` (Markdown → `.docx`). Sé fiel al OOXML:
   usa **estilos con nombre y Títulos (Heading 1/2/3)**, no saltos ni espacios
   para “simular” formato.
3. **Edición EN VIVO** (el usuario tiene el documento abierto): usa el co-editor.
   `crewhall office insert` añade **texto plano en el cursor del agente** y el
   usuario lo ve al instante. No esperes formato de ahí: `insert` **no** aplica
   Markdown ni estilos. Para cambios estructurados, edita el archivo (paso 2) y
   vuelve a abrir (`office open`), que recarga por `document.key`.
4. **Revisión**: usa **comentarios anclados** (`office comment`) para señalar
   texto concreto y responde al usuario por el **chat del documento**
   (`office say`). Los comentarios y el chat del usuario te llegan como prompts.
5. **Guarda siempre.** `insert` guarda solo; tras editar por FS, el callback de
   OnlyOffice persiste el archivo (no hace falta nada más). Si dudas, `office save`.
6. `office command` llama un método del editor (familia `asc_*`); úsalo solo si
   conoces el método exacto. La guía completa, con ejemplos, está en
   `ONLYOFFICE.md` (en tu misma carpeta): **léela antes de trabajo serio**.
"""

GUIDE = """# OnlyOffice para agentes de crewhall — guía profesional

> Léela antes de producir o editar un artefacto. Describe **cómo funciona esta
> integración** y cómo sacarle el máximo partido con calidad de oficina.

## 1. Qué es OnlyOffice (y qué no)

OnlyOffice Document Server es un editor colaborativo que trabaja sobre **OOXML**
(el mismo formato que `.docx`/`.xlsx`/`.pptx`) y sobre OpenDocument
(`.odt`/`.ods`/`.odp`). **No es Microsoft Office** ni un clon perfecto:

- Edita los formatos de forma **nativa y colaborativa**, pero funciones muy
  específicas de MS Office (macros, algunos efectos, objetos OLE, ciertos campos)
  pueden no estar o comportarse distinto.
- El **`.md` se abre como texto plano** de un documento de Word y el **`.csv`
  como hoja**; ninguno conserva Markdown ni formato enriquecido.
- El **`.pdf` es solo lectura**: se visualiza, no se edita.

Regla de oro: piensa en “**documento OOXML bien formado**”, no en “Markdown con
extensión .docx”.

## 2. Formatos: qué elegir

| Artefacto | Formato recomendado | Notas |
|---|---|---|
| Informe, carta, contrato, memoria | **`.docx`** | OOXML de Word; soporta estilos, tablas, listas, imágenes, encabezados/pies, comentarios. |
| Hoja de cálculo, presupuesto, modelo | **`.xlsx`** | Fórmulas, varias hojas, formato, filtros. `.csv` solo para datos planos. |
| Presentación | **`.pptx`** | Diapositivas, diseños, notas. |
| Documento editable en estándar abierto | `.odt` / `.ods` / `.odp` | También nativos. |
| Datos tabulares crudos sin formato | `.csv` | Sin estilos ni varias hojas. |
| Texto plano | `.txt` | Sin formato. |
| Vistas/entregables finales | `.pdf` | **Solo lectura** para el editor. |

Si el usuario pide “un documento”, entrega `.docx`. Si pide “una hoja”, `.xlsx`.

## 3. Cómo trabaja crewhall con OnlyOffice

Hay **tres URLs** distintas y cada una hace una cosa (confundirlas es el error
clásico):

| URL | Quién la usa | Para qué |
|---|---|---|
| `DocumentServerUrl` (`office.public_url`) | el **navegador** | carga el SDK y muestra el editor |
| `document.url` (`office.base_url` + `/api/conversation/artifact?...&dt=<token>`) | el **servidor OnlyOffice** | descarga el archivo (con un token temporal, sin cookie) |
| `callbackUrl` (`office.base_url` + `/api/office/callback?...`) | el **servidor OnlyOffice** | **guarda** los cambios en el artefacto de la conversación |

Cuando alguien edita (el usuario o el agente) y se guarda, OnlyOffice hace
`POST` al `callbackUrl` con el documento nuevo y crewhall lo escribe en
`outputs/`. Por eso “lo que se guarda aparece en el panel de artefactos”.

El **`document.key`** identifica la versión del documento (crewhall lo deriva de
`ruta:mtime:tamaño`). Si cambia, el editor **recarga** el archivo. Por eso, tras
**reestructurar el archivo por FS**, basta con volver a abrirlo (`office open`)
para que el editor muestre la versión nueva.

## 4. Niveles de trabajo (elígela bien)

1. **Archivo por FS (recomendado para crear y formatear).** Escribes
   `.docx`/`.xlsx`/`.pptx` con una librería. Control total del formato, fiel a
   OOXML. Es la vía para títulos, tablas, estilos, portadas, numeración…
2. **Co-editor en vivo (Nivel 3).** crewhall levanta un editor headless como
   “AI Agent” unido a la **misma sesión** que el usuario. `insert` añade **texto
   plano** donde esté el cursor del agente y el usuario lo ve al instante (con su
   cursor). Es ideal para **añadir/continuar texto** delante del usuario, no para
   dar formato.
3. **Comentarios y chat.** Revisión anclada a texto y conversación dentro del
   documento.

### La regla que evita el 90% de los problemas
> **Formato por FS, texto en vivo por el co-editor.**

No intentes crear una tabla o un título con `insert`: saldrá texto plano sin
estilo. Créalo por FS; usa `insert` para lo incremental.

## 5. Comandos (lo que de verdad puedes llamar)

Variables que crewhall inyecta en cada chat: `$CREWHALL_CONVERSATION`,
`$CREWHALL_OUTPUTS`, `$CREWHALL_INPUTS`, `$CREWHALL_WORKSPACE`.

```bash
# Copia de seguridad / scratch: escribe SIEMPRE el artefacto en $CREWHALL_OUTPUTS
ls "$CREWHALL_OUTPUTS"

# Co-editor en vivo (idempotente: no hace falta si no hay usuario mirando)
crewhall office open   --conversation "$CREWHALL_CONVERSATION" --path informe.docx
crewhall office read   --conversation "$CREWHALL_CONVERSATION" --path informe.docx   # texto actual
crewhall office insert --conversation "$CREWHALL_CONVERSATION" --path informe.docx --text "…"  # TEXTO PLANO
crewhall office save   --conversation "$CREWHALL_CONVERSATION" --path informe.docx   # fuerza guardado

# Revisión
crewhall office comment  --conversation "$CREWHALL_CONVERSATION" --path informe.docx --text "…"
crewhall office comments --conversation "$CREWHALL_CONVERSATION" --path informe.docx
crewhall office say      --conversation "$CREWHALL_CONVERSATION" --path informe.docx --text "…"
crewhall office chat     --conversation "$CREWHALL_CONVERSATION" --path informe.docx
crewhall office users    --conversation "$CREWHALL_CONVERSATION" --path informe.docx
crewhall office close    --conversation "$CREWHALL_CONVERSATION" --path informe.docx
```

Semántica exacta:

- `insert` → `Add_Text`: **texto plano en el cursor del agente**; guarda solo.
- `read` → devuelve el **texto** actual del documento (sin formato).
- `command --method X --args '[…]'` → llama `X` en el objeto editor (familia
  `asc_*`, p. ej. `asc_Save`). Es de bajo nivel: úsalo solo si conoces el método.
- `comment` → comentario **anclado** al texto seleccionado; `say` → mensaje al
  chat de co-autoría (el usuario lo lee dentro del documento).

Si OnlyOffice no está disponible, `open`/`insert` fallan con un mensaje claro:
en ese caso, **edita el archivo por FS** (sigue siendo un artefacto válido).

## 6. Crear documentos profesionales por FS

Instala lo que necesites en la conversación (red permitida) y escribe en
`$CREWHALL_OUTPUTS`.

### Word — `python-docx`
```python
from docx import Document
from docx.shared import Pt
d = Document()
d.add_heading("Informe de resultados", level=1)   # estilos con nombre, no "negrita gigante"
d.add_paragraph("Resumen ejecutivo…")
d.add_paragraph("Primer punto", style="List Bullet")   # listas con estilo real
t = d.add_table(rows=1, cols=2); t.style = "Light Grid Accent 1"
t.rows[0].cells[0].text = "Método"; t.rows[0].cells[1].text = "Resultado"
d.save("$CREWHALL_OUTPUTS/informe.docx")
```

### Excel — `openpyxl`
```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
wb = Workbook(); ws = wb.active; ws.title = "Enero"
ws.append(["Concepto", "Importe"])                       # fila de cabecera
for c in ws[1]: c.font = Font(bold=True)
for c in ws[1]: c.fill = PatternFill("solid", fgColor="DDEEFF")
ws.append(["Servicios", 1200]); ws.append(["Total", "=SUM(B2:B2)"])  # fórmulas como texto "="
ws.column_dimensions["B"].width = 14
wb.save("$CREWHALL_OUTPUTS/presupuesto.xlsx")
```

### Slides — `python-pptx`
```python
from pptx import Presentation
from pptx.util import Inches
p = Presentation(); s = p.slides.add_slide(p.slide_layouts[0])
s.shapes.title.text = "Propuesta 2027"; s.placeholders[1].text = "Resumen"
p.save("$CREWHALL_OUTPUTS/propuesta.pptx")
```

### Markdown → Word, sin programar el OOXML
```bash
pandoc informe.md -o "$CREWHALL_OUTPUTS/informe.docx"
```

## 7. Buenas prácticas de formato (fidelidad OOXML)

- **Estilos con nombre** (`Heading 1/2/3`, `Title`, `List Bullet`, `Quote`,
  `Table Grid`) en vez de “negrita + tamaño” para simular un título. Así el
  documento tiene un índice/navegación real y se ve bien en OnlyOffice y Word.
- **Tablas de verdad**, no columnas alineadas con espacios o tabuladores.
- **Listas con su estilo**, no guiones escritos a mano en párrafos.
- **Una sola idea por párrafo**; usa títulos para jerarquía.
- Números y fechas: escríbelos como datos (celdas numéricas/fechas), no texto.
- En Excel, **fórmulas** como cadenas que empiezan por `=` (`"=SUM(A1:A5)"`);
  OnlyOffice/Excel las calculan al abrir.
- Evita objetos exóticos (macro, OLE, SmartArt complejo): pueden degradarse.
- Guarda cada artefacto con un **nombre claro y su extensión correcta**.

## 8. Excel, en concreto

- Varias hojas con `ws.title`; no metas todo en la primera.
- Formato de moneda/fecha con `number_format` (`'"$"#,##0.00'`, `'DD/MM/YYYY'`).
- Congela la cabecera: `ws.freeze_panes = "A2"`.
- Filtro automático: `ws.auto_filter.ref = ws.dimensions`.
- Anchos de columna legibles (`column_dimensions[..].width`).
- `.csv` **no** guarda fórmulas ni varias hojas: si eso importa, usa `.xlsx`.

## 9. Interacción en vivo con el usuario (Nivel 3)

- Abre el co-editor (`office open`) **antes** de `insert`; si el usuario tiene el
  documento abierto, verá el texto aparecer en vivo.
- Cuando recibas `[OnlyOffice chat · usuario · doc] …` o
  `[OnlyOffice comentario · usuario] sobre "texto": …`, responde con `office say`
  (chat) o con un `office comment` (revisión), y **haz el cambio pedido**.
- Si el cambio es estructural (reordenar, dar formato a media página), edítalo
  por FS y **reabre** (`office open`) para que todos vean la versión nueva.
- Cierra el co-editor cuando termines una tanda larga (`office close`) para
  liberar recursos.

## 10. Errores frecuentes y cómo evitarlos

- **“El editor muestra la versión anterior.”** El `document.key` no cambió:
  vuelve a abrir el artefacto; verás la versión de disco.
- **“Mi Markdown/CSS no se ve.”** OnlyOffice no interpreta Markdown: formatea con
  estilos reales o produce el archivo con una librería.
- **“El callback da 403 / no guarda.”** El secreto JWT de crewhall y del Document
  Server deben coincidir (`office.jwt_secret`); lo configura `office setup`.
- **“`insert` salió sin formato / en el sitio raro.”** `insert` es texto plano en
  el cursor del agente. Para contenido formateado, escribe el archivo.
- **“El DS no descarga el archivo.”** `office.base_url` debe ser alcanzable desde
  el contenedor (Tailscale o el gateway Docker); `crewhall doctor` lo verifica.

## 11. Flujo recomendado, de principio a fin

1. Decide el formato (`docx`/`xlsx`/`pptx`) según lo que pidió el usuario.
2. Si no existe, **créalo por FS** en `$CREWHALL_OUTPUTS` con estructura y
   estilos correctos (Títulos, tablas, listas, fórmulas).
3. Si el usuario lo tiene abierto y quiere ver cambios **en vivo**, usa el
   co-editor para el texto incremental y `office save`.
4. Atiende comentarios/chat con `office comment` / `office say`.
5. Al terminar, deja el archivo final en `$CREWHALL_OUTPUTS` (nombre y extensión
   correctos) y menciónalo en tu respuesta.

> Resumen en una frase: **formato y estructura por archivo (FS); texto incremental
> y conversación por el co-editor.**
"""


def ensure_guide(directory: str) -> None:
    """Write ONLYOFFICE.md into a directory (idempotent). Best effort."""
    import os

    path = os.path.join(directory, "ONLYOFFICE.md")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                if fh.read() == GUIDE:
                    return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(GUIDE)
        os.chmod(path, 0o600)
    except OSError:
        pass
