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

_RULES_SECTION_ES = """
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

_GUIDE_ES = """# OnlyOffice para agentes de crewhall — guía profesional

> Léela antes de producir o editar un artefacto. Describe **cómo funciona esta
> integración** y cómo sacarle el máximo partido con calidad profesional.

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
- **“Microsoft Office dice que el archivo está corrupto.”** Un `.docx`/`.xlsx`/
  `.pptx` **debe ser un paquete OOXML real**: un ZIP con `[Content_Types].xml` y su
  parte principal (`word/document.xml`, `xl/workbook.xml`, `ppt/presentation.xml`).
  Si creaste un archivo con extensión de Office pero con contenido que no lo es
  (p. ej. Markdown renombrado a `.docx`), cualquier suite lo rechazará. crewhall
  **valida al abrir y al guardar**: si el artefacto no es OOXML válido, abrirlo
  falla con aviso (HTTP 422) y el guardado que devuelve el editor se **rechaza**
  sin sobreescribir la versión buena. Crea siempre el archivo con una librería
  real (paso 6), nunca renombrando texto.

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


_RULES_SECTION_EN = """
## OnlyOffice: flawless documents (read this before touching an artifact)

OnlyOffice is **not Microsoft Word/Excel/PowerPoint**: it edits native OOXML.
So a document comes out right and co-edits without surprises:

1. **Pick the right format from the start.**
   - Formatted text -> `.docx` (not `.md`/`.txt` if there will be headings, bold or lists).
   - Spreadsheet -> `.xlsx` (use `.csv` only for flat, unformatted data).
   - Presentation -> `.pptx`.
   - `.pdf` is **read-only** (the editor does not modify it).
2. **To create or restructure with real formatting** (headings, tables, styles,
   lists, bold), **write the file with a library** inside `$CREWHALL_OUTPUTS` and
   leave it there: `python-docx` (Word), `openpyxl` (Excel), `python-pptx`
   (Slides), or `pandoc` (Markdown -> `.docx`). Be faithful to OOXML: use
   **named styles and Headings (Heading 1/2/3)**, not line breaks or spaces to
   "fake" formatting.
3. **LIVE editing** (the user has the document open): use the co-editor.
   `crewhall office insert` adds **plain text at the agent's cursor** and the user
   sees it at once. Do not expect formatting from it: `insert` does **not** apply
   Markdown or styles. For structural changes, edit the file (step 2) and reopen
   it (`office open`), which reloads via `document.key`.
4. **Review**: use **anchored comments** (`office comment`) to point at specific
   text and answer the user in the **document chat** (`office say`). Comments and
   the user's chat reach you as prompts.
5. **Always save.** `insert` saves by itself; after editing through the FS, the
   OnlyOffice callback persists the file (nothing else needed). If in doubt,
   `office save`.
6. `office command` calls an editor method (the `asc_*` family); use it only if
   you know the exact method. The full guide, with examples, is in
   `ONLYOFFICE.md` (same folder): **read it before serious work**.
"""

_GUIDE_EN = """# OnlyOffice for crewhall agents - professional guide

> Read this before producing or editing an artifact. It describes **how this
> integration works** and how to get the most out of it with professional quality.

## 1. What OnlyOffice is (and is not)

OnlyOffice Document Server is a collaborative editor that works on **OOXML**
(the same format as `.docx`/`.xlsx`/`.pptx`) and OpenDocument
(`.odt`/`.ods`/`.odp`). It is **not Microsoft Office** nor a perfect clone:

- It edits the formats **natively and collaboratively**, but very specific
  MS Office features (macros, some effects, OLE objects, certain fields) may be
  missing or behave differently.
- `.md` opens as the **plain text of a Word document** and `.csv` as a
  **sheet**; neither keeps Markdown or rich formatting.
- `.pdf` is **read-only**: it is viewed, not edited.

Golden rule: think "**well-formed OOXML document**", not "Markdown with a .docx
extension".

## 2. Formats: what to choose

| Artifact | Recommended format | Notes |
|---|---|---|
| Report, letter, contract, memo | **`.docx`** | Word OOXML; supports styles, tables, lists, images, headers/footers, comments. |
| Spreadsheet, budget, model | **`.xlsx`** | Formulas, several sheets, formatting, filters. `.csv` only for flat data. |
| Presentation | **`.pptx`** | Slides, layouts, notes. |
| Editable document in an open standard | `.odt` / `.ods` / `.odp` | Native too. |
| Raw unformatted tabular data | `.csv` | No styles, no multiple sheets. |
| Plain text | `.txt` | No formatting. |
| Final views/deliverables | `.pdf` | **Read-only** for the editor. |

If the user asks for "a document", deliver `.docx`. If they ask for "a sheet",
`.xlsx`.

## 3. How crewhall works with OnlyOffice

There are **three different URLs** and each does one thing (confusing them is the
classic mistake):

| URL | Who uses it | What for |
|---|---|---|
| `DocumentServerUrl` (`office.public_url`) | the **browser** | loads the SDK and shows the editor |
| `document.url` (`office.base_url` + `/api/conversation/artifact?...&dt=<token>`) | the **OnlyOffice server** | downloads the file (with a temporary token, no cookie) |
| `callbackUrl` (`office.base_url` + `/api/office/callback?...`) | the **OnlyOffice server** | **saves** the changes into the conversation artifact |

When someone edits (the user or the agent) and saves, OnlyOffice `POST`s to the
`callbackUrl` with the new document and crewhall writes it to `outputs/`. That is
why "what is saved appears in the artifacts panel".

The **`document.key`** identifies the document version (crewhall derives it from
`path:mtime:size`). If it changes, the editor **reloads** the file. So after
**restructuring the file through the FS**, just reopen it (`office open`) for the
editor to show the new version.

## 4. Levels of work (choose well)

1. **File through the FS (recommended for creating and formatting).** You write
   `.docx`/`.xlsx`/`.pptx` with a library. Full control of formatting, faithful
   to OOXML. It is the way for headings, tables, styles, cover pages, numbering.
2. **Live co-editor (Level 3).** crewhall starts a headless editor as "AI Agent"
   joined to the **same session** as the user. `insert` adds **plain text** where
   the agent's cursor is and the user sees it at once (with their cursor). It is
   ideal to **add/continue text** in front of the user, not to format.
3. **Comments and chat.** Review anchored to text and conversation inside the
   document.

### The rule that avoids 90% of the problems
> **Formatting through the FS, live text through the co-editor.**

Do not try to create a table or a heading with `insert`: it will come out as
unstyled plain text. Create it through the FS; use `insert` for the incremental
part.

## 5. Commands (what you can actually call)

Variables crewhall injects into every chat: `$CREWHALL_CONVERSATION`,
`$CREWHALL_OUTPUTS`, `$CREWHALL_INPUTS`, `$CREWHALL_WORKSPACE`.

```bash
# Backup / scratch: ALWAYS write the artifact to $CREWHALL_OUTPUTS
ls "$CREWHALL_OUTPUTS"

# Live co-editor (idempotent: not needed if no user is watching)
crewhall office open   --conversation "$CREWHALL_CONVERSATION" --path report.docx
crewhall office read   --conversation "$CREWHALL_CONVERSATION" --path report.docx   # current text
crewhall office insert --conversation "$CREWHALL_CONVERSATION" --path report.docx --text "…"  # PLAIN TEXT
crewhall office save   --conversation "$CREWHALL_CONVERSATION" --path report.docx   # force save

# Review
crewhall office comment  --conversation "$CREWHALL_CONVERSATION" --path report.docx --text "…"
crewhall office comments --conversation "$CREWHALL_CONVERSATION" --path report.docx
crewhall office say      --conversation "$CREWHALL_CONVERSATION" --path report.docx --text "…"
crewhall office chat     --conversation "$CREWHALL_CONVERSATION" --path report.docx
crewhall office users    --conversation "$CREWHALL_CONVERSATION" --path report.docx
crewhall office close    --conversation "$CREWHALL_CONVERSATION" --path report.docx
```

Exact semantics:

- `insert` -> `Add_Text`: **plain text at the agent's cursor**; saves by itself.
- `read` -> returns the document's current **text** (no formatting).
- `command --method X --args '[…]'` -> calls `X` on the editor object (the `asc_*`
  family, e.g. `asc_Save`). It is low-level: use it only if you know the method.
- `comment` -> comment **anchored** to the selected text; `say` -> message to the
  co-authoring chat (the user reads it inside the document).

If OnlyOffice is unavailable, `open`/`insert` fail with a clear message: in that
case, **edit the file through the FS** (it is still a valid artifact).

## 6. Creating professional documents through the FS

Install what you need in the conversation (network allowed) and write to
`$CREWHALL_OUTPUTS`.

### Word - `python-docx`
```python
from docx import Document
d = Document()
d.add_heading("Results report", level=1)   # named styles, not "giant bold"
d.add_paragraph("Executive summary...")
d.add_paragraph("First point", style="List Bullet")   # real styled lists
t = d.add_table(rows=1, cols=2); t.style = "Light Grid Accent 1"
t.rows[0].cells[0].text = "Method"; t.rows[0].cells[1].text = "Result"
d.save("$CREWHALL_OUTPUTS/report.docx")
```

### Excel - `openpyxl`
```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
wb = Workbook(); ws = wb.active; ws.title = "January"
ws.append(["Item", "Amount"])                       # header row
for c in ws[1]: c.font = Font(bold=True)
for c in ws[1]: c.fill = PatternFill("solid", fgColor="DDEEFF")
ws.append(["Services", 1200]); ws.append(["Total", "=SUM(B2:B2)"])  # formulas as "=" strings
ws.column_dimensions["B"].width = 14
wb.save("$CREWHALL_OUTPUTS/budget.xlsx")
```

### Slides - `python-pptx`
```python
from pptx import Presentation
p = Presentation(); s = p.slides.add_slide(p.slide_layouts[0])
s.shapes.title.text = "Proposal 2027"; s.placeholders[1].text = "Summary"
p.save("$CREWHALL_OUTPUTS/proposal.pptx")
```

### Markdown -> Word, without programming the OOXML
```bash
pandoc report.md -o "$CREWHALL_OUTPUTS/report.docx"
```

## 7. Formatting best practices (OOXML fidelity)

- **Named styles** (`Heading 1/2/3`, `Title`, `List Bullet`, `Quote`,
  `Table Grid`) instead of "bold + size" to fake a heading. That way the document
  has a real outline/navigation and looks good in OnlyOffice and Word.
- **Real tables**, not columns aligned with spaces or tabs.
- **Lists with their style**, not dashes typed into paragraphs.
- **One idea per paragraph**; use headings for hierarchy.
- Numbers and dates: write them as data (numeric/date cells), not text.
- In Excel, **formulas** as strings starting with `=` (`"=SUM(A1:A5)"`);
  OnlyOffice/Excel compute them on open.
- Avoid exotic objects (macros, OLE, complex SmartArt): they can degrade.
- Save every artifact with a **clear name and the correct extension**.

## 8. Excel, specifically

- Several sheets via `ws.title`; do not put everything in the first one.
- Currency/date formatting with `number_format` (`'"$"#,##0.00'`, `'DD/MM/YYYY'`).
- Freeze the header: `ws.freeze_panes = "A2"`.
- Auto filter: `ws.auto_filter.ref = ws.dimensions`.
- Readable column widths (`column_dimensions[..].width`).
- `.csv` does **not** store formulas or several sheets: if that matters, use `.xlsx`.

## 9. Live interaction with the user (Level 3)

- Open the co-editor (`office open`) **before** `insert`; if the user has the
  document open, they will see the text appear live.
- When you receive `[OnlyOffice chat · user · doc] …` or
  `[OnlyOffice comment · user] on "text": …`, reply with `office say` (chat) or
  with an `office comment` (review), and **make the requested change**.
- If the change is structural (reorder, format half a page), edit it through the
  FS and **reopen** (`office open`) so everyone sees the new version.
- Close the co-editor when you finish a long batch (`office close`) to free
  resources.

## 10. Common errors and how to avoid them

- **"The editor shows the previous version."** The `document.key` did not change:
  reopen the artifact; you will see the on-disk version.
- **"My Markdown/CSS does not show."** OnlyOffice does not interpret Markdown:
  format with real styles or produce the file with a library.
- **"The callback returns 403 / does not save."** The JWT secret of crewhall and
  the Document Server must match (`office.jwt_secret`); `office setup` sets it.
- **"`insert` came out unformatted / in the wrong place."** `insert` is plain text
  at the agent's cursor. For formatted content, write the file.
- **"The DS cannot download the file."** `office.base_url` must be reachable from
  the container (Tailscale or the Docker gateway); `crewhall doctor` verifies it.
- **"Microsoft Office says the file is corrupt."** A `.docx`/`.xlsx`/`.pptx`
  **must be a real OOXML package**: a ZIP with `[Content_Types].xml` and its main
  part (`word/document.xml`, `xl/workbook.xml`, `ppt/presentation.xml`). If you
  created a file with an Office extension but content that is not OOXML (e.g.
  Markdown renamed to `.docx`), any suite will reject it. crewhall **validates on
  open and on save**: if the artifact is not valid OOXML, opening it fails with a
  warning (HTTP 422) and the save returned by the editor is **rejected** without
  overwriting the good version. Always create the file with a real library
  (step 6), never by renaming text.

## 11. Recommended flow, start to finish

1. Decide the format (`docx`/`xlsx`/`pptx`) from what the user asked.
2. If it does not exist, **create it through the FS** in `$CREWHALL_OUTPUTS` with
   the correct structure and styles (Headings, tables, lists, formulas).
3. If the user has it open and wants to see changes **live**, use the co-editor
   for incremental text and `office save`.
4. Handle comments/chat with `office comment` / `office say`.
5. When done, leave the final file in `$CREWHALL_OUTPUTS` (correct name and
   extension) and mention it in your reply.

> One-sentence summary: **formatting and structure through the file (FS);
> incremental text and conversation through the co-editor.**
"""


def rules_section(lang: str | None = None) -> str:
    return _RULES_SECTION_EN if (lang or "").lower() == "en" else _RULES_SECTION_ES


def guide_text(lang: str | None = None) -> str:
    return _GUIDE_EN if (lang or "").lower() == "en" else _GUIDE_ES


# Backwards-compatible defaults (Spanish).
RULES_SECTION = _RULES_SECTION_ES
GUIDE = _GUIDE_ES


def ensure_guide(directory: str) -> None:
    """Write ONLYOFFICE.md into a directory (idempotent). Best effort."""
    import os

    from .. import settings

    lang = str(settings.get("chat.language") or "es").lower()
    text = guide_text(lang)
    path = os.path.join(directory, "ONLYOFFICE.md")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                if fh.read() == text:
                    return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
    except OSError:
        pass
