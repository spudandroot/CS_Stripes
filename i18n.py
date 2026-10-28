"""English and Spanish text for the app.

Code is written in English; tr("...") shows it in the chosen language. Text with
blanks to fill in uses {names}: tr("Sent {n} labels to {name}.", n=3, name=p.name).
N_("...") marks text that is kept in English inside the app (printer problems, for
example, are also used as keys) and passed through tr() where it's shown.
test_i18n.py checks that every tr() and N_() text has a Spanish version.
"""

LANGUAGES = (("en", "English"), ("es", "Español"))

_language = "en"


def set_language(code):
    global _language
    _language = code if code in dict(LANGUAGES) else "en"


def language():
    return _language


def N_(text):
    return text


def tr(text, **values):
    if _language == "es":
        text = ES.get(text, text)
    return text.format(**values) if values else text


def retranslate(shown):
    """Fixed text already on screen (a button, a heading...), in the current language."""
    core = shown.strip()
    english = _ENGLISH.get(core, core)
    if english not in ES:
        return shown
    return shown.replace(core, tr(english), 1)


ES = {
    # Main window
    "Zebra label printing": "Impresión de etiquetas Zebra",
    "Version {version}": "Versión {version}",
    "Printer:": "Impresora:",
    "Check printer": "Revisar impresora",
    "Manage printers...": "Administrar impresoras...",
    "Printer monitor": "Monitor de impresoras",
    "Label stock:": "Tipo de etiqueta:",
    "Simple": "Sencillo",
    "Advanced": "Avanzado",
    "Recent:": "Recientes:",
    "New label": "Etiqueta nueva",
    "Preview": "Vista previa",
    "Preview: drag a field to move it": "Vista previa: arrastre un campo para moverlo",
    "Preview: drag items to move them, drag the corner to resize":
        "Vista previa: arrastre los elementos para moverlos y la esquina para cambiar el tamaño",
    "On-screen preview is approximate.": "La vista previa en pantalla es aproximada.",
    "Copies:": "Copias:",
    "Exact preview (online)": "Vista previa exacta (en línea)",
    "Print": "Imprimir",
    "Ready. Tip: Ctrl+P prints.": "Listo. Consejo: Ctrl+P imprime.",
    "Add a printer to get started": "Agregue una impresora para empezar",
    "Type ZPL on the left": "Escriba ZPL a la izquierda",
    "Type your label text on the left": "Escriba el texto de la etiqueta a la izquierda",
    "{model} via {where}, {dpi} dpi  |  {w} x {h} in": "{model} por {where}, {dpi} dpi  |  {w} x {h} pulg.",
    "  |  one-off stock, resets after printing": "  |  tipo de etiqueta solo para esta vez, vuelve al imprimir",
    "No printer set up yet. Click 'Manage printers...'.":
        "Todavía no hay impresoras. Haga clic en 'Administrar impresoras...'.",
    "Printer's own size ({w} x {h} in)": "Tamaño de la impresora ({w} x {h} pulg.)",
    "{name}  (default)": "{name}  (predeterminada)",
    "Checking...": "Revisando...",
    "Ready": "Lista",
    "Not reachable": "Sin conexión",
    "Printing": "Imprimiendo",

    # Simple tab
    "Type your label, one line per row:": "Escriba su etiqueta, una línea por renglón:",
    "Text is sized to fill the label. For barcodes, QR codes, pictures or a different size per line, "
    "use the Advanced tab.":
        "El texto se ajusta para llenar la etiqueta. Para códigos de barras, códigos QR, imágenes o un tamaño "
        "distinto por línea, use la pestaña Avanzado.",
    "Show ZPL": "Mostrar ZPL",

    # Advanced tab
    "Item": "Elemento",
    "Size": "Tamaño",
    "Align": "Alineación",
    "+ Text": "+ Texto",
    "+ Barcode": "+ Cód. barras",
    "+ QR code": "+ Código QR",
    "+ Picture": "+ Imagen",
    "Move up": "Subir",
    "Move down": "Bajar",
    "Delete": "Eliminar",
    "Text:": "Texto:",
    "Data:": "Datos:",
    "Size:": "Tamaño:",
    "Fit": "Ajustar",
    "Fixed:": "Fijo:",
    "Type:": "Tipo:",
    "Show digits": "Mostrar dígitos",
    "Picture:": "Imagen:",
    "Change...": "Cambiar...",
    "Dither (for photos)": "Tramado (para fotos)",
    "Align:": "Alinear:",
    "Left": "Izquierda",
    "Center": "Centro",
    "Right": "Derecha",
    "Add an item, or select one to change it.\nCtrl+click or Shift+click to select several.":
        "Agregue un elemento, o seleccione uno para cambiarlo.\nCtrl+clic o Mayús+clic para seleccionar varios.",
    "Auto layout (stack items to fill the label)": "Diseño automático (llenar la etiqueta)",
    "Text": "Texto",
    "Barcode": "Código de barras",
    "QR code": "Código QR",
    "Picture": "Imagen",
    "(blank line)": "(línea en blanco)",
    "Choose a picture": "Elija una imagen",
    "Pictures": "Imágenes",
    "All files": "Todos los archivos",
    "Couldn't open that picture:\n{error}": "No se pudo abrir esa imagen:\n{error}",
    "That picture's format can't be read. Save it as PNG or JPG and try again.":
        "No se puede leer el formato de esa imagen. Guárdela como PNG o JPG e inténtelo de nuevo.",
    "(unreadable)": "(ilegible)",
    "(empty)": "(vacía)",
    "That recent label couldn't be loaded.": "No se pudo cargar esa etiqueta reciente.",

    # ZPL tab
    "ZPL, printed exactly as typed:": "ZPL, se imprime tal como lo escribe:",
    "From my label": "De mi etiqueta",
    "The preview follows what you type. Drag a field on the preview to move it; its ^FO or ^FT numbers "
    "change to match.":
        "La vista previa sigue lo que escribe. Arrastre un campo en la vista previa para moverlo; sus números "
        "^FO o ^FT cambian para coincidir.",
    "Make a label on the Simple or Advanced tab first.": "Primero haga una etiqueta en la pestaña Sencillo o Avanzado.",
    "Replace the ZPL here with the label from the other tabs?":
        "¿Reemplazar el ZPL de aquí con la etiqueta de las otras pestañas?",
    "Changes printer settings when printed: {what}": "Cambia la configuración de la impresora al imprimir: {what}",

    # Printing and preview
    "Getting exact preview from labelary.com...": "Obteniendo la vista previa exacta de labelary.com...",
    "Exact preview failed.": "Falló la vista previa exacta.",
    "Could not get the preview from labelary.com:\n{error}": "No se pudo obtener la vista previa de labelary.com:\n{error}",
    "Exact preview shown.": "Se muestra la vista previa exacta.",
    "Exact preview": "Vista previa exacta",
    "Rendered by labelary.com from the exact data that will be sent to the printer.":
        "Generada por labelary.com con los mismos datos que se enviarán a la impresora.",
    "Pick a printer and type some ZPL first.": "Primero elija una impresora y escriba algo de ZPL.",
    "Pick a printer and add something to the label first.": "Primero elija una impresora y agregue algo a la etiqueta.",
    "Type some ZPL first.": "Primero escriba algo de ZPL.",
    "Add a printer first (Manage printers...).": "Primero agregue una impresora (Administrar impresoras...).",
    "Type something on the label first.": "Primero escriba algo en la etiqueta.",
    "Fix these before printing:": "Corrija esto antes de imprimir:",
    "Copies must be a whole number from 1 to {max}.": "Las copias deben ser un número entero del 1 al {max}.",
    "This ZPL also changes the printer's own settings: {what}.\n\nThose stay on the printer and change how "
    "labels from other programs print too. Print anyway?":
        "Este ZPL también cambia la configuración de la impresora: {what}.\n\nEsos cambios se quedan en la "
        "impresora y también cambian cómo se imprimen las etiquetas de otros programas. ¿Imprimir de todos modos?",
    "Print {n} labels on {name}?": "¿Imprimir {n} etiquetas en {name}?",
    "Print anyway?": "¿Imprimir de todos modos?",
    "Sending to {name}...": "Enviando a {name}...",
    "Print failed: {error}": "Falló la impresión: {error}",
    "Could not print on {name} ({where}).": "No se pudo imprimir en {name} ({where}).",
    "Label stock is back to the printer's default.": "El tipo de etiqueta volvió al predeterminado de la impresora.",
    "Sent the ZPL to {name}.": "Se envió el ZPL a {name}.",
    "Sent 1 label to {name}.": "Se envió 1 etiqueta a {name}.",
    "Sent {n} labels to {name}.": "Se enviaron {n} etiquetas a {name}.",
    "Could not save settings:\n{error}": "No se pudo guardar la configuración:\n{error}",
    "Welcome! Let's set up your Zebra printers.\n\nClick 'Add from Windows...' to pick printers already "
    "installed on this PC, then check each one's label size.":
        "¡Bienvenido! Vamos a configurar sus impresoras Zebra.\n\nHaga clic en 'Agregar desde Windows...' para "
        "elegir impresoras ya instaladas en esta PC, y luego revise el tamaño de etiqueta de cada una.",

    # Label checks
    "Item {n} goes past the edge of the label.": "El elemento {n} se sale del borde de la etiqueta.",
    "Line {n} is probably too wide for its space.": "La línea {n} probablemente es demasiado ancha para su espacio.",
    "Line {n} is taller than its space.": "La línea {n} es más alta que su espacio.",
    "Line {n} is very small and may be hard to read.": "La línea {n} es muy pequeña y puede ser difícil de leer.",
    "Item {n}: {problem}": "Elemento {n}: {problem}",
    "Item {n}: the barcode is too long to fit; shorten it or make its space wider.":
        "Elemento {n}: el código de barras es demasiado largo; acórtelo o haga su espacio más ancho.",
    "Item {n}: the barcode is very short and may not scan.":
        "Elemento {n}: el código de barras es muy bajo y puede que no se pueda escanear.",
    "Item {n}: QR code is empty.": "Elemento {n}: el código QR está vacío.",
    "Item {n}: the QR code holds too much to fit; shorten it or make its space bigger.":
        "Elemento {n}: el código QR tiene demasiados datos; acórtelo o haga su espacio más grande.",
    "Barcode is empty.": "El código de barras está vacío.",
    "Code 128 supports letters, digits and common symbols only.":
        "Code 128 solo admite letras, dígitos y símbolos comunes.",
    "Code 39 supports A-Z, 0-9, space and - . $ / + % only.": "Code 39 solo admite A-Z, 0-9, espacio y - . $ / + %.",
    "{name} needs {n} digits (or {m} with the check digit).":
        "{name} necesita {n} dígitos (o {m} con el dígito verificador).",
    "{name} check digit is wrong; it should be {digit}.":
        "El dígito verificador de {name} es incorrecto; debe ser {digit}.",
    "Something goes past the edge of the label.": "Algo se sale del borde de la etiqueta.",
    "The label isn't closed with ^XZ.": "La etiqueta no se cierra con ^XZ.",
    "ZPL labels start with ^XA and end with ^XZ.": "Las etiquetas ZPL empiezan con ^XA y terminan con ^XZ.",
    "Rotated fields are shown unrotated in the preview.": "Los campos girados se muestran sin girar en la vista previa.",
    "Only the first label (^XA ... ^XZ) is shown in the preview.":
        "Solo la primera etiqueta (^XA ... ^XZ) se muestra en la vista previa.",
    "Not shown in the preview:": "No se muestra en la vista previa:",
    "Graphic not shown: {error}.": "Gráfico no mostrado: {error}.",
    "^GF data has an unexpected character {char}": "Los datos de ^GF tienen un carácter inesperado {char}",
    "only ^GFA (ASCII hex) graphics can be previewed": "solo se pueden previsualizar gráficos ^GFA (hex ASCII)",
    "^GF needs the bytes per row": "^GF necesita los bytes por fila",

    # Printer settings that ZPL can change
    "print width": "ancho de impresión",
    "label length": "largo de etiqueta",
    "label shift": "desplazamiento de etiqueta",
    "label top": "posición superior de etiqueta",
    "darkness": "oscuridad",
    "print speed": "velocidad de impresión",
    "media type": "tipo de material",
    "thermal type": "tipo térmico",
    "print mode": "modo de impresión",
    "units": "unidades",
    "print orientation": "orientación de impresión",
    "mirror image": "imagen en espejo",
    "maximum label length": "largo máximo de etiqueta",
    "media feed": "avance del material",
    "saving or resetting the configuration": "guardar o restablecer la configuración",
    "reprint after error": "reimprimir después de un error",
    "display": "pantalla",
    "printer name": "nombre de la impresora",
    "front panel password": "contraseña del panel",
    "network": "red",
    "network address": "dirección de red",
    "network ID": "ID de red",
    "network settings": "configuración de red",
    "serial port": "puerto serie",
    "printer clock": "reloj de la impresora",
    "wireless": "inalámbrico",
    "deleting stored objects": "borrar objetos guardados",
    "storing a format": "guardar un formato",
    "storing a graphic": "guardar un gráfico",
    "tear-off position": "posición de corte",
    "backfeed": "retroceso",
    "printer reset": "reinicio de la impresora",
    "media calibration": "calibración del material",
    "length calibration": "calibración del largo",
    "encoding table": "tabla de codificación",
    "command prefix": "prefijo de comandos",
    "delimiter": "delimitador",
    "diagnostics": "diagnóstico",
    "dots per mm": "puntos por mm",
    "printing the configuration label": "imprimir la etiqueta de configuración",

    # Printer status
    "Out of labels": "Sin etiquetas",
    "Paused": "En pausa",
    "Print head open": "Cabezal abierto",
    "Ribbon out": "Sin cinta",
    "Print head too cold": "Cabezal demasiado frío",
    "Print head too hot": "Cabezal demasiado caliente",
    "Receive buffer full": "Búfer de recepción lleno",
    "Printer memory error": "Error de memoria de la impresora",
    "Offline": "Fuera de línea",
    "Door/head open": "Puerta o cabezal abierto",
    "Label jam": "Etiqueta atascada",
    "Error": "Error",
    "Idle": "Inactiva",
    "Printing: 1 label left in this job": "Imprimiendo: falta 1 etiqueta de este trabajo",
    "Printing: {n} labels left in this job": "Imprimiendo: faltan {n} etiquetas de este trabajo",
    "1 job waiting": "1 trabajo en espera",
    "{n} jobs waiting": "{n} trabajos en espera",
    "Label waiting to be taken": "Etiqueta esperando a que la retiren",
    "Labels loaded are {length} in long, not {stock}": "Las etiquetas cargadas miden {length} pulg. de largo, no {stock}",
    "No answer from the printer (timed out). Is it on and connected?":
        "La impresora no responde (se agotó el tiempo). ¿Está encendida y conectada?",
    "The printer refused the connection. Check the IP address and port.":
        "La impresora rechazó la conexión. Revise la dirección IP y el puerto.",
    "Unknown printer address. Check the IP address.": "Dirección de impresora desconocida. Revise la dirección IP.",
    "Windows printer error: {error}": "Error de la impresora de Windows: {error}",
    "Network error: {error}": "Error de red: {error}",
    "The printer sent an unexpected status reply.": "La impresora envió una respuesta de estado inesperada.",
    "Windows printer support is not available (pywin32 is not installed).":
        "La impresión por Windows no está disponible (pywin32 no está instalado).",
    "Windows printer '{name}'": "Impresora de Windows '{name}'",

    # Printer monitor
    "Refresh now": "Actualizar ahora",
    "More info for all": "Más información de todas",
    "Less info for all": "Menos información de todas",
    "Auto refresh:": "Actualizar solo:",
    "Every 5 seconds": "Cada 5 segundos",
    "Every 10 seconds": "Cada 10 segundos",
    "Every 30 seconds": "Cada 30 segundos",
    "Every minute": "Cada minuto",
    "Off": "Nunca",
    "Checking printers...": "Revisando impresoras...",
    "No printers set up yet.": "Todavía no hay impresoras configuradas.",
    "need attention": "requieren atención",
    "not reachable": "sin conexión",
    "to check": "por revisar",
    "printing": "imprimiendo",
    "ready": "listas",
    "updated {time}": "actualizado {time}",
    "Wrong labels?": "¿Etiquetas equivocadas?",
    "No answer.": "Sin respuesta.",
    "Load a new roll and close the head.": "Cargue un rollo nuevo y cierre el cabezal.",
    "Press Pause on the printer to resume.": "Presione Pausa en la impresora para continuar.",
    "Close the print head.": "Cierre el cabezal.",
    "Load a new ribbon.": "Cargue una cinta nueva.",
    "Let the printer warm up.": "Deje que la impresora se caliente.",
    "Let the printer cool down before printing more.": "Deje que la impresora se enfríe antes de imprimir más.",
    "Wait for it to finish, or turn it off and on.": "Espere a que termine, o apáguela y vuelva a encenderla.",
    "Turn the printer off and on.": "Apague y vuelva a encender la impresora.",
    "Check that it's turned on and its network cable is plugged in.":
        "Revise que esté encendida y que el cable de red esté conectado.",
    "Load the right roll, then calibrate the printer so it measures it again.":
        "Cargue el rollo correcto y luego calibre la impresora para que lo vuelva a medir.",
    "Now": "Ahora",
    "Windows queue": "Cola de Windows",
    "Label stock": "Tipo de etiqueta",
    "{w} x {h} in (none set)": "{w} x {h} pulg. (sin definir)",
    "More info": "Más información",
    "Less info": "Menos información",
    "Checked {time}": "Revisada {time}",
    "answered in {ms} ms": "respondió en {ms} ms",
    "Full report...": "Informe completo...",
    "Labels printed": "Etiquetas impresas",
    "Printed length": "Longitud impresa",
    "Darkness": "Oscuridad",
    "Speed": "Velocidad",
    "Print mode": "Modo de impresión",
    "Media": "Material",
    "Sensor": "Sensor",
    "Printing method": "Método de impresión",
    "Ribbon": "Con cinta",
    "Direct thermal": "Térmica directa",
    "Print width": "Ancho de impresión",
    "{n} in": "{n} pulg.",
    "Label length": "Largo de etiqueta",
    "{n} in, with the gap": "{n} pulg., con el espacio",
    "Firmware": "Firmware",
    "Up for": "Encendida hace",
    "MAC address": "Dirección MAC",
    "Name on printer": "Nombre en la impresora",
    "Serial number": "Número de serie",
    "Labels printed (lifetime)": "Etiquetas impresas (total)",
    "Printed length (lifetime)": "Longitud impresa (total)",
    "Uptime": "Tiempo encendida",
    "Resolution (dpi)": "Resolución (dpi)",
    "Rewind": "Rebobinado",
    "Peel-off": "Despegado",
    "Tear-off": "Arrancar",
    "Cutter": "Cortador",
    "Applicator": "Aplicador",
    "Delayed cut": "Corte retardado",
    "Linerless peel": "Despegado sin soporte",
    "Linerless rewind": "Rebobinado sin soporte",
    "Partial cutter": "Corte parcial",
    "Kiosk": "Quiosco",
    "Stream": "Continuo",
    "{name}: full report": "{name}: informe completo",
    "Setting": "Ajuste",
    "Value": "Valor",
    "Device": "Dispositivo",
    "Status": "Estado",
    "Configuration report": "Informe de configuración",
    "Yes": "Sí",
    "No": "No",

    # Deployment status
    "Deployment status": "Estado de instalaciones",
    "Deployment status...": "Estado de instalaciones...",
    "Computer": "Equipo",
    "User": "Usuario",
    "Version": "Versión",
    "Last seen": "Última vez visto",
    "Last update": "Última actualización",
    "Installed": "Instalado",
    "Notes": "Notas",
    "Loading...": "Cargando...",
    "From {folder}. Double-click a row for its history.": "De {folder}. Haga doble clic en una fila para ver su historial.",
    "Refresh": "Actualizar",
    "Close": "Cerrar",
    "Couldn't read {folder}: {error}": "No se pudo leer {folder}: {error}",
    "Settings: {source}": "Configuración: {source}",
    "Open now": "Abierta ahora",
    "Closed": "Cerrada",
    "{n} installs": "{n} instalaciones",
    "{n} open now": "{n} abiertas ahora",
    "{n} on {version}": "{n} en {version}",
    "{n} behind": "{n} atrasadas",
    "History": "Historial",
    "No events yet.": "Todavía no hay eventos.",

    # Manage printers
    "Manage printers": "Administrar impresoras",
    "Printers in this app": "Impresoras en esta aplicación",
    "Add from Windows...": "Agregar desde Windows...",
    "Add by IP": "Agregar por IP",
    "Name": "Nombre",
    "Windows printer": "Impresora de Windows",
    "IP address": "Dirección IP",
    "Port": "Puerto",
    "Model": "Modelo",
    "Label width (in)": "Ancho de etiqueta (pulg.)",
    "Label height (in)": "Alto de etiqueta (pulg.)",
    "Shift right (dots)": "Mover a la derecha (puntos)",
    "Shift down (dots)": "Mover hacia abajo (puntos)",
    "Save printer": "Guardar impresora",
    "Test connection": "Probar conexión",
    "Print test label": "Imprimir etiqueta de prueba",
    "Calibrate media": "Calibrar etiquetas",
    "(none - send straight to the IP address)": "(ninguna - enviar directo a la dirección IP)",
    "Click 'Add from Windows...' to pick printers installed on this PC.":
        "Haga clic en 'Agregar desde Windows...' para elegir impresoras instaladas en esta PC.",
    "Couldn't list Windows printers: {error}": "No se pudieron listar las impresoras de Windows: {error}",
    "No printers were found in Windows.": "No se encontraron impresoras en Windows.",
    "All the printers installed in Windows are already added.":
        "Todas las impresoras instaladas en Windows ya están agregadas.",
    "Add printers from Windows": "Agregar impresoras desde Windows",
    "Pick the label printers to add (Ctrl+click to pick several).\nZebra printers are listed first.":
        "Elija las impresoras de etiquetas que quiere agregar (Ctrl+clic para elegir varias).\n"
        "Las impresoras Zebra aparecen primero.",
    "model unknown": "modelo desconocido",
    "Add selected": "Agregar seleccionadas",
    "Cancel": "Cancelar",
    "Added {n} printer(s). Check the label size and dpi for each one, then 'Print test label'.":
        "Se agregaron {n} impresora(s). Revise el tamaño de etiqueta y los dpi de cada una, y luego use "
        "'Imprimir etiqueta de prueba'.",
    "  - company": "  - empresa",
    "{w} x {h} in": "{w} x {h} pulg.",
    "Company printer, set by IT in {folder}. It can't be changed here.":
        "Impresora de la empresa, configurada por TI en {folder}. No se puede cambiar aquí.",
    "Fill in the details and click 'Save printer'.": "Complete los datos y haga clic en 'Guardar impresora'.",
    "Give the printer a name.": "Póngale un nombre a la impresora.",
    "Pick a Windows printer or enter the printer's IP address.":
        "Elija una impresora de Windows o escriba la dirección IP de la impresora.",
    "Port must be a number (normally 9100).": "El puerto debe ser un número (normalmente 9100).",
    "Label width and height must be in inches, e.g. 2 and 1.":
        "El ancho y el alto de la etiqueta deben estar en pulgadas, por ejemplo 2 y 1.",
    "Shift must be a whole number of dots between -500 and 500.":
        "Mover debe ser un número entero de puntos entre -500 y 500.",
    "A company printer already has that name.": "Una impresora de la empresa ya tiene ese nombre.",
    "Another printer already has that name.": "Otra impresora ya tiene ese nombre.",
    "Saved {name}.": "Se guardó {name}.",
    "Remove {name} from this app?\n\n(It stays installed in Windows.)":
        "¿Quitar {name} de esta aplicación?\n\n(Sigue instalada en Windows.)",
    "Contacting {name}...": "Contactando a {name}...",
    "Connected, but the printer reports: {problems}.": "Conectada, pero la impresora reporta: {problems}.",
    "Windows says the printer is ready. Add its IP address for a more reliable check.":
        "Windows dice que la impresora está lista. Agregue su dirección IP para una revisión más confiable.",
    "Connected. The printer is ready.": "Conectada. La impresora está lista.",
    "Printing a test label on {name}...": "Imprimiendo una etiqueta de prueba en {name}...",
    "Test label sent. The box should line up with the label's edges. If it's off, adjust Shift and save.":
        "Etiqueta de prueba enviada. El recuadro debe coincidir con los bordes de la etiqueta. Si no coincide, "
        "ajuste 'Mover' y guarde.",
    "Calibrate {name}?\n\nThe printer will feed a few blank labels to measure them. Do this after loading a "
    "new kind of label.":
        "¿Calibrar {name}?\n\nLa impresora sacará algunas etiquetas en blanco para medirlas. Hágalo después de "
        "cargar un tipo de etiqueta nuevo.",
    "Calibrating {name}...": "Calibrando {name}...",
    "Calibration started.": "Calibración iniciada.",

    # Company settings files (seen by IT)
    "Couldn't read the company settings from {folder}; using the copy saved {when}.":
        "No se pudo leer la configuración de la empresa en {folder}; se usa la copia guardada el {when}.",
    "Couldn't read the company settings from {folder}: {error}":
        "No se pudo leer la configuración de la empresa en {folder}: {error}",
    "Could not read {path}: {error}": "No se pudo leer {path}: {error}",
    "Could not read stock.json: {error}": "No se pudo leer stock.json: {error}",
    "stock.json entry {n} skipped: {error}": "Se omitió la entrada {n} de stock.json: {error}",
    "printers.json entry {n} skipped: {error}": "Se omitió la entrada {n} de printers.json: {error}",
    "width_in and height_in must be above 0": "width_in y height_in deben ser mayores que 0",
    "media must be one of {choices}": "media debe ser uno de: {choices}",
    "dpi must be one of {choices}": "dpi debe ser uno de: {choices}",
    "duplicate id": "id repetido",
    "duplicate name": "nombre repetido",
    "needs a host (IP address) or windows_name": "necesita host (dirección IP) o windows_name",
    "Printer {name}: unknown stock '{stock}'": "Impresora {name}: tipo de etiqueta desconocido '{stock}'",
    # Tray
    "Open Stripes": "Abrir Stripes",
    "Quit Stripes": "Salir de Stripes",
    "Stripes is still running. Click its icon to open it; right-click it to quit.":
        "Stripes sigue abierto. Haga clic en su icono para abrirlo; haga clic derecho para salir.",
}

_ENGLISH = {spanish: english for english, spanish in ES.items()}
