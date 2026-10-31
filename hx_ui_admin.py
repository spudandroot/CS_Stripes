"""The MapleJet admin screens: the admin window and its pages (company, lines, machines, printers,
products and films, print texts, machine + film adjustments), the film editor, the print-text
designer, the adjustment dialog, and the test print and calibration windows.

Every page follows one pattern: a list of records, an editor working on a copy of the selected
record, and Save, which checks it, asks what needs an answer and saves on a worker thread with the
revision it was loaded at, so two administrators can't overwrite each other unseen. Records are
never deleted: "Active" or a status of "Not active" retires one. The admin screens never talk to a
printer, except the test print and calibration windows, which run hx_send.TestPrintRun on their own
thread under the printer's lock.

Bag text (text before and after items, fixed text, the pre-printed origin) is only shown in
entries, tree cells and BagText canvases, never translated.
"""

from __future__ import annotations

import copy
import dataclasses
import ipaddress
import math
import os
import re
import sys
import time
import traceback
import webbrowser
import tkinter as tk
from datetime import date
from decimal import Decimal
from tkinter import filedialog, ttk

import hx_message
import hx_protocol
import hx_state
import hx_ui_common
import hx_win
from hx_core import CancelToken, Msg
from hx_model import (BOLD_RANGE, RESOLUTIONS, WIDTH_RANGE, AuthFailed, BoardUpdate, Calibration, Company, Country,
                      DataUpdate, Film, HxError, Ink, Line, Machine, MachineFilmAdjustment, NotAllowed, PlanError,
                      Printer, PrintText, PrintTextItem, Product, RevisionConflict, ShareHealth, TestEvent,
                      TestFinished, TestRanges, adjustment_id, new_id)
from hx_ui_common import (RED_BG, RED_FG, UI_FONT, BagText, ChoiceBox, HxWindow, PhotoWindow, PictureView,
                          RecordBox, Scroller, WorkFailed, check_tk_thread, mark_done, mark_running, mono_font_family,
                          photo_from_png, png_preview, roles, start_thread, text, when)
from i18n import N_, add_spanish, tr

ES = {
    "MapleJet admin": "Administración de MapleJet",
    "The server can't be reached. Changes can't be saved now.":
        "No se puede conectar con el servidor. Ahora no se pueden guardar cambios.",
    "Company": "Empresa",
    "Lines": "Líneas",
    "Printers": "Impresoras",
    "Products and films": "Productos y películas",
    "Machine + film": "Máquina + película",
    "Show not active": "Mostrar inactivos",
    "New line": "Línea nueva",
    "New machine": "Máquina nueva",
    "New printer": "Impresora nueva",
    "New product": "Producto nuevo",
    "New print text": "Texto de impresión nuevo",
    "New adjustment...": "Ajuste nuevo...",
    "Not saved yet.": "Todavía no se guarda.",
    "Revision {n} · saved by {user} at {time}": "Revisión {n} · guardado por {user} el {time}",
    "Discard your changes to {name}?": "¿Descartar sus cambios en {name}?",
    "Discard": "Descartar",
    "Keep editing": "Seguir editando",
    "{field}: type a number.": "{field}: escriba un número.",
    "{field}: type a whole number.": "{field}: escriba un número entero.",
    "{field} is needed.": "{field}: falta este dato.",
    "Saving...": "Guardando...",
    "Reload": "Volver a cargar",
    "Stripes couldn't save this: {error}": "Stripes no pudo guardar esto: {error}",
    "{user} changed this record at {time}. Reload to see their change; saving now would be refused.":
        "{user} cambió este registro el {time}. Vuelva a cargarlo para ver su cambio; si guarda ahora, se rechazará.",
    "Code": "Código",
    "Cartridge": "Cartucho",
    "Notify user": "Usuario a notificar",
    "Placement": "Colocación",
    "Printer": "Impresora",
    "UID": "UID",
    "Password": "Contraseña",
    "Customer": "Cliente",
    "Settings revision": "Revisión de ajustes",
    "Revision": "Revisión",
    "Adjustment": "Valor del ajuste",
    "Set by": "Ajustado por",
    "Set at": "Ajustado el",
    "Time server": "Servidor de hora",
    "Fallback": "Alternativo",
    "Countries (spelled exactly as printed)": "Países (escritos tal como se imprimen)",
    "Country": "País",
    "Also refused in other text": "También rechazado en otros textos",
    "Offered": "Se ofrece",
    "Add country": "Agregar país",
    "Apply": "Aplicar",
    "Inks (the names the printers report)": "Tintas (los nombres que reportan las impresoras)",
    "Ink name": "Nombre de la tinta",
    "Colour": "Color",
    "Add ink": "Agregar tinta",
    "Remove ink": "Quitar tinta",
    "Lot: from": "Lote: de",
    "to": "a",
    "digits": "dígitos",
    "Free text: at most": "Texto libre: máximo",
    "characters": "caracteres",
    "Administrators group": "Grupo de administradores",
    "Senders group": "Grupo de remitentes",
    "SID": "SID",
    "Look up": "Buscar",
    "You are in this group.": "Usted está en este grupo.",
    "You aren't in this group.": "Usted no está en este grupo.",
    "Standard fonts": "Fuentes estándar",
    "Change these only when every printer's font list has changed: Stripes refuses to send to a printer whose "
    "fonts differ.":
        "Cámbielas solo cuando haya cambiado la lista de fuentes de todas las impresoras: Stripes no envía a una "
        "impresora con fuentes distintas.",
    "Which counter counts bags": "Qué contador cuenta las bolsas",
    "Print counter (PMCNT)": "Contador de impresión (PMCNT)",
    "Message counter (MSCNT)": "Contador de mensajes (MSCNT)",
    "No counter: time each test step": "Sin contador: medir cada paso de prueba por tiempo",
    "Windows couldn't find a group called {name}.": "Windows no encontró un grupo llamado {name}.",
    "You aren't in {group}. After saving, you won't be able to change the MapleJet settings. Save anyway?":
        "Usted no está en {group}. Después de guardar no podrá cambiar la configuración de MapleJet. ¿Guardar de "
        "todos modos?",
    "Save anyway": "Guardar de todos modos",
    "With this country list, these records contain words about origin and can't be sent until they're fixed: "
    "{list} Save anyway?":
        "Con esta lista de países, estos registros contienen palabras sobre el origen y no se pueden enviar hasta "
        "corregirlos: {list} ¿Guardar de todos modos?",
    "Printed code": "Código impreso",
    "Up to 3 capital letters or digits, printed by a Line code item.":
        "Hasta 3 letras mayúsculas o dígitos, impresos por un elemento Código de línea.",
    "Up to 3 capital letters or digits, printed by a Machine code item.":
        "Hasta 3 letras mayúsculas o dígitos, impresos por un elemento Código de máquina.",
    "Cartridge type": "Tipo de cartucho",
    "Only films of the same type are offered at Send.": "Al enviar solo se ofrecen películas del mismo tipo.",
    "Me": "Yo",
    "DOMAIN\\name. Gets this line's alerts on whichever PC they're signed in to.":
        "DOMINIO\\nombre. Recibe las alertas de esta línea en cualquier PC donde inicie sesión.",
    "Add a machine": "Agregar una máquina",
    "Type the notify user as DOMAIN\\name, for example EXAMPLE\\jsmith.":
        "Escriba el usuario a notificar como DOMINIO\\nombre, por ejemplo EXAMPLE\\jsmith.",
    "A printer belongs to one machine.": "Una impresora pertenece a una sola máquina.",
    "Placement mode": "Modo de colocación",
    "Delay: each film's delay after the trigger, plus this machine's delay adjustment for that film. No "
    "calibration.":
        "Retardo: el retardo de cada película después del disparo, más el ajuste de retardo de esta máquina para "
        "esa película. Sin calibración.",
    "Encoder: each film's print position in mm, worked out from this machine's head distance. Calibrate it once, "
    "at installation.":
        "Encoder: la posición de impresión de cada película en mm, calculada con la distancia del cabezal de esta "
        "máquina. Calíbrela una vez, al instalarla.",
    "Print speed (cm/min)": "Velocidad de impresión (cm/min)",
    "Needed before this machine can be sent to.": "Se necesita antes de poder enviar a esta máquina.",
    "Rotate": "Girar",
    "Mirror": "Espejo",
    "Depend on how the printer is mounted.": "Dependen de cómo está montada la impresora.",
    "Seconds per bag": "Segundos por bolsa",
    "Used to time test steps when no counter counts bags.":
        "Se usa para medir los pasos de prueba cuando ningún contador cuenta bolsas.",
    "Encoder (encoder machines only)": "Encoder (solo máquinas con encoder)",
    "Head distance: {mm} mm (calibrated by {user} at {time})": "Distancia del cabezal: {mm} mm (calibrada por {user} "
                                                               "el {time})",
    "Head distance: not calibrated. This machine can't be sent to until it is.":
        "Distancia del cabezal: sin calibrar. No se puede enviar a esta máquina hasta calibrarla.",
    "Calibrate...": "Calibrar...",
    "Expected wheel size (mm)": "Tamaño de rueda esperado (mm)",
    "Expected pulses per turn": "Pulsos por vuelta esperados",
    "Stripes reads these from the printer every day and raises an alert if they change.":
        "Stripes los lee de la impresora cada día y da una alerta si cambian.",
    "Now:": "Ahora:",
    "Change the placement mode of {machine}? Other film values and adjustments then apply, so check every film on "
    "this machine with a test print.":
        "¿Cambiar el modo de colocación de {machine}? Entonces se usan otros valores de película y otros ajustes, así "
        "que revise cada película en esta máquina con una impresión de prueba.",
    "Change it": "Cambiarlo",
    "The printer's fixed address.": "La dirección fija de la impresora.",
    "80 for a real printer.": "80 para una impresora real.",
    "Checked on every poll and Send.": "Se revisa en cada consulta y cada envío.",
    "No password saved yet. Stripes can't check this printer until one is.":
        "Todavía no hay contraseña guardada. Stripes no puede revisar esta impresora hasta que la haya.",
    "A password is saved. Type a new one only to change it.":
        "Hay una contraseña guardada. Escriba una nueva solo para cambiarla.",
    "The saved password was made by a different Stripes build. Type it again.":
        "La contraseña guardada se hizo con otra versión de Stripes. Escríbala de nuevo.",
    "Stripes keeps only a scrambled token made from the password, never the password itself.":
        "Stripes guarda solo un código cifrado hecho con la contraseña, nunca la contraseña.",
    "Type the printer's IP address, for example 192.0.2.10.":
        "Escriba la dirección IP de la impresora, por ejemplo 192.0.2.10.",
    "{name} already has this address.": "{name} ya tiene esta dirección.",
    "The port must be a number from 1 to 65535.": "El puerto debe ser un número de 1 a 65535.",
    "The UID must be a whole number from 1 to 4294967295.": "El UID debe ser un número entero de 1 a 4294967295.",
    "Used by:": "La usa:",
    "Not used by any machine yet.": "Todavía no la usa ninguna máquina.",
    "Add new version": "Agregar versión nueva",
    "A new artwork revision is a new film. It starts blank and needs its own test print.":
        "Una revisión nueva del arte es una película nueva. Empieza en blanco y necesita su propia impresión de "
        "prueba.",
    "Give the product a name.": "Póngale un nombre al producto.",
    "Another product already has that name.": "Otro producto ya tiene ese nombre.",
    "The customer name is too long.": "El nombre del cliente es demasiado largo.",
    "Films": "Películas",
    "Product: {name}": "Producto: {name}",
    "Up to 6 capital letters or digits, used in the printer's message name. A new film needs a new code.":
        "Hasta 6 letras mayúsculas o dígitos, usados en el nombre del mensaje de la impresora. Una película nueva "
        "necesita un código nuevo.",
    "Artwork revision": "Revisión del arte",
    "Only Active films are offered at Send.": "Al enviar solo se ofrecen películas activas.",
    "Settings revision {n}": "Revisión de ajustes {n}",
    "Goes up on every save.": "Aumenta cada vez que se guarda.",
    "Bag": "Bolsa",
    "Back film: the code goes on the back": "Película de atrás: el código va atrás",
    "Front film: the code goes on the front": "Película del frente: el código va al frente",
    "Paper or plastic": "Papel o plástico",
    "Must match the line's cartridge type.": "Debe coincidir con el tipo de cartucho de la línea.",
    "Ink colour": "Color de tinta",
    "Checked against the ink the printer reports.": "Se compara con la tinta que reporta la impresora.",
    "Film length (mm)": "Largo de la película (mm)",
    "The bag's cut-off length, from the spec sheet. Needed for encoder machines.":
        "El largo de corte de la bolsa, de la hoja de especificaciones. Se necesita para máquinas con encoder.",
    "Print position (mm)": "Posición de impresión (mm)",
    "From the bag's bottom edge, the edge that feeds in first. Needed for encoder machines. Found with the test "
    "print.":
        "Desde el borde inferior de la bolsa, el borde que entra primero. Se necesita para máquinas con encoder. Se "
        "encuentra con la impresión de prueba.",
    "Delay (ms)": "Retardo (ms)",
    "Time from the trigger to the print. Needed for delay machines. Found with the test print.":
        "Tiempo desde el disparo hasta la impresión. Se necesita para máquinas con retardo. Se encuentra con la "
        "impresión de prueba.",
    "Print length (mm)": "Largo de la impresión (mm)",
    "How long the code is along the film, for the warning when a print would still be running as the film stops. "
    "Leave empty if unknown.":
        "Cuánto mide el código a lo largo de la película, para avisar cuando una impresión seguiría en curso al "
        "detenerse la película. Déjelo vacío si no se sabe.",
    "Print settings": "Ajustes de impresión",
    "Font": "Fuente",
    "One font and size for the whole print text.": "Una fuente y un tamaño para todo el texto de impresión.",
    "Resolution": "Resolución",
    "Ultra fine": "Ultrafina",
    "Fine": "Fina",
    "Normal": "Normal",
    "Draft": "Borrador",
    "Bold": "Negrita",
    "Width": "Ancho",
    "Origin": "Origen",
    "The origin is pre-printed on the film": "El origen viene preimpreso en la película",
    "Only print texts without an origin item can be used.": "Solo se pueden usar textos de impresión sin elemento de "
                                                            "origen.",
    "Only print texts with an origin item can be used.": "Solo se pueden usar textos de impresión con elemento de "
                                                         "origen.",
    "Pre-printed origin": "Origen preimpreso",
    "Shown at Send, on the floor board and in the log.": "Se muestra al enviar, en el tablero de planta y en el "
                                                         "registro.",
    "Default": "Predeterminado",
    "Pre-filled at Send.": "Se llena solo al enviar.",
    "Files and photos": "Archivos y fotos",
    "Originals stay on the server. Each PC keeps small previews of the photos.":
        "Los originales se quedan en el servidor. Cada PC guarda vistas previas pequeñas de las fotos.",
    "Print texts that don't suit this setting were unticked.":
        "Se desmarcaron los textos de impresión que no corresponden a este ajuste.",
    "Delay machines can't send this film until it has a delay.":
        "Las máquinas con retardo no pueden enviar esta película hasta que tenga un retardo.",
    "Encoder machines can't send this film until it has a film length and a print position.":
        "Las máquinas con encoder no pueden enviar esta película hasta que tenga largo y posición de impresión.",
    "Kind": "Clase",
    "Added by": "Agregado por",
    "Added": "Agregado el",
    "Add as": "Agregar como",
    "Add file...": "Agregar archivo...",
    "Open": "Abrir",
    "Remove from film": "Quitar de la película",
    "Choose a file to add": "Elija un archivo para agregar",
    "PDF files": "Archivos PDF",
    "The file is added. Save the film to keep it.": "Se agregó el archivo. Guarde la película para conservarlo.",
    "Stripes can't reach the server, so the file can't be opened now.":
        "Stripes no puede conectarse con el servidor, así que ahora no se puede abrir el archivo.",
    "Machine adjustments": "Ajustes por máquina",
    "Edit adjustment...": "Editar ajuste...",
    "Ignored during a test print.": "No se usa durante una impresión de prueba.",
    "Test print...": "Impresión de prueba...",
    "Save the film first.": "Primero guarde la película.",
    "Text before": "Texto antes",
    "Data": "Datos",
    "Text after": "Texto después",
    "Gap": "Espacio",
    "Below": "Debajo",
    "Fixed text": "Texto fijo",
    "Packed-on date": "Fecha de empaque",
    "Best-by date": "Fecha de consumo preferente",
    "Country of origin": "País de origen",
    "Free text": "Texto libre",
    "Machine code": "Código de máquina",
    "Line code": "Código de línea",
    "[lot]": "[lote]",
    "[countries]": "[países]",
    "[machine code]": "[código de máquina]",
    "[line code]": "[código de línea]",
    "days": "días",
    "months": "meses",
    "years": "años",
    "Add item": "Agregar elemento",
    "Remove item": "Quitar elemento",
    "A print text has at most one origin item.": "Un texto de impresión tiene como máximo un elemento de origen.",
    "Date format": "Formato de fecha",
    "prints": "imprime",
    "Offset": "Desplazamiento",
    "Always prints \"Product of\" and the countries chosen at Send. It has no text before or after.":
        "Siempre imprime \"Product of\" y los países elegidos al enviar. No tiene texto antes ni después.",
    "Question for the sender": "Pregunta para quien envía",
    "Most characters": "Máximo de caracteres",
    "Required": "Obligatorio",
    "Right of the previous item": "A la derecha del elemento anterior",
    "Below the previous item": "Debajo del elemento anterior",
    "Extra space before the item, in printer canvas units.": "Espacio extra antes del elemento, en unidades del "
                                                             "lienzo de la impresora.",
    "Up to 3 capital letters or digits, used in the printer's message name.":
        "Hasta 3 letras mayúsculas o dígitos, usados en el nombre del mensaje de la impresora.",
    "Revision {n}": "Revisión {n}",
    "Goes up on every save. The send log records which one printed.":
        "Aumenta cada vez que se guarda. El registro de envíos anota cuál se imprimió.",
    "Preview with the longest values": "Vista previa con los valores más largos",
    "Longest line: {n} characters.": "Línea más larga: {n} caracteres.",
    "Approximate: the test print shows exactly how it prints.":
        "Aproximada: la impresión de prueba muestra exactamente cómo se imprime.",
    "Films that may use this print text": "Películas que pueden usar este texto de impresión",
    "Default for this film": "Predeterminado de esta película",
    "Only films whose origin isn't pre-printed are listed, because this print text has an origin item.":
        "Solo se muestran películas sin origen preimpreso, porque este texto de impresión tiene un elemento de origen.",
    "Only films with a pre-printed origin are listed, because this print text has no origin item.":
        "Solo se muestran películas con origen preimpreso, porque este texto de impresión no tiene elemento de origen.",
    "{film} uses this print text as its default. Choose another default in that film first.":
        "{film} usa este texto de impresión como predeterminado. Primero elija otro predeterminado en esa película.",
    "{film}: {problem}": "{film}: {problem}",
    "Save or revert your changes to {film} first.": "Primero guarde o deshaga sus cambios en {film}.",
    "The print text was saved, but these films weren't:": "Se guardó el texto de impresión, pero estas películas no:",
    "All machines": "Todas las máquinas",
    "All films": "Todas las películas",
    "Machine + film adjustment": "Ajuste de máquina + película",
    "Delay machine": "Máquina con retardo",
    "Encoder machine": "Máquina con encoder",
    "Position adjustment (mm)": "Ajuste de posición (mm)",
    "Delay adjustment (ms)": "Ajuste de retardo (ms)",
    "Can be negative. 0 means none.": "Puede ser negativo. 0 significa ninguno.",
    "Set by {user} at {time}": "Ajustado por {user} el {time}",
    "Choose a machine and a film.": "Elija una máquina y una película.",
    "The position adjustment must be between -1000 and 1000 mm.":
        "El ajuste de posición debe estar entre -1000 y 1000 mm.",
    "The delay adjustment must be between -10000 and 10000 ms.":
        "El ajuste de retardo debe estar entre -10000 y 10000 ms.",
    "Test print": "Impresión de prueba",
    "Test print: {film} ({product})": "Impresión de prueba: {film} ({product})",
    "{machine} is running empty film, with no product.": "{machine} está corriendo película vacía, sin producto.",
    "What to vary": "Qué variar",
    "from": "de",
    "in steps of": "en pasos de",
    "None ticked: the film's.": "Sin marcar: los de la película.",
    "Fonts side by side": "Fuentes lado a lado",
    "Add font": "Agregar fuente",
    "Remove font": "Quitar fuente",
    "{steps} steps · about {bags} bags.": "{steps} pasos · unas {bags} bolsas.",
    "Each step runs for {n} bags, counted by the printer.": "Cada paso dura {n} bolsas, contadas por la impresora.",
    "Each step runs for about {seconds} s, because no counter counts bags.":
        "Cada paso dura unos {seconds} s, porque ningún contador cuenta bolsas.",
    "Start test print": "Iniciar impresión de prueba",
    "The server can't be reached, so a test print can't take the printer's lock.":
        "No se puede conectar con el servidor, así que la impresión de prueba no puede tomar el bloqueo de la "
        "impresora.",
    "Start the test print on {machine}? It takes the printer for the whole test, about {bags} bags, and ends with "
    "printing off and slots 9 and 10 empty.":
        "¿Iniciar la impresión de prueba en {machine}? Ocupa la impresora durante toda la prueba, unas {bags} bolsas, "
        "y termina con la impresión apagada y las posiciones 9 y 10 vacías.",
    "Start": "Iniciar",
    "A test print on {machine} is still running.": "Todavía hay una impresión de prueba en curso en {machine}.",
    "Step {step} of {of}": "Paso {step} de {of}",
    "Building and checking the next message...": "Armando y revisando el siguiente mensaje...",
    "Printing: {bags} of {per} bags": "Imprimiendo: {bags} de {per} bolsas",
    "Printing...": "Imprimiendo...",
    "Step done.": "Paso terminado.",
    "Stopping: printing off, then emptying slots 9 and 10.":
        "Deteniendo: se apaga la impresión y luego se vacían las posiciones 9 y 10.",
    "Printing is off and slots 9 and 10 are empty.": "La impresión está apagada y las posiciones 9 y 10 están vacías.",
    "Stop test": "Detener la prueba",
    "Finished: {done} of {of} steps.": "Terminado: {done} de {of} pasos.",
    "Stopped after {done} of {of} steps: {problem}": "Se detuvo después de {done} de {of} pasos: {problem}",
    "Printing is off and slots 9 and 10 are empty. {machine} needs a normal Send before production.":
        "La impresión está apagada y las posiciones 9 y 10 están vacías. {machine} necesita un envío normal antes de "
        "producir.",
    "Stripes couldn't empty slots 9 and 10. Check the printer, and send to it before production.":
        "Stripes no pudo vaciar las posiciones 9 y 10. Revise la impresora y envíele un mensaje antes de producir.",
    "Step": "Paso",
    "Mark": "Marca",
    "Position or delay": "Posición o retardo",
    "Fonts": "Fuentes",
    "Open the film": "Abrir la película",
    "Type the chosen values into the film. That's a normal edit; the film's status doesn't change.":
        "Escriba en la película los valores elegidos. Es una edición normal; el estado de la película no cambia.",
    "Stop the test print?": "¿Detener la impresión de prueba?",
    "Keep it running": "Dejarla seguir",
    "Calibrate {machine}": "Calibrar {machine}",
    "Delay machines aren't calibrated.": "Las máquinas con retardo no se calibran.",
    "Calibrate once, at installation, with empty film. This procedure is still being confirmed on the test unit.":
        "Calibre una vez, al instalar, con película vacía. Este procedimiento todavía se está confirmando en la "
        "unidad de prueba.",
    "Film loaded for calibration": "Película cargada para calibrar",
    "1. Rough head distance": "1. Distancia aproximada del cabezal",
    "Save rough distance": "Guardar la distancia aproximada",
    "Saved: {mm} mm.": "Guardado: {mm} mm.",
    "Save the rough distance first.": "Guarde primero la distancia aproximada.",
    "2. Distance scale": "2. Escala de distancia",
    "Stripes prints two marks {mm} mm apart, at {a} mm and {b} mm after the trigger.":
        "Stripes imprime dos marcas a {mm} mm de distancia, a {a} mm y {b} mm después del disparo.",
    "Print the two marks": "Imprimir las dos marcas",
    "Mark A from the bag's bottom edge (mm)": "Marca A desde el borde inferior de la bolsa (mm)",
    "Mark B (mm)": "Marca B (mm)",
    "Gap: {gap} mm.": "Espacio: {gap} mm.",
    "The gap is within {mm} mm: the wheel size is right.":
        "El espacio está dentro de {mm} mm: el tamaño de la rueda es correcto.",
    "Correct the wheel size on the printer to {wheel} mm and set Expected wheel size on this machine's page to "
    "{wheel} mm, then print the marks again.":
        "Corrija el tamaño de la rueda en la impresora a {wheel} mm y ponga {wheel} mm en Tamaño de rueda esperado en "
        "la página de esta máquina; luego vuelva a imprimir las marcas.",
    "The gap is {measured} mm instead of {sent} mm. Correct the wheel size on the printer and set the same value as "
    "Expected wheel size on this machine's page, then print the marks again.":
        "El espacio es de {measured} mm en vez de {sent} mm. Corrija el tamaño de la rueda en la impresora y ponga el "
        "mismo valor en Tamaño de rueda esperado en la página de esta máquina; luego vuelva a imprimir las marcas.",
    "Print the marks first, then measure them.": "Primero imprima las marcas y luego mídalas.",
    "Print the mark first, then measure it.": "Primero imprima la marca y luego mídala.",
    "3. Head distance": "3. Distancia del cabezal",
    "Rough head distance by tape measure (mm)": "Distancia aproximada del cabezal con cinta métrica (mm)",
    "From the print head back along the film path to where a bag's bottom edge sits when the film stops. It can be "
    "longer than one bag.":
        "Desde el cabezal, hacia atrás por el recorrido de la película, hasta donde queda el borde inferior de una "
        "bolsa cuando la película se detiene. Puede ser más larga que una bolsa.",
    "Stripes prints one mark {h} mm after the trigger.": "Stripes imprime una marca {h} mm después del disparo.",
    "Print the mark": "Imprimir la marca",
    "Measured from the bag's bottom edge (mm)": "Medido desde el borde inferior de la bolsa (mm)",
    "Head distance: {mm} mm": "Distancia del cabezal: {mm} mm",
    "Last spacing check: {mm} mm by {user} at {time}.": "Última revisión de espacio: {mm} mm por {user} el {time}.",
    "Last head distance: {mm} mm by {user} at {time}.": "Última distancia del cabezal: {mm} mm por {user} el {time}.",
    "Save calibration": "Guardar calibración",
    "Saved. The head distance of {machine} is now {mm} mm.": "Guardado. La distancia del cabezal de {machine} ahora es "
                                                             "{mm} mm.",
}
add_spanish(ES)

PAGES = ("company", "lines", "machines", "printers", "products", "print_texts", "adjustments")

STATUS_CHOICES = [("development", N_("In development")), ("active", N_("Active")), ("inactive", N_("Not active"))]
CARTRIDGE_CHOICES = [("paper", N_("Paper (Boxtron)")), ("plastic", N_("Plastic"))]
COLOUR_CHOICES = [("white", N_("White")), ("black", N_("Black"))]
RESOLUTION_CHOICES = [("0", N_("Ultra fine")), ("1", N_("Fine")), ("2", N_("Normal")), ("3", N_("Draft"))]
PLACEMENT_TEXTS = {"encoder": N_("Encoder"), "delay": N_("Delay")}
ITEM_TEXTS = {"fixed": N_("Fixed text"), "packed_date": N_("Packed-on date"), "best_by_date": N_("Best-by date"),
              "lot": N_("Lot"), "origin": N_("Country of origin"), "free_text": N_("Free text"),
              "machine_code": N_("Machine code"), "line_code": N_("Line code")}
UNIT_CHOICES = [("days", N_("days")), ("months", N_("months")), ("years", N_("years"))]
FILE_KIND_TEXTS = {"front_photo": N_("Front photo"), "back_photo": N_("Back photo"), "artwork": N_("Artwork"),
                   "spec": N_("Spec sheet"), "other": N_("Other file")}
PICTURE_TYPES = "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp"
THUMB_PX = 160
ADJUST_LIMITS = {"position_mm": 1000.0, "delay_ms": 10000.0}
_NOTIFY_PATTERN = r"[^\\/\s][^\\/]*\\[^\\/]+"
_WRAP = 620
# An editor's layout: the field column is never narrower than FIELD_MIN_PX, hints wrap to the room
# they have but never narrower than HINT_MIN_PX, and an editor narrower than FORM_MIN_PX scrolls
# sideways instead of squeezing its fields
FIELD_MIN_PX = 160
HINT_MIN_PX = 100
FORM_MIN_PX = 500
# A wrapped label's own width depends on its wraplength, so two widths can feed each other for
# ever (seen with the Windows fonts: wrap to a, the label narrows to b, wrap to b, it widens to a...).
# A width the label was given less than _FIT_LOOP_S ago is taken as that loop, not a resize
_FIT_LOOP_S = 0.5


# --------------------------------------------------------------------------
# Pure helpers (no Tk)
# --------------------------------------------------------------------------

def parse_number(text: str, *, whole: bool = False):
    """A typed number: stripped; "" -> None; one "," with no "." counts as the decimal point.
    Raises ValueError for anything else (and for a fraction when whole)."""
    t = (text or "").strip()
    if t == "":
        return None
    if t.count(",") == 1 and "." not in t:
        t = t.replace(",", ".")
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", t):
        raise ValueError(f"not a number: {text!r}")
    value = float(t)
    if not math.isfinite(value):
        raise ValueError(f"not a number: {text!r}")
    if whole:
        if not value.is_integer():
            raise ValueError(f"not a whole number: {text!r}")
        return int(value)
    return value


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def printer_problems(printer: Printer, data) -> list[Msg]:
    """ui.md §11.5 item 4, in order."""
    problems = []
    name = (printer.name or "").strip()
    if not name or len(printer.name) > 40:
        problems.append(Msg(N_("Give the printer a name.")))
    elif any(p.id != printer.id and (p.name or "").strip().casefold() == name.casefold()
             for p in data.printers.values()):
        problems.append(Msg(N_("Another printer already has that name.")))
    try:
        # As saved: a host with spaces around it can't be connected to (the form strips them)
        ipaddress.IPv4Address(printer.host or "")
        host_ok = True
    except ValueError:
        host_ok = False
        problems.append(Msg(N_("Type the printer's IP address, for example 192.0.2.10.")))
    if host_ok and printer.active:
        for p in sorted(data.printers.values(), key=lambda p: (hx_state.natural_key(p.name), p.id)):
            if p.id != printer.id and p.active and p.host.strip() == printer.host.strip() and p.port == printer.port:
                problems.append(Msg(N_("{name} already has this address."), {"name": p.name}))
                break
    if not _is_int(printer.port) or not 1 <= printer.port <= 65535:
        problems.append(Msg(N_("The port must be a number from 1 to 65535.")))
    if not _is_int(printer.uid) or not 1 <= printer.uid <= 4294967295:
        problems.append(Msg(N_("The UID must be a whole number from 1 to 4294967295.")))
    return problems


def product_problems(product: Product, data) -> list[Msg]:
    """ui.md §11.6 item 4."""
    problems = []
    name = (product.name or "").strip()
    if not name or len(product.name) > 80:
        problems.append(Msg(N_("Give the product a name.")))
    elif any(p.id != product.id and p.active and (p.name or "").strip().casefold() == name.casefold()
             for p in data.products.values()):
        problems.append(Msg(N_("Another product already has that name.")))
    if len(product.customer or "") > 80:
        problems.append(Msg(N_("The customer name is too long.")))
    return problems


def adjustment_problems(adj: MachineFilmAdjustment, data) -> list[Msg]:
    """ui.md §11.9 item 3."""
    if adj.machine_id not in data.machines or adj.film_id not in data.films:
        return [Msg(N_("Choose a machine and a film."))]
    problems = []
    top_mm, top_ms = ADJUST_LIMITS["position_mm"], ADJUST_LIMITS["delay_ms"]
    if not _finite(adj.position_mm) or not -top_mm <= adj.position_mm <= top_mm:
        problems.append(Msg(N_("The position adjustment must be between -1000 and 1000 mm.")))
    if not _finite(adj.delay_ms) or not -top_ms <= adj.delay_ms <= top_ms:
        problems.append(Msg(N_("The delay adjustment must be between -10000 and 10000 ms.")))
    return problems


def notify_user_problems(text: str) -> list[Msg]:
    """"" is fine; otherwise DOMAIN\\name."""
    if text == "":
        return []
    if not re.fullmatch(_NOTIFY_PATTERN, text or ""):
        return [Msg(N_("Type the notify user as DOMAIN\\name, for example EXAMPLE\\jsmith."))]
    return []


def origin_rule_ok(film: Film, pt: PrintText) -> bool:
    """Pre-printed film: pt has no origin item; otherwise exactly one (send.md §2.4)."""
    n = sum(1 for it in pt.items if it.kind == "origin")
    return n == 0 if film.origin_preprinted else n == 1


def print_text_choices(film: Film, data) -> list[PrintText]:
    """Print texts of any status that suit the film's origin setting, by name."""
    found = [pt for pt in data.print_texts.values() if origin_rule_ok(film, pt)]
    return sorted(found, key=lambda pt: (hx_state.natural_key(pt.name), pt.id))


def film_choices(pt: PrintText, data) -> list[Film]:
    """Films of any status that suit the print text's origin items, by product, then code and revision."""
    def product_name(film):
        product = data.products.get(film.product_id)
        return product.name if product is not None else ""
    found = [f for f in data.films.values() if origin_rule_ok(f, pt)]
    return sorted(found, key=lambda f: (hx_state.natural_key(product_name(f)),) + _film_key(f))


def film_name(film: Film) -> str:
    """A film as people know it: code · artwork revision (data)."""
    return f"{film.code} · {film.artwork_revision}" if film.artwork_revision else film.code


def _film_key(film: Film) -> tuple:
    """Films in the Send window's order: by code, then artwork revision ("Rev 2" before "Rev 10")."""
    return hx_state.natural_key(film.code), hx_state.natural_key(film.artwork_revision), film.id


def membership_saves(pt: PrintText, wanted: set[str], data) -> tuple[list[Film], list[Msg]]:
    """The films whose print_text_ids change when the print text is allowed with exactly the wanted
    films, each checked with validate_film; (films to save, problems) (ui.md §11.8.5)."""
    data2 = dataclasses.replace(data, print_texts={**data.print_texts, pt.id: pt})
    films, problems = [], []
    for film in film_choices(pt, data2):
        has, want = pt.id in film.print_text_ids, film.id in wanted
        if has == want:
            continue
        changed = copy.deepcopy(film)
        if want:
            changed.print_text_ids.append(pt.id)
        else:
            changed.print_text_ids = [i for i in changed.print_text_ids if i != pt.id]
            if film.default_print_text_id == pt.id:
                problems.append(Msg(N_("{film} uses this print text as its default. Choose another default in that "
                                       "film first."), {"film": film_name(film)}))
                continue
        found = hx_message.validate_film(changed, data2)
        problems += [Msg(N_("{film}: {problem}"), {"film": film_name(film), "problem": p}) for p in found]
        if not found:
            films.append(changed)
    # A film that already allows the print text but no longer suits it (an origin item removed or
    # added) would be left failing validate_film: refused, so the admin unticks it in that film first
    chosen = {f.id for f in film_choices(pt, data2)}
    for film in sorted(data.films.values(), key=_film_key):
        if film.id in chosen or pt.id not in film.print_text_ids:
            continue
        before = hx_message.validate_film(film, data)
        found = [p for p in hx_message.validate_film(film, data2) if p not in before]
        problems += [Msg(N_("{film}: {problem}"), {"film": film_name(film), "problem": p}) for p in found]
    return films, problems


def new_film(product_id: str) -> Film:
    """A new artwork revision is a new, blank film (plan)."""
    return Film(id=new_id("fm"), product_id=product_id, code="")


def longest_values(pt: PrintText, company: Company, today: date) -> dict[str, str]:
    """Each item's longest possible value, for the designer's preview; "?" when it can't be worked out."""
    values = {}
    for item in pt.items:
        try:
            values[item.id] = _longest(item, company, today)
        except (KeyError, ValueError, TypeError, OverflowError):
            values[item.id] = "?"
    return values


def _longest(item, company, today):
    before, after = item.before, item.after
    kind = item.kind
    if kind == "fixed":
        return item.text
    if kind == "packed_date":
        return before + hx_message.render_date(hx_message.DATE_FORMATS[item.date_format], today) + after
    if kind == "best_by_date":
        add = {f"add_{item.offset_unit}": item.offset}
        if item.offset_unit not in ("days", "months", "years"):
            raise ValueError("unknown unit")
        return before + hx_message.render_date(hx_message.DATE_FORMATS[item.date_format], today, **add) + after
    if kind == "lot":
        return before + "8" * company.lot_max_digits + after
    if kind == "origin":
        active = [c.name for c in company.countries if c.active]
        return hx_message.origin_phrase(active) if active else ""
    if kind == "free_text":
        return before + "W" * item.max_len + after
    if kind in ("machine_code", "line_code"):
        return before + "WWW" + after
    raise ValueError(f"unknown item kind {kind!r}")


def preview_lines(pt: PrintText, values: dict[str, str]) -> list[str]:
    """A new line for the first item and every item placed below; otherwise on the same line, after a
    space when it has a gap (as send.md §3.5 renders one line)."""
    lines = []
    for k, item in enumerate(pt.items):
        value = values.get(item.id, "")
        if k == 0 or item.placement == "below" or not lines:
            lines.append(value)
        else:
            lines[-1] += (" " if item.gap > 0 else "") + value
    return lines


# --------------------------------------------------------------------------
# Small Tk helpers
# --------------------------------------------------------------------------

def _num(x) -> str:
    """A number for people (never translated): 150.0 -> "150", 152.5 -> "152.5". Exact, with no
    exponent, so a form reads back what it was filled with: 1480.125 stays 1480.125, and an
    unrelated save never changes a stored value (ui.md §2.2 rule 7). Round worked-out values first."""
    if x is None:
        return ""
    if isinstance(x, float):
        if not math.isfinite(x):
            return str(x)
        shown = format(Decimal(repr(x)), "f")      # repr: the shortest text that reads back the same
        if "." in shown:
            shown = shown.rstrip("0").rstrip(".")
        return "0" if shown == "-0" else shown
    return str(x)


def _enable(widget, on):
    try:
        widget.state(["!disabled"] if on else ["disabled"])
    except (AttributeError, tk.TclError):
        widget.configure(state="normal" if on else "disabled")


def _enabled(widget) -> bool:
    return widget.instate(["!disabled"])


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _error_msg(error) -> Msg:
    if isinstance(error, HxError):
        return error.msg
    traceback.print_exception(error)
    return Msg(N_("Stripes hit a problem: {error}"), {"error": f"{type(error).__name__}: {error}"})


def _clear(frame, texts):
    """Destroy a frame's children and drop their Texts entries."""
    def forget(widget):
        for child in widget.winfo_children():
            forget(child)
        texts.forget(widget)
    for child in frame.winfo_children():
        forget(child)
        child.destroy()


def _full_screen(window, on=True):
    """The red page: full screen and on top. A window on screen is taken off and shown again, so a
    window manager that only reads the state at mapping applies it too."""
    mapped = bool(window.winfo_ismapped())
    if mapped:
        window.withdraw()
    window.attributes("-fullscreen", bool(on))
    window.attributes("-topmost", bool(on))
    if mapped:
        window.deiconify()


def _marked(key, work):
    """A thread's work that also ends its running mark when the thread ends, even if its window has
    gone by then (the window marks it done too, when TestFinished arrives)."""
    def target():
        try:
            return work()
        finally:
            mark_done(key)
    return target


def _fit_wrap(label, *, cap=_WRAP, floor=HINT_MIN_PX):
    """Wrap a label's text to the width it's given, at most cap and at least floor pixels, so a long
    hint wraps instead of running out of the window. The label must fill its cell (grid sticky "ew",
    or pack fill="x") so it gets wider again when the window does."""
    recent = []         # (monotonic, px) of this label's latest wrap changes, within _FIT_LOOP_S

    def fit(event):
        px = max(floor, min(cap, event.width - 4))
        if getattr(label, "_wrap_px", None) == px:
            return
        now = time.monotonic()
        recent[:] = [(t, p) for t, p in recent if now - t < _FIT_LOOP_S]
        if any(p == px for _, p in recent):
            return          # back to a width it had a moment ago: the loop, not a resize
        recent.append((now, px))
        label._wrap_px = px
        label.configure(wraplength=px)
    label.bind("<Configure>", fit, add="+")
    label._fit_wrap = fit       # tests call it with made-up events
    return label


def _red_label(master, size=14, bold=False, wrap=1200):
    return tk.Label(master, background=RED_BG, foreground=RED_FG, font=(UI_FONT, size, "bold" if bold else "normal"),
                    wraplength=wrap, justify="left", anchor="w")


class _Lines(ttk.Label):
    """Msgs in the current language, one per line (or joined by sep); hidden while there are none.
    Grid it once where it belongs; with fit, grid it sticky "ew" and it wraps to the width it gets."""

    def __init__(self, master, *, style="Bad.TLabel", wrap=_WRAP, sep="\n", fit=False, **kw):
        super().__init__(master, style=style, wraplength=wrap, justify="left", **kw)
        self.msgs: list[Msg] = []
        self.sep = sep
        if fit:
            _fit_wrap(self, cap=wrap)

    def show(self, msgs, *, header: Msg | None = None) -> None:
        msgs = list(msgs)
        self.msgs = ([header] if header is not None and msgs else []) + msgs
        self.render()

    def render(self) -> None:
        check_tk_thread()
        self.configure(text=self.sep.join(text(m) for m in self.msgs))
        if self.msgs:
            self.grid()
        else:
            self.grid_remove()


def _render_lines(widget):
    for child in widget.winfo_children():
        if isinstance(child, HxWindow):
            continue
        if isinstance(child, _Lines):
            child.render()
        _render_lines(child)


def _yes_no(value) -> str:
    return tr("Yes") if value else tr("No")


def _choice_text(choices, key) -> str:
    for k, english in choices:
        if k == key:
            return tr(english)
    return key


def _adjustment_text(machine, adj) -> str:
    """+20 ms, -3 mm or 0 (data, never translated)."""
    if adj is None:
        return "0"
    encoder = machine is not None and machine.placement == "encoder"
    value, unit = (adj.position_mm, "mm") if encoder else (adj.delay_ms, "ms")
    if not value:
        return "0"
    return f"{value:+g} {unit}"


def _sorted(records):
    return sorted(records, key=lambda r: (hx_state.natural_key(getattr(r, "name", "")), r.id))


# --------------------------------------------------------------------------
# A record's fields
# --------------------------------------------------------------------------

class _Form(ttk.Frame):
    """One record's fields: filled from a record and read into a copy of it. fields holds each
    record field's variable or widget, by the field's name (ui.md §11.1 test attributes)."""

    def __init__(self, master, page):
        super().__init__(master)
        self.page = page
        self.window = page.window
        self.services = page.services
        self.texts = page.texts
        self.fields: dict[str, tk.Variable | tk.Widget] = {}
        self._kinds: dict[str, tuple] = {}      # name -> (kind, label, required)
        self._loading = 0
        # Labels, fields, hints. The field column keeps at least FIELD_MIN_PX; the hint column, weighted
        # three times as much, takes most of any shortfall and its hints wrap to what is left, so an
        # editor narrower than the hints' one-line width never squeezes a field to nothing
        self.columnconfigure(1, weight=1, minsize=FIELD_MIN_PX)
        self.columnconfigure(2, weight=3)

    # building

    def label(self, row, english, col=0, **grid):
        label = ttk.Label(self)
        self.texts.set(label, english)
        label.grid(row=row, column=col, sticky="w", padx=(0, 8), pady=2, **grid)
        return label

    def hint(self, master, english, *, fit=False, **values):
        """A muted hint; with fit it wraps to the width it gets (grid it sticky "ew")."""
        label = ttk.Label(master, style="Muted.TLabel", wraplength=_WRAP, justify="left")
        self.texts.set(label, english, **values)
        if fit:
            _fit_wrap(label)
        return label

    def side_hint(self, row, english, *, column=2, columnspan=1, **values):
        """A hint right of the row's field (column 2), or under it (column 1, spanning 2), wrapping to
        the room it has."""
        label = self.hint(self, english, fit=True, **values)
        label.grid(row=row, column=column, columnspan=columnspan, sticky="ew", padx=(8, 0) if column == 2 else 0)
        return label

    def section(self, row, english):
        label = ttk.Label(self, style="Hx.Section.TLabel")
        self.texts.set(label, english)
        label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))
        return label

    def var(self, name, kind="text", *, label="", required=False, var=None):
        """A tracked variable for a record field; kind text, number, whole or bool."""
        if var is None:
            var = tk.BooleanVar(value=False) if kind == "bool" else tk.StringVar(value="")
        var.trace_add("write", self.changed)
        self.fields[name] = var
        self._kinds[name] = (kind, label, required)
        return var

    def entry(self, row, name, english, *, kind="text", width=30, required=False, hint=None, master=None):
        self.label(row, english)
        var = self.var(name, kind, label=english, required=required)
        entry = ttk.Entry(master or self, textvariable=var, width=width)
        # Without a hint it may reach into the hint column, so a wide entry never narrows the hints
        entry.grid(row=row, column=1, columnspan=1 if hint else 2, sticky="w", pady=2)
        if hint:
            self.side_hint(row, hint)
        return var, entry

    def spin(self, master, name, english, lo, hi, *, width=5):
        var = self.var(name, "whole", label=english, required=True)
        spin = ttk.Spinbox(master, from_=lo, to=hi, increment=1, width=width, textvariable=var)
        return var, spin

    def check(self, master, name, english):
        var = self.var(name, "bool")
        check = ttk.Checkbutton(master, variable=var)
        self.texts.set(check, english)
        return var, check

    def choice(self, master, name, choices, *, kind="choice", width=20):
        box = ChoiceBox(master, choices, width=width)
        box.bind("<<ComboboxSelected>>", self.changed, add="+")
        self.fields[name] = box
        self._kinds[name] = (kind, "", False)
        return box

    def records(self, master, name, *, width=30, kind="record"):
        box = RecordBox(master, width=width)
        box.bind("<<ComboboxSelected>>", self.changed, add="+")
        self.fields[name] = box
        self._kinds[name] = (kind, "", False)
        return box

    def changed(self, *args):
        if not self._loading:
            self.page.mark_dirty()
            self.on_change()

    def on_change(self):
        """Live updates after any change (hints, previews)."""

    # filling and reading

    def fill(self, record) -> None:
        self._loading += 1
        try:
            self.prepare(record)
            for name, field in self.fields.items():
                kind = self._kinds.get(name, ("custom",))[0]
                value = getattr(record, name, None)
                if kind == "text":
                    field.set(value or "")
                elif kind in ("number", "whole"):
                    field.set(_num(value))
                elif kind == "bool":
                    field.set(bool(value))
                elif kind in ("choice", "choice_int"):
                    field.set_key("" if value is None else str(value))
                elif kind in ("record", "record_int"):
                    field.set_record_id("" if value is None else str(value))
            self.fill_extra(record)
        finally:
            self._loading -= 1
        self.on_change()

    def prepare(self, record):
        """Before the fields are set: choice lists that depend on the record."""

    def fill_extra(self, record):
        """Fields that aren't plain variables."""

    def read(self, record, problems: list[Msg]) -> None:
        for name, field in self.fields.items():
            kind, english, required = self._kinds.get(name, ("custom", "", False))
            if kind == "text":
                setattr(record, name, field.get())
            elif kind in ("number", "whole"):
                try:
                    value = parse_number(field.get(), whole=kind == "whole")
                except ValueError:
                    key = N_("{field}: type a whole number.") if kind == "whole" else N_("{field}: type a number.")
                    problems.append(Msg(key, {"field": Msg(english)}))
                    continue
                if value is None and required:
                    problems.append(Msg(N_("{field} is needed."), {"field": Msg(english)}))
                    continue
                if kind == "number" and value is not None:
                    value = float(value)
                setattr(record, name, value)
            elif kind == "bool":
                setattr(record, name, bool(field.get()))
            elif kind == "choice":
                setattr(record, name, field.key())
            elif kind == "choice_int":
                key = field.key()
                setattr(record, name, int(key) if key.lstrip("-").isdigit() else -1)
            elif kind == "record":
                setattr(record, name, field.record_id())
            elif kind == "record_int":
                key = field.record_id()
                setattr(record, name, int(key) if key.isdigit() else -1)
        self.read_extra(record, problems)

    def read_extra(self, record, problems):
        pass

    def before_save(self):
        """Save pressed, before the form is read (the printer password)."""

    def checks(self, record) -> list[Msg]:
        return []

    def warnings(self, record) -> list[Msg]:
        """Warnings that don't block, shown after the checks."""
        return []

    def questions(self, record) -> bool:
        """Warnings that need an answer; False stops the save."""
        return True

    def refresh(self) -> None:
        """A DataUpdate or BoardUpdate: lists and live labels."""

    def refresh_texts(self) -> None:
        """Text built from data and translated words, again in the current language."""

    def can_save(self) -> bool:
        return True


# --------------------------------------------------------------------------
# The list-plus-editor pattern
# --------------------------------------------------------------------------

class AdminPage(ttk.Frame):
    """A list of records and an editor working on a copy of one (ui.md §11.1)."""
    key = ""
    tab_text = ""
    new_text = ""
    columns: tuple = ()             # (column id, N_ heading, width); the first is the tree's own column
    tree_show = "headings"

    def __init__(self, master, window: "AdminWindow"):
        super().__init__(master, padding=8)
        self.window = window
        self.services = window.services
        self.texts = window.texts
        self.working = None
        self.expected_revision = None
        self.dirty = False
        self.editor: _Form | None = None
        self.show_inactive_var = tk.BooleanVar(value=False)
        self._saving = False
        self._build()

    @property
    def fields(self) -> dict:
        """The open editor's fields, by the record's field names."""
        return self.editor.fields if self.editor is not None else {}

    # building

    def _build(self):
        texts = self.texts
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        self.top = ttk.Frame(self)
        self.top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.new_button = ttk.Button(self.top, command=self.start_new)
        if self.new_text:
            texts.set(self.new_button, self.new_text)
            self.new_button.pack(side="left")
        self.extra_buttons(self.top)
        self.show_inactive_check = ttk.Checkbutton(self.top, variable=self.show_inactive_var,
                                                   command=self.refresh_list)
        texts.set(self.show_inactive_check, N_("Show not active"))
        self.show_inactive_check.pack(side="left", padx=(16, 0))

        self.list_frame = ttk.Frame(self)
        self.list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.list_frame.rowconfigure(0, weight=1)
        ids = [c[0] for c in self.columns]
        shown_columns = ids[1:] if self.tree_show == "tree headings" else ids
        self.tree = ttk.Treeview(self.list_frame, columns=shown_columns, show=self.tree_show, height=18,
                                 selectmode="browse")
        if self.tree_show == "tree headings":
            column_id, english, width = self.columns[0]
            texts.heading(self.tree, "#0", english)
            self.tree.column("#0", width=width, stretch=True)
            rest = self.columns[1:]
        else:
            rest = self.columns
        for column_id, english, width in rest:
            texts.heading(self.tree, column_id, english)
            self.tree.column(column_id, width=width, stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select(), add="+")

        self.editor_scroller = Scroller(self)
        self.editor_scroller.grid(row=1, column=1, sticky="nsew")
        # Sideways too: an editor narrower than FORM_MIN_PX keeps that width and scrolls
        canvas = self.editor_scroller.canvas
        self.editor_xbar = ttk.Scrollbar(self.editor_scroller, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=self.editor_xbar.set)
        canvas.bind("<Configure>", lambda e: self._fit_editor(e.width), add="+")
        box = self.editor_box = self.editor_scroller.inner
        box.columnconfigure(0, weight=1)
        self.form_holder = ttk.Frame(box)
        self.form_holder.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.form_holder.columnconfigure(0, weight=1)
        self.revision_label = ttk.Label(box, style="Muted.TLabel")
        self.revision_label.grid(row=1, column=0, sticky="w", padx=4, pady=(8, 0))
        self.changed_banner = _fit_wrap(ttk.Label(box, style="Warn.TLabel", wraplength=_WRAP, justify="left"))
        self.changed_banner.grid(row=2, column=0, sticky="ew", padx=4)
        self.changed_banner.grid_remove()
        self.problems_label = _Lines(box, style="Bad.TLabel", fit=True)
        self.problems_label.grid(row=3, column=0, sticky="ew", padx=4, pady=(4, 0))
        self.problems_label.show([])
        self.warnings_label = _Lines(box, style="Warn.TLabel", fit=True)
        self.warnings_label.grid(row=4, column=0, sticky="ew", padx=4)
        self.warnings_label.show([])
        buttons = ttk.Frame(box)
        buttons.grid(row=5, column=0, sticky="ew", padx=4, pady=(8, 4))
        self.save_button = ttk.Button(buttons, style="Hx.Main.TButton", command=self.save)
        texts.set(self.save_button, N_("Save"))
        self.save_button.pack(side="right")
        self.revert_button = ttk.Button(buttons, command=self.revert)
        texts.set(self.revert_button, N_("Revert"))
        self.revert_button.pack(side="right", padx=(0, 8))
        self.reload_button = ttk.Button(buttons, command=self.reload)
        texts.set(self.reload_button, N_("Reload"))
        self.reload_button.pack(side="right", padx=(0, 8))
        self.reload_button.pack_forget()
        self._buttons = buttons
        self.status_label = ttk.Label(self, style="Muted.TLabel", wraplength=900, justify="left")
        self.status_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.build_editors()
        self.refresh_list()
        self.update_save_state()

    def _fit_editor(self, width):
        """Runs after the Scroller's own <Configure> binding, which gives the editor the canvas's
        width: below FORM_MIN_PX it gets FORM_MIN_PX instead, with a sideways scrollbar."""
        canvas = self.editor_scroller.canvas
        narrow = width < FORM_MIN_PX
        for item in canvas.find_all():
            if canvas.type(item) == "window":
                canvas.itemconfigure(item, width=max(width, FORM_MIN_PX))
        if narrow and not self.editor_xbar.winfo_manager():
            self.editor_xbar.pack(side="bottom", fill="x", before=canvas)
        elif not narrow and self.editor_xbar.winfo_manager():
            self.editor_xbar.pack_forget()
            canvas.xview_moveto(0)

    def extra_buttons(self, top):
        """More buttons next to New (the products page's Add new version)."""

    def build_editors(self):
        """Make self.editor (and any other editor) inside self.form_holder."""

    def set_editor(self, form: _Form | None):
        if self.editor is not None and self.editor is not form:
            self.editor.grid_remove()
        self.editor = form
        if form is not None:
            form.grid(row=0, column=0, sticky="nsew")

    # what each page lists

    def collection(self, data) -> dict:
        return {}

    def records(self, data) -> list:
        return _sorted(self.collection(data).values())

    def values(self, record) -> tuple:
        return (record.name,)

    def record_name(self, record) -> str:
        return getattr(record, "name", "") or ""

    def inactive(self, record) -> bool:
        return getattr(record, "active", True) is False or getattr(record, "status", "") == "inactive"

    def find(self, data, record_id):
        return self.collection(data).get(record_id) if data is not None else None

    def new_record(self, **kw):
        raise NotImplementedError

    def editor_for(self, record) -> _Form | None:
        return self.editor

    # the list

    def refresh_list(self) -> None:
        data = self.services.data.current
        chosen = self.working.id if self.working is not None else ""
        # Selecting the record being edited (or nothing) is ignored by _on_select
        self.tree.delete(*self.tree.get_children())
        if data is not None:
            self.fill_tree(data)
        if chosen and self.tree.exists(chosen):
            self.tree.selection_set(chosen)
            self.tree.see(chosen)
        else:
            self.tree.selection_set(())

    def fill_tree(self, data):
        show_all = self.show_inactive_var.get()
        for record in self.records(data):
            if self.inactive(record) and not show_all:
                continue
            values = self.values(record)
            if self.tree_show == "tree headings":
                self.tree.insert("", "end", iid=record.id, text=values[0], values=values[1:])
            else:
                self.tree.insert("", "end", iid=record.id, values=values)

    def _on_select(self):
        chosen = self.tree.selection()
        record_id = chosen[0] if chosen else ""
        if not record_id or (self.working is not None and record_id == self.working.id):
            return
        if self._saving:
            # The save's answer comes back to this record: the editor doesn't change record meanwhile
            self._restore_selection()
            return
        if not self.leave():
            self._restore_selection()
            return
        record = self.find_any(record_id)
        if record is not None:
            self.load(record)

    def find_any(self, record_id):
        return self.find(self.services.data.current, record_id)

    def _restore_selection(self):
        if self.working is not None and self.tree.exists(self.working.id):
            self.tree.selection_set(self.working.id)
        else:
            self.tree.selection_set(())

    def select_record(self, record_id: str) -> None:
        """Select a record by id (with the leaving question)."""
        if not record_id or (self.working is not None and self.working.id == record_id) or self._saving:
            return
        record = self.find_any(record_id)
        if record is None or not self.leave():
            return
        if self.inactive(record) and not self.show_inactive_var.get():
            self.show_inactive_var.set(True)
        self.load(record)
        self.refresh_list()

    # editing

    def mark_dirty(self) -> None:
        if self.working is not None and not self.dirty:
            self.dirty = True
            self.update_save_state()

    def leave(self) -> bool:
        """True when the editor may change record: nothing unsaved, or the admin discards it."""
        if not self.dirty or self.working is None:
            return True
        if hx_ui_common.ask(self, Msg(N_("Discard your changes to {name}?"),
                                      {"name": self.record_name(self.working) or "?"}),
                            yes=N_("Discard"), no=N_("Keep editing")):
            self.dirty = False
            return True
        return False

    def leave_page(self) -> bool:
        """Leaving this page for another one: as leave(), and an edit the admin discards is dropped,
        so the page shows the record as saved when they come back (ui.md §11.1 step 4). A save in
        flight isn't asked about: its answer comes back to this page."""
        if self._saving or not self.dirty or self.working is None:
            return True
        if not self.leave():
            return False
        self.discard()
        return True

    def discard(self) -> None:
        """Drop the edits: the record as saved, or nothing for a record never saved."""
        record = None
        if self.working is not None and self.expected_revision is not None:
            record = self.find_any(self.working.id)
        if record is None:
            self.clear()
            self.problems_label.show([])
            self.warnings_label.show([])
        else:
            self.load(record)

    def load(self, record) -> None:
        self.working = copy.deepcopy(record)
        self.expected_revision = record.revision
        self.set_editor(self.editor_for(record))
        self.problems_label.show([])
        self.warnings_label.show([])
        self.changed_banner.grid_remove()
        self.reload_button.pack_forget()
        if self.editor is not None:
            self.editor.fill(self.working)
        self.dirty = False
        self.show_revision()
        self.update_save_state()

    def start_new(self, **kw) -> None:
        if self._saving or not self.leave():
            return
        record = self.new_record(**kw)
        self.working = record
        self.expected_revision = None
        self.set_editor(self.editor_for(record))
        self.problems_label.show([])
        self.warnings_label.show([])
        self.changed_banner.grid_remove()
        self.reload_button.pack_forget()
        self.tree.selection_set(())
        if self.editor is not None:
            self.editor.fill(self.working)
        self.dirty = True
        self.show_revision()
        self.update_save_state()

    def clear(self) -> None:
        self.working = None
        self.expected_revision = None
        self.dirty = False
        if self.editor is not None:
            self.editor.grid_remove()
        self.texts.forget(self.revision_label)
        self.revision_label.configure(text="")
        self.update_save_state()

    def show_revision(self):
        record = self.working
        if record is None:
            self.texts.forget(self.revision_label)
            self.revision_label.configure(text="")
        elif self.expected_revision is None:
            self.texts.set(self.revision_label, N_("Not saved yet."))
        else:
            self.texts.set(self.revision_label, N_("Revision {n} · saved by {user} at {time}"), n=record.revision,
                           user=record.updated_by, time=when(record.updated_at))

    def can_save(self) -> bool:
        services = self.services
        data = services.data.current
        return (self.working is not None and self.editor is not None and not self._saving and data is not None
                and services.store.available and data.source != "local" and roles(services).admin
                and self.editor.can_save())

    def update_save_state(self) -> None:
        _enable(self.save_button, self.can_save())
        _enable(self.revert_button, self.working is not None and not self._saving)
        _enable(self.new_button, not self._saving)

    # saving

    def read_record(self):
        record = copy.deepcopy(self.working)
        problems = []
        self.editor.read(record, problems)
        return record, problems

    def save(self) -> None:
        if not self.can_save():
            return
        self.editor.before_save()
        record, problems = self.read_record()
        if not problems:
            problems = self.editor.checks(record)
        self.problems_label.show(problems)
        self.reload_button.pack_forget()
        if problems:
            return
        self.warnings_label.show(self.editor.warnings(record))
        if not self.editor.questions(record):
            return
        self.save_record(record)

    def save_record(self, record) -> None:
        services = self.services
        expected = self.expected_revision
        self._saving = True
        self.update_save_state()
        self.status_label.configure(style="Muted.TLabel")
        self.texts.set(self.status_label, N_("Saving..."))

        def done(saved, error):
            if not _alive(self):
                return
            self._saving = False
            if not self.editing(record):
                self.answer_elsewhere(record, error)
            elif error is None:
                self.saved(saved)
            else:
                self.save_failed(error)
            self.update_save_state()
            self.window.save_answered(self, error is None)

        services.bg.run(lambda: services.data.save(record, expected_revision=expected), done)

    def editing(self, record) -> bool:
        """True while the editor still holds the record a save was started for. Nothing changes record
        during a save, so this only guards the answer of a save against an editor that moved on."""
        return self.working is not None and self.working.id == record.id

    def answer_elsewhere(self, record, error) -> None:
        """A save's answer when the editor holds another record: said in status_label only, so the
        other record's form isn't touched."""
        if error is None:
            self.status_label.configure(style="Ok.TLabel")
            self.texts.set(self.status_label, N_("Saved {name}."), name=self.record_name(record))
        else:
            self.status_label.configure(style="Bad.TLabel")
            self.texts.msg(self.status_label, _error_msg(error))
        self.update_save_state()

    def saved(self, saved) -> None:
        self.working = copy.deepcopy(saved)
        self.expected_revision = saved.revision
        if self.editor is not None:
            self.editor.fill(self.working)
        self.dirty = False
        self.changed_banner.grid_remove()
        self.reload_button.pack_forget()
        self.show_revision()
        self.status_label.configure(style="Ok.TLabel")
        self.texts.set(self.status_label, N_("Saved {name}."), name=self.record_name(saved))

    def save_failed(self, error) -> None:
        self.texts.forget(self.status_label)
        self.status_label.configure(text="")
        if isinstance(error, RevisionConflict):
            self.problems_label.show([error.msg])
            self.reload_button.pack(side="right", padx=(0, 8))
        elif isinstance(error, HxError):
            self.problems_label.show([error.msg])
        elif isinstance(error, ValueError):
            self.problems_label.show([Msg(N_("Stripes couldn't save this: {error}"), {"error": str(error)})])
        else:
            self.problems_label.show([_error_msg(error)])

    def revert(self) -> None:
        """The record as saved, after the discard question."""
        if self.working is None or self._saving or not self.leave():
            return
        self.discard()

    def reload(self) -> None:
        """Someone else saved: their version (after the discard question)."""
        self.revert()

    # updates

    def data_changed(self) -> None:
        data = self.services.data.current
        self.refresh_list()
        if self.working is not None and self.expected_revision is not None and data is not None:
            current = self.find_any(self.working.id)
            if current is not None and current.revision > self.expected_revision:
                self.texts.set(self.changed_banner, N_("{user} changed this record at {time}. Reload to see their "
                                                       "change; saving now would be refused."),
                               user=current.updated_by, time=when(current.updated_at))
                self.changed_banner.grid()
                self.reload_button.pack(side="right", padx=(0, 8))
        if self.editor is not None and self.working is not None:
            self.editor.refresh()
        self.update_save_state()

    def board_changed(self) -> None:
        if self.editor is not None and self.working is not None:
            self.editor.refresh()

    def retranslate(self) -> None:
        self.refresh_list()
        if self.editor is not None:
            self.editor.refresh_texts()
        _render_lines(self)


# --------------------------------------------------------------------------
# Company
# --------------------------------------------------------------------------

class _CompanyForm(_Form):
    """The company settings (ui.md §11.2)."""

    def __init__(self, master, page):
        super().__init__(master, page)
        self.countries: list[Country] = []
        self.inks: list[Ink] = []
        texts = self.texts
        row = 0
        self.label(row, N_("Time server"))
        servers = ttk.Frame(self)
        servers.grid(row=row, column=1, columnspan=2, sticky="w")
        ttk.Entry(servers, width=28, textvariable=self.var("time_server")).pack(side="left")
        fallback = ttk.Label(servers)
        texts.set(fallback, N_("Fallback"))
        fallback.pack(side="left", padx=(12, 6))
        ttk.Entry(servers, width=20, textvariable=self.var("time_server_fallback")).pack(side="left")

        # Countries
        row += 1
        self.section(row, N_("Countries (spelled exactly as printed)"))
        row += 1
        self.countries_tree = ttk.Treeview(self, columns=("country", "aliases", "offered"), show="headings", height=5,
                                           selectmode="browse")
        for column, english, width in (("country", N_("Country"), 120), ("aliases", N_("Also refused in other text"),
                                                                         240), ("offered", N_("Offered"), 70)):
            texts.heading(self.countries_tree, column, english)
            self.countries_tree.column(column, width=width)
        self.countries_tree.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.countries_tree.bind("<<TreeviewSelect>>", lambda e: self._country_chosen(), add="+")
        self.fields["countries"] = self.countries_tree
        self.add_country_button = ttk.Button(self, command=self._add_country)
        texts.set(self.add_country_button, N_("Add country"))
        self.add_country_button.grid(row=row, column=2, sticky="nw", padx=(8, 0))
        row += 1
        editor = ttk.Frame(self)
        editor.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.country_name_var = tk.StringVar()
        self.country_aliases_var = tk.StringVar()
        self.country_active_var = tk.BooleanVar(value=True)
        caption = ttk.Label(editor)
        texts.set(caption, N_("Country"))
        caption.pack(side="left")
        ttk.Entry(editor, width=16, textvariable=self.country_name_var).pack(side="left", padx=(4, 10))
        caption = ttk.Label(editor)
        texts.set(caption, N_("Also refused in other text"))
        caption.pack(side="left")
        ttk.Entry(editor, width=28, textvariable=self.country_aliases_var).pack(side="left", padx=(4, 10))
        offered = ttk.Checkbutton(editor, variable=self.country_active_var)
        texts.set(offered, N_("Offered"))
        offered.pack(side="left")
        self.apply_country_button = ttk.Button(editor, command=self._apply_country)
        texts.set(self.apply_country_button, N_("Apply"))
        self.apply_country_button.pack(side="left", padx=(10, 0))

        # Inks
        row += 1
        self.section(row, N_("Inks (the names the printers report)"))
        row += 1
        self.inks_tree = ttk.Treeview(self, columns=("name", "cartridge", "colour"), show="headings", height=4,
                                      selectmode="browse")
        for column, english, width in (("name", N_("Ink name"), 180), ("cartridge", N_("Cartridge"), 120),
                                       ("colour", N_("Colour"), 90)):
            texts.heading(self.inks_tree, column, english)
            self.inks_tree.column(column, width=width)
        self.inks_tree.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.inks_tree.bind("<<TreeviewSelect>>", lambda e: self._ink_chosen(), add="+")
        self.fields["inks"] = self.inks_tree
        ink_buttons = ttk.Frame(self)
        ink_buttons.grid(row=row, column=2, sticky="nw", padx=(8, 0))
        self.add_ink_button = ttk.Button(ink_buttons, command=self._add_ink)
        texts.set(self.add_ink_button, N_("Add ink"))
        self.add_ink_button.pack(fill="x")
        self.remove_ink_button = ttk.Button(ink_buttons, command=self._remove_ink)
        texts.set(self.remove_ink_button, N_("Remove ink"))
        self.remove_ink_button.pack(fill="x", pady=(4, 0))
        row += 1
        editor = ttk.Frame(self)
        editor.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 0))
        caption = ttk.Label(editor)
        texts.set(caption, N_("Ink name"))
        caption.pack(side="left")
        self.ink_name_var = tk.StringVar()
        ttk.Entry(editor, width=24, textvariable=self.ink_name_var).pack(side="left", padx=(4, 10))
        self.ink_cartridge_box = ChoiceBox(editor, CARTRIDGE_CHOICES, width=16)
        self.ink_cartridge_box.pack(side="left", padx=(0, 6))
        self.ink_colour_box = ChoiceBox(editor, COLOUR_CHOICES, width=10)
        self.ink_colour_box.pack(side="left", padx=(0, 6))
        self.apply_ink_button = ttk.Button(editor, command=self._apply_ink)
        texts.set(self.apply_ink_button, N_("Apply"))
        self.apply_ink_button.pack(side="left", padx=(4, 0))

        # Lot and free text
        row += 1
        limits = ttk.Frame(self)
        limits.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))
        for english, name, lo, hi in ((N_("Lot: from"), "lot_min_digits", 1, 20), (N_("to"), "lot_max_digits", 1, 20)):
            caption = ttk.Label(limits)
            texts.set(caption, english)
            caption.pack(side="left", padx=(0, 4))
            _, spin = self.spin(limits, name, english, lo, hi, width=4)
            spin.pack(side="left", padx=(0, 6))
        caption = ttk.Label(limits)
        texts.set(caption, N_("digits"))
        caption.pack(side="left", padx=(0, 24))
        caption = ttk.Label(limits)
        texts.set(caption, N_("Free text: at most"))
        caption.pack(side="left", padx=(0, 4))
        _, spin = self.spin(limits, "free_text_max", N_("Free text: at most"), 1, 100, width=4)
        spin.pack(side="left", padx=(0, 6))
        caption = ttk.Label(limits)
        texts.set(caption, N_("characters"))
        caption.pack(side="left")

        # Groups
        self.member_labels = {}
        for english, name, button_name in ((N_("Administrators group"), "admin_group", "admin_lookup_button"),
                                           (N_("Senders group"), "sender_group", "sender_lookup_button")):
            row += 1
            self.label(row, english)
            group = ttk.Frame(self)
            group.grid(row=row, column=1, columnspan=2, sticky="w", pady=(4, 0))
            ttk.Entry(group, width=26, textvariable=self.var(name)).pack(side="left")
            caption = ttk.Label(group)
            texts.set(caption, N_("SID"))
            caption.pack(side="left", padx=(10, 4))
            ttk.Entry(group, width=34, textvariable=self.var(name + "_sid")).pack(side="left")
            button = ttk.Button(group, command=lambda n=name: self._look_up(n))
            texts.set(button, N_("Look up"))
            button.pack(side="left", padx=(6, 0))
            setattr(self, button_name, button)
            row += 1
            member = ttk.Label(self, style="Muted.TLabel")
            member.grid(row=row, column=1, sticky="w")
            self.member_labels[name] = member

        # Standard fonts
        row += 1
        self.label(row, N_("Standard fonts"))
        fonts = ttk.Frame(self)
        fonts.grid(row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        self.font_vars = []
        for i in range(6):
            ttk.Label(fonts, text=str(i + 1)).pack(side="left", padx=(6 if i else 0, 2))
            var = tk.StringVar()
            var.trace_add("write", self.changed)
            ttk.Entry(fonts, width=12, textvariable=var).pack(side="left")
            self.font_vars.append(var)
        self.fields["standard_fonts"] = fonts
        row += 1
        self.side_hint(row, N_("Change these only when every printer's font list has changed: Stripes refuses to send "
                               "to a printer whose fonts differ."), column=1, columnspan=2)

        # Which counter counts bags (OPEN)
        row += 1
        self.label(row, N_("Which counter counts bags"))
        counters = ttk.Frame(self)
        counters.grid(row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        counter = self.var("bag_counter", "text")
        for value, english in (("PMCNT", N_("Print counter (PMCNT)")), ("MSCNT", N_("Message counter (MSCNT)")),
                               ("none", N_("No counter: time each test step"))):
            radio = ttk.Radiobutton(counters, variable=counter, value=value)
            texts.set(radio, english)
            radio.pack(anchor="w")

    def fill_extra(self, record):
        self.countries = copy.deepcopy(record.countries)
        self.inks = copy.deepcopy(record.inks)
        for var, name in zip(self.font_vars, list(record.standard_fonts) + [""] * 6):
            var.set(name)
        self._show_countries()
        self._show_inks()
        self.country_name_var.set("")
        self.country_aliases_var.set("")
        self.country_active_var.set(True)
        self.ink_name_var.set("")
        self.ink_cartridge_box.set_key("")
        self.ink_colour_box.set_key("")

    def read_extra(self, record, problems):
        record.countries = copy.deepcopy(self.countries)
        record.inks = copy.deepcopy(self.inks)
        record.standard_fonts = [v.get().strip() for v in self.font_vars]

    def on_change(self):
        identity = self.services.identity
        sids = {s.upper() for s in identity.sids}
        for name, label in self.member_labels.items():
            sid = self.fields[name + "_sid"].get().strip().upper()
            member = sid and sid in sids
            self.texts.set(label, N_("You are in this group.") if member else N_("You aren't in this group."))

    # countries and inks

    def _show_countries(self):
        tree = self.countries_tree
        chosen = tree.selection()
        tree.delete(*tree.get_children())
        for i, c in enumerate(self.countries):
            tree.insert("", "end", iid=str(i), values=(c.name, ", ".join(c.aliases), _yes_no(c.active)))
        if chosen and tree.exists(chosen[0]):
            tree.selection_set(chosen[0])

    def _country_from_editor(self):
        aliases = [a.strip() for a in self.country_aliases_var.get().split(",") if a.strip()]
        return Country(name=self.country_name_var.get(), aliases=aliases, active=bool(self.country_active_var.get()))

    def _country_chosen(self):
        chosen = self.countries_tree.selection()
        if not chosen:
            return
        c = self.countries[int(chosen[0])]
        self.country_name_var.set(c.name)
        self.country_aliases_var.set(", ".join(c.aliases))
        self.country_active_var.set(c.active)

    def _add_country(self):
        country = self._country_from_editor()
        if not country.name.strip():
            return
        self.countries.append(country)
        self._show_countries()
        self.countries_tree.selection_set(str(len(self.countries) - 1))
        self.changed()

    def _apply_country(self):
        chosen = self.countries_tree.selection()
        if not chosen:
            return
        self.countries[int(chosen[0])] = self._country_from_editor()
        self._show_countries()
        self.changed()

    def _show_inks(self):
        tree = self.inks_tree
        chosen = tree.selection()
        tree.delete(*tree.get_children())
        for i, ink in enumerate(self.inks):
            tree.insert("", "end", iid=str(i), values=(ink.name, _choice_text(CARTRIDGE_CHOICES, ink.cartridge),
                                                       _choice_text(COLOUR_CHOICES, ink.colour)))
        if chosen and tree.exists(chosen[0]):
            tree.selection_set(chosen[0])

    def _ink_chosen(self):
        chosen = self.inks_tree.selection()
        if not chosen:
            return
        ink = self.inks[int(chosen[0])]
        self.ink_name_var.set(ink.name)
        self.ink_cartridge_box.set_key(ink.cartridge)
        self.ink_colour_box.set_key(ink.colour)

    def _ink_from_editor(self):
        return Ink(name=self.ink_name_var.get().strip(), cartridge=self.ink_cartridge_box.key(),
                   colour=self.ink_colour_box.key())

    def _add_ink(self):
        ink = self._ink_from_editor()
        if not ink.name:
            return
        self.inks.append(ink)
        self._show_inks()
        self.changed()

    def _apply_ink(self):
        chosen = self.inks_tree.selection()
        if not chosen:
            return
        self.inks[int(chosen[0])] = self._ink_from_editor()
        self._show_inks()
        self.changed()

    def _remove_ink(self):
        chosen = self.inks_tree.selection()
        if not chosen:
            return
        del self.inks[int(chosen[0])]
        self.inks_tree.selection_set(())
        self._show_inks()
        self.changed()

    def _look_up(self, name):
        services = self.services
        group = self.fields[name].get().strip()

        def done(sid, error):
            if not _alive(self):
                return
            if error is None and sid:
                self.fields[name + "_sid"].set(sid)
                return
            hx_ui_common.tell(self, Msg(N_("Windows couldn't find a group called {name}."), {"name": group}))

        services.bg.run(lambda: hx_win.lookup_group_sid(group), done)

    # checks

    def checks(self, record):
        return hx_message.validate_company(record)

    def questions(self, record):
        identity = self.services.identity
        sids = {s.upper() for s in identity.sids}
        if (record.admin_group_sid or "").strip().upper() not in sids:
            if not hx_ui_common.ask(self, Msg(N_("You aren't in {group}. After saving, you won't be able to change the "
                                                 "MapleJet settings. Save anyway?"), {"group": record.admin_group}),
                                    yes=N_("Save anyway"), no=N_("Go back")):
                return False
        conflicts = hx_message.origin_conflicts(self.services.data.current, record)
        if conflicts:
            listed = "\n" + "\n".join(text(m) for m in conflicts) + "\n"
            if not hx_ui_common.ask(self, Msg(N_("With this country list, these records contain words about origin "
                                                 "and can't be sent until they're fixed: {list} Save anyway?"),
                                              {"list": listed}),
                                    yes=N_("Save anyway"), no=N_("Go back")):
                return False
        return True

    def refresh_texts(self):
        self._show_countries()
        self._show_inks()


class CompanyPage(AdminPage):
    key = "company"
    tab_text = N_("Company")

    def _build(self):
        super()._build()
        self.list_frame.grid_remove()
        self.show_inactive_check.pack_forget()
        self.columnconfigure(0, weight=0)

    def build_editors(self):
        self.set_editor(_CompanyForm(self.form_holder, self))

    def collection(self, data):
        return {"company": data.company} if data is not None else {}

    def record_name(self, record):
        return tr("Company")

    def refresh_list(self):
        if self.working is None:
            data = self.services.data.current
            if data is not None:
                self.load(data.company)


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------

class _LineForm(_Form):
    """A line (ui.md §11.3)."""

    def __init__(self, master, page):
        super().__init__(master, page)
        self.entry(0, "name", N_("Name"))
        self.entry(1, "code", N_("Printed code"), width=5,
                   hint=N_("Up to 3 capital letters or digits, printed by a Line code item."))
        self.label(2, N_("Cartridge type"))
        self.choice(self, "cartridge", CARTRIDGE_CHOICES, width=16).grid(row=2, column=1, sticky="w", pady=2)
        self.side_hint(2, N_("Only films of the same type are offered at Send."))
        self.label(3, N_("Notify user"))
        notify = ttk.Frame(self)
        notify.grid(row=3, column=1, columnspan=2, sticky="w")
        ttk.Entry(notify, width=26, textvariable=self.var("notify_user")).pack(side="left")
        self.me_button = ttk.Button(notify, command=lambda: self.fields["notify_user"].set(self.services.identity.user))
        self.texts.set(self.me_button, N_("Me"))
        self.me_button.pack(side="left", padx=(6, 0))
        # Under the entry and its button, which are too wide to leave the hint room beside them
        self.side_hint(4, N_("DOMAIN\\name. Gets this line's alerts on whichever PC they're signed in to."), column=1,
                       columnspan=2)
        _, active = self.check(self, "active", N_("Active"))
        active.grid(row=5, column=1, sticky="w", pady=2)
        self.label(6, N_("Machines"))
        self.machines_label = ttk.Label(self, wraplength=400, justify="left")
        self.machines_label.grid(row=6, column=1, columnspan=2, sticky="w")
        self.add_machine_button = ttk.Button(self, command=self._add_machine)
        self.texts.set(self.add_machine_button, N_("Add a machine"))
        self.add_machine_button.grid(row=7, column=1, sticky="w", pady=(4, 0))

    def fill_extra(self, record):
        self.refresh()

    def refresh(self):
        data = self.services.data.current
        record = self.page.working
        if data is None or record is None:
            return
        names = [m.name for m in data.line_machines(record.id)]
        self.machines_label.configure(text="\n".join(names))      # record names: data
        _enable(self.add_machine_button, self.page.expected_revision is not None)

    def _add_machine(self):
        record = self.page.working
        if record is None or self.page.expected_revision is None:
            return
        self.window.select("machines")
        if self.window.current_page() is self.window.pages["machines"]:
            self.window.pages["machines"].start_new(line_id=record.id)

    def checks(self, record):
        return hx_message.validate_line(record, self.services.data.current) + notify_user_problems(record.notify_user)


class LinesPage(AdminPage):
    key = "lines"
    tab_text = N_("Lines")
    new_text = N_("New line")
    columns = (("name", N_("Name"), 140), ("code", N_("Code"), 50), ("cartridge", N_("Cartridge"), 110),
               ("notify", N_("Notify user"), 140), ("active", N_("Active"), 60))

    def build_editors(self):
        self.set_editor(_LineForm(self.form_holder, self))

    def collection(self, data):
        return data.lines

    def values(self, line):
        return (line.name, line.code, _choice_text(CARTRIDGE_CHOICES, line.cartridge), line.notify_user,
                _yes_no(line.active))

    def new_record(self, **kw):
        return Line(id=new_id("ln"), name="", code="", cartridge="")


# --------------------------------------------------------------------------
# Machines
# --------------------------------------------------------------------------

class _MachineForm(_Form):
    """A machine and its placement mode (ui.md §11.4)."""

    def __init__(self, master, page):
        super().__init__(master, page)
        texts = self.texts
        self.entry(0, "name", N_("Name"))
        self.entry(1, "code", N_("Printed code"), width=5,
                   hint=N_("Up to 3 capital letters or digits, printed by a Machine code item."))
        self.label(2, N_("Line"))
        self.line_box = self.records(self, "line_id")
        self.line_box.grid(row=2, column=1, columnspan=2, sticky="w", pady=2)
        self.label(3, N_("Printer"))
        self.printer_box = self.records(self, "printer_id")
        self.printer_box.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        self.side_hint(4, N_("A printer belongs to one machine."), column=1, columnspan=2)     # under the wide box
        self.label(5, N_("Placement mode"))
        placement = self.var("placement", "text")
        radios = ttk.Frame(self)
        radios.grid(row=5, column=1, columnspan=2, sticky="w")
        for value in ("delay", "encoder"):
            radio = ttk.Radiobutton(radios, variable=placement, value=value)
            texts.set(radio, PLACEMENT_TEXTS[value])
            radio.pack(side="left", padx=(0, 12))
        self.placement_hint = _fit_wrap(ttk.Label(self, style="Muted.TLabel", wraplength=_WRAP, justify="left"))
        self.placement_hint.grid(row=6, column=1, columnspan=2, sticky="ew")
        self.entry(7, "print_speed", N_("Print speed (cm/min)"), kind="whole", width=8,
                   hint=N_("Needed before this machine can be sent to."))
        flags = ttk.Frame(self)
        flags.grid(row=8, column=1, columnspan=2, sticky="ew", pady=2)
        _, rotate = self.check(flags, "rotate", N_("Rotate"))
        rotate.pack(side="left")
        _, mirror = self.check(flags, "mirror", N_("Mirror"))
        mirror.pack(side="left", padx=(12, 0))
        self.hint(flags, N_("Depend on how the printer is mounted."), fit=True).pack(side="left", fill="x",
                                                                                    expand=True, padx=(12, 0))
        self.entry(9, "seconds_per_bag", N_("Seconds per bag"), kind="number", width=8, required=True,
                   hint=N_("Used to time test steps when no counter counts bags."))

        # The two expected values on rows of their own, so the frame fits the window's own size
        self.encoder_frame = ttk.LabelFrame(self, padding=8)
        texts.set(self.encoder_frame, N_("Encoder (encoder machines only)"))
        self.encoder_frame.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.encoder_frame.columnconfigure(1, weight=1)
        self.head_label = _fit_wrap(ttk.Label(self.encoder_frame, wraplength=480, justify="left"), cap=480)
        self.head_label.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.calibrate_button = ttk.Button(self.encoder_frame, command=self._calibrate)
        texts.set(self.calibrate_button, N_("Calibrate..."))
        self.calibrate_button.grid(row=0, column=2, sticky="ne", padx=(8, 0))
        self.encoder_entries = []
        for n, (name, english, kind) in enumerate((("encoder_wheel_mm", N_("Expected wheel size (mm)"), "number"),
                                                   ("encoder_ppr", N_("Expected pulses per turn"), "whole"))):
            caption = ttk.Label(self.encoder_frame)
            texts.set(caption, english)
            caption.grid(row=1 + n, column=0, sticky="w", pady=(6, 0), padx=(0, 8))
            entry = ttk.Entry(self.encoder_frame, width=8, textvariable=self.var(name, kind, label=english))
            entry.grid(row=1 + n, column=1, sticky="w", pady=(6, 0))
            self.encoder_entries.append(entry)
        self.hint(self.encoder_frame, N_("Stripes reads these from the printer every day and raises an alert if they "
                                         "change."), fit=True).grid(row=3, column=0, columnspan=3, sticky="ew",
                                                                    pady=(4, 0))
        _, active = self.check(self, "active", N_("Active"))
        active.grid(row=11, column=1, sticky="w", pady=2)
        self.label(12, N_("Now:"))
        self.now_label = ttk.Label(self)
        self.now_label.grid(row=12, column=1, columnspan=2, sticky="w")

    def prepare(self, record):
        data = self.services.data.current
        lines = [ln for ln in data.lines.values() if ln.active or ln.id == record.line_id]
        self.line_box.set_records([(ln.id, ln.name) for ln in _sorted(lines)])
        used = {m.printer_id for m in data.machines.values() if m.active and m.id != record.id}
        printers = [p for p in data.printers.values()
                    if (p.active and p.id not in used) or p.id == record.printer_id]
        self.printer_box.set_records([(p.id, f"{p.name} ({p.host})") for p in _sorted(printers)])

    def fill_extra(self, record):
        self.refresh()

    def on_change(self):
        placement = self.fields["placement"].get()
        if placement == "encoder":
            self.texts.set(self.placement_hint, N_("Encoder: each film's print position in mm, worked out from this "
                                                   "machine's head distance. Calibrate it once, at installation."))
        elif placement == "delay":
            self.texts.set(self.placement_hint, N_("Delay: each film's delay after the trigger, plus this machine's "
                                                   "delay adjustment for that film. No calibration."))
        else:
            self.texts.forget(self.placement_hint)
            self.placement_hint.configure(text="")
        encoder = placement == "encoder"
        for entry in self.encoder_entries:
            _enable(entry, encoder)
        self._show_calibrate()

    def _show_calibrate(self):
        page = self.page
        record = page.working
        saved = page.expected_revision is not None
        encoder = self.fields["placement"].get() == "encoder"
        _enable(self.calibrate_button, record is not None and saved and not page.dirty and encoder)

    def refresh(self):
        data = self.services.data.current
        record = self.page.working
        if data is None or record is None:
            return
        cal = data.calibrations.get(record.id)
        current = data.machines.get(record.id)
        head = current.head_distance_mm if current is not None else record.head_distance_mm
        if head is None:
            self.texts.set(self.head_label, N_("Head distance: not calibrated. This machine can't be sent to until "
                                               "it is."))
        else:
            self.texts.set(self.head_label, N_("Head distance: {mm} mm (calibrated by {user} at {time})"),
                           mm=_num(head), user=cal.head_by if cal is not None else "",
                           time=when(cal.head_at) if cal is not None else "")
        self.refresh_texts()
        self._show_calibrate()

    def refresh_texts(self):
        record = self.page.working
        view = None
        board = self.services.watcher.latest
        if board is not None and record is not None:
            for line in board.lines:
                for v in line.machines:
                    if v.machine_id == record.id:
                        view = v
        if view is None:
            self.now_label.configure(text="")
        else:
            self.now_label.configure(text=f"{text(view.headline)} · {text(hx_state.checked_text(view.checked_age_s))}")

    def _calibrate(self):
        record = self.page.working
        if record is None:
            return
        services = self.services
        window = services.nav.window(f"calibration:{record.id}")
        if window is not None:
            window.show()
            return
        CalibrationWindow(self.window, services, machine_id=record.id)

    def checks(self, record):
        return hx_message.validate_machine(record, self.services.data.current)

    def questions(self, record):
        data = self.services.data.current
        saved = data.machines.get(record.id) if self.page.expected_revision is not None else None
        if saved is not None and saved.placement != record.placement:
            return hx_ui_common.ask(self, Msg(N_("Change the placement mode of {machine}? Other film values and "
                                                 "adjustments then apply, so check every film on this machine with a "
                                                 "test print."), {"machine": record.name}),
                                    yes=N_("Change it"), no=N_("Go back"))
        return True


class MachinesPage(AdminPage):
    key = "machines"
    tab_text = N_("Machines")
    new_text = N_("New machine")
    columns = (("name", N_("Name"), 150), ("code", N_("Code"), 50), ("line", N_("Line"), 100),
               ("placement", N_("Placement"), 80), ("printer", N_("Printer"), 120), ("active", N_("Active"), 60))

    def build_editors(self):
        self.set_editor(_MachineForm(self.form_holder, self))

    def collection(self, data):
        return data.machines

    def values(self, machine):
        data = self.services.data.current
        line = data.lines.get(machine.line_id)
        printer = data.printers.get(machine.printer_id)
        return (machine.name, machine.code, line.name if line is not None else "",
                _choice_text(list(PLACEMENT_TEXTS.items()), machine.placement),
                printer.name if printer is not None else "", _yes_no(machine.active))

    def new_record(self, *, line_id="", **kw):
        return Machine(id=new_id("mc"), name="", code="", line_id=line_id, printer_id="", placement="")

    def mark_dirty(self):
        super().mark_dirty()
        if isinstance(self.editor, _MachineForm):
            self.editor._show_calibrate()


# --------------------------------------------------------------------------
# Printers
# --------------------------------------------------------------------------

class _PrinterForm(_Form):
    """A printer; its password is typed once and kept only as a scrambled token (ui.md §11.5)."""

    def __init__(self, master, page):
        super().__init__(master, page)
        self.entry(0, "name", N_("Name"))
        self.entry(1, "host", N_("IP address"), width=18, hint=N_("The printer's fixed address."))
        self.entry(2, "port", N_("Port"), kind="whole", width=8, required=True, hint=N_("80 for a real printer."))
        self.entry(3, "uid", N_("UID"), kind="whole", width=12, required=True,
                   hint=N_("Checked on every poll and Send."))
        self.label(4, N_("Password"))
        # No textvariable: the password is never in a Tk variable
        self.password_entry = ttk.Entry(self, show="•", width=24)
        self.password_entry.grid(row=4, column=1, sticky="w", pady=2)
        self.password_entry.bind("<KeyRelease>", lambda e: self.changed() if self.password_entry.get() else None,
                                 add="+")
        self.password_status = _fit_wrap(ttk.Label(self, wraplength=360, justify="left"), cap=360)
        self.password_status.grid(row=4, column=2, sticky="ew", padx=(8, 0))
        self.side_hint(5, N_("Stripes keeps only a scrambled token made from the password, never the password itself."),
                       column=1, columnspan=2)
        _, active = self.check(self, "active", N_("Active"))
        active.grid(row=6, column=1, sticky="w", pady=2)
        self.label(7, N_("Used by:"))
        self.used_label = ttk.Label(self)
        self.used_label.grid(row=7, column=1, columnspan=2, sticky="w")

    def fill_extra(self, record):
        self.password_entry.delete(0, "end")
        self._show_status(record)
        self.refresh()

    def read_extra(self, record, problems):
        # Spaces typed around the address are dropped: no socket reaches " 192.0.2.10 "
        record.host = (record.host or "").strip()

    def _show_status(self, record):
        if not record.token:
            self.texts.set(self.password_status, N_("No password saved yet. Stripes can't check this printer until "
                                                    "one is."))
            return
        try:
            hx_protocol.unscramble(record.token, hx_protocol.release_key())
        except AuthFailed:
            self.texts.set(self.password_status, N_("The saved password was made by a different Stripes build. Type "
                                                    "it again."))
            return
        self.texts.set(self.password_status, N_("A password is saved. Type a new one only to change it."))

    def before_save(self):
        """Only the scrambled token goes into the working copy and the record; the entry is cleared
        at once, so a failed save can be tried again without typing it (protocol.md §3.1)."""
        pw = self.password_entry.get()
        if pw:
            self.page.working.token = hx_protocol.scramble(hx_protocol.make_token(pw), hx_protocol.release_key())
            self.password_entry.delete(0, "end")
            self._show_status(self.page.working)
        del pw

    def refresh(self):
        self.refresh_texts()

    def refresh_texts(self):
        data = self.services.data.current
        record = self.page.working
        if data is None or record is None:
            return
        machine = next((m for m in _sorted(data.machines.values()) if m.active and m.printer_id == record.id), None)
        if machine is None:
            self.texts.set(self.used_label, N_("Not used by any machine yet."))
            return
        self.texts.forget(self.used_label)
        shown = machine.name
        board = self.services.watcher.latest
        for line in board.lines if board is not None else []:
            for view in line.machines:
                if view.machine_id == machine.id:
                    shown += f" · {text(view.headline)} · {text(hx_state.checked_text(view.checked_age_s))}"
        self.used_label.configure(text=shown)

    def checks(self, record):
        return printer_problems(record, self.services.data.current)


class PrintersPage(AdminPage):
    key = "printers"
    tab_text = N_("Printers")
    new_text = N_("New printer")
    columns = (("name", N_("Name"), 130), ("host", N_("IP address"), 110), ("uid", N_("UID"), 60),
               ("password", N_("Password"), 80), ("active", N_("Active"), 60))

    def build_editors(self):
        self.set_editor(_PrinterForm(self.form_holder, self))

    def collection(self, data):
        return data.printers

    def values(self, printer):
        return (printer.name, printer.host, printer.uid, _yes_no(bool(printer.token)), _yes_no(printer.active))

    def new_record(self, **kw):
        return Printer(id=new_id("pr"), name="", host="", port=80, uid=0)


# --------------------------------------------------------------------------
# Products and films
# --------------------------------------------------------------------------

class ProductEditor(_Form):
    """A product: name, customer, active and its films (ui.md §11.6)."""

    def __init__(self, master, page):
        super().__init__(master, page)
        self.entry(0, "name", N_("Name"), width=40)
        self.entry(1, "customer", N_("Customer"), width=40)
        _, active = self.check(self, "active", N_("Active"))
        active.grid(row=2, column=1, sticky="w", pady=2)
        self.label(3, N_("Films"))
        self.films_label = ttk.Label(self, justify="left")
        self.films_label.grid(row=3, column=1, sticky="w")

    def fill_extra(self, record):
        self.refresh()

    def refresh(self):
        data = self.services.data.current
        record = self.page.working
        if data is None or not isinstance(record, Product):
            return
        films = sorted((f for f in data.films.values() if f.product_id == record.id), key=_film_key)
        self.films_label.configure(text="\n".join(film_name(f) for f in films))   # data

    def checks(self, record):
        return product_problems(record, self.services.data.current)


class FilmEditor(_Form):
    """Every field of a film, its files and photos, its machine adjustments and the test print
    (ui.md §11.7)."""

    def __init__(self, master, page):
        super().__init__(master, page)
        texts = self.texts
        self.pt_ids: list[str] = []
        self.pt_vars: dict[str, tk.BooleanVar] = {}
        self.thumbs: dict[str, ttk.Label] = {}
        self._photos = {}
        row = 0
        self.product_label = ttk.Label(self, style="Hx.Section.TLabel")
        self.product_label.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.new_note = _fit_wrap(ttk.Label(self, style="Warn.TLabel", wraplength=_WRAP, justify="left"))
        texts.set(self.new_note, N_("A new artwork revision is a new film. It starts blank and needs its own test "
                                    "print."))
        self.new_note.grid(row=row, column=0, columnspan=3, sticky="ew")
        row += 1
        self.entry(row, "code", N_("Code"), width=8, hint=N_("Up to 6 capital letters or digits, used in the "
                                                             "printer's message name. A new film needs a new code."))
        row += 1
        self.entry(row, "artwork_revision", N_("Artwork revision"), width=12)
        row += 1
        self.label(row, N_("Status"))
        self.choice(self, "status", STATUS_CHOICES, width=16).grid(row=row, column=1, sticky="w", pady=2)
        self.side_hint(row, N_("Only Active films are offered at Send."))
        row += 1
        self.revision_text = ttk.Label(self)
        self.revision_text.grid(row=row, column=1, sticky="w")
        self.side_hint(row, N_("Goes up on every save."))

        row += 1
        self.section(row, N_("Bag"))
        row += 1
        side = self.var("side", "text")
        sides = ttk.Frame(self)
        sides.grid(row=row, column=0, columnspan=3, sticky="w")
        for value, english in (("back", N_("Back film: the code goes on the back")),
                               ("front", N_("Front film: the code goes on the front"))):
            radio = ttk.Radiobutton(sides, variable=side, value=value)
            texts.set(radio, english)
            radio.pack(side="top", anchor="w")        # one per row: both fit a narrow editor
        row += 1
        self.label(row, N_("Paper or plastic"))
        self.choice(self, "cartridge", CARTRIDGE_CHOICES, width=16).grid(row=row, column=1, sticky="w", pady=2)
        self.side_hint(row, N_("Must match the line's cartridge type."))
        row += 1
        self.label(row, N_("Ink colour"))
        self.choice(self, "ink_colour", COLOUR_CHOICES, width=10).grid(row=row, column=1, sticky="w", pady=2)
        self.side_hint(row, N_("Checked against the ink the printer reports."))

        row += 1
        self.section(row, N_("Placement"))
        for name, english, hint in (
                ("length_mm", N_("Film length (mm)"), N_("The bag's cut-off length, from the spec sheet. Needed for "
                                                         "encoder machines.")),
                ("position_mm", N_("Print position (mm)"), N_("From the bag's bottom edge, the edge that feeds in "
                                                              "first. Needed for encoder machines. Found with the test "
                                                              "print.")),
                ("delay_ms", N_("Delay (ms)"), N_("Time from the trigger to the print. Needed for delay machines. "
                                                  "Found with the test print.")),
                ("print_length_mm", N_("Print length (mm)"), N_("How long the code is along the film, for the warning "
                                                                "when a print would still be running as the film "
                                                                "stops. Leave empty if unknown."))):
            row += 1
            self.entry(row, name, english, kind="number", width=10, hint=hint)

        row += 1
        self.section(row, N_("Print settings"))
        row += 1
        self.label(row, N_("Font"))
        self.font_box = self.records(self, "font", kind="record_int", width=18)
        self.font_box.grid(row=row, column=1, sticky="w", pady=2)
        self.side_hint(row, N_("One font and size for the whole print text."))
        row += 1
        settings = ttk.Frame(self)
        settings.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        for name, english, lo, hi in (("size", N_("Size"), 1, 10), ("bold", N_("Bold"), 1, 10),
                                      ("width", N_("Width"), 1, 10)):
            caption = ttk.Label(settings)
            texts.set(caption, english)
            caption.pack(side="left", padx=(0, 4))
            _, spin = self.spin(settings, name, english, lo, hi, width=4)
            spin.pack(side="left", padx=(0, 14))
        row += 1
        self.label(row, N_("Resolution"))
        self.choice(self, "resolution", RESOLUTION_CHOICES, kind="choice_int", width=12).grid(row=row, column=1,
                                                                                            sticky="w", pady=2)

        row += 1
        self.section(row, N_("Origin"))
        row += 1
        _, preprinted = self.check(self, "origin_preprinted", N_("The origin is pre-printed on the film"))
        preprinted.grid(row=row, column=0, columnspan=2, sticky="w")
        self.origin_hint = _fit_wrap(ttk.Label(self, style="Muted.TLabel", wraplength=_WRAP, justify="left"))
        self.origin_hint.grid(row=row, column=2, sticky="ew", padx=(8, 0))
        row += 1
        self.label(row, N_("Pre-printed origin"))
        origin = ttk.Frame(self)
        origin.grid(row=row, column=1, columnspan=2, sticky="w")
        self.preprinted_entry = ttk.Entry(origin, width=24, textvariable=self.var("preprinted_origin"))
        self.preprinted_entry.pack(side="left")
        self.origin_preview = BagText(origin, size=13, bold=True)
        self.origin_preview.pack(side="left", padx=(10, 0))
        row += 1
        self.side_hint(row, N_("Shown at Send, on the floor board and in the log."), column=1, columnspan=2)
        self._last_preprinted = None

        row += 1
        self.section(row, N_("Print texts"))
        row += 1
        self.print_texts_frame = ttk.Frame(self)
        self.print_texts_frame.grid(row=row, column=0, columnspan=3, sticky="w")
        self.fields["print_text_ids"] = self.print_texts_frame
        row += 1
        self.unticked_note = _Lines(self, style="Warn.TLabel", fit=True)
        self.unticked_note.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.unticked_note.show([])
        row += 1
        self.label(row, N_("Default"))
        self.default_box = self.records(self, "default_print_text_id", width=30)
        self.default_box.grid(row=row, column=1, columnspan=2, sticky="w", pady=2)
        row += 1
        self.side_hint(row, N_("Pre-filled at Send."), column=1, columnspan=2)       # under the wide box

        row += 1
        self.section(row, N_("Files and photos"))
        row += 1
        photos = ttk.Frame(self)
        photos.grid(row=row, column=0, columnspan=3, sticky="w")
        for kind in ("front_photo", "back_photo"):
            thumb = ttk.Label(photos, style="Muted.TLabel", cursor="hand2")
            thumb.pack(side="left", padx=(0, 12))
            self.thumbs[kind] = thumb
        row += 1
        self.files_tree = ttk.Treeview(self, columns=("kind", "name", "size", "by", "at"), show="headings", height=4,
                                       selectmode="browse")
        for column, english, width in (("kind", N_("Kind"), 80), ("name", N_("Name"), 140), ("size", N_("Size"), 60),
                                       ("by", N_("Added by"), 100), ("at", N_("Added"), 100)):
            texts.heading(self.files_tree, column, english)
            self.files_tree.column(column, width=width)
        self.files_tree.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.fields["files"] = self.files_tree
        row += 1
        files = ttk.Frame(self)
        files.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        caption = ttk.Label(files)
        texts.set(caption, N_("Add as"))
        caption.pack(side="left", padx=(0, 4))
        self.add_kind_box = ChoiceBox(files, list(FILE_KIND_TEXTS.items()), width=14)
        self.add_kind_box.pack(side="left", padx=(0, 6))
        self.add_file_button = ttk.Button(files, command=self._add_file)
        texts.set(self.add_file_button, N_("Add file..."))
        self.add_file_button.pack(side="left")
        row += 1
        chosen = ttk.Frame(self)        # a row of their own, so the buttons fit the window's own size in Spanish
        chosen.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.open_file_button = ttk.Button(chosen, command=self._open_file)
        texts.set(self.open_file_button, N_("Open"))
        self.open_file_button.pack(side="left", padx=(0, 6))
        self.remove_file_button = ttk.Button(chosen, command=self._remove_file)
        texts.set(self.remove_file_button, N_("Remove from film"))
        self.remove_file_button.pack(side="left")
        row += 1
        self.files_note = _Lines(self, style="Muted.TLabel", fit=True)
        self.files_note.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.files_note.show([Msg(N_("Originals stay on the server. Each PC keeps small previews of the photos."))])

        row += 1
        self.section(row, N_("Machine adjustments"))
        row += 1
        self.adjust_tree = ttk.Treeview(self, columns=("machine", "placement", "adjustment", "by", "at"),
                                        show="headings", height=4, selectmode="browse")
        for column, english, width in (("machine", N_("Machine"), 130), ("placement", N_("Placement"), 70),
                                       ("adjustment", N_("Adjustment"), 80), ("by", N_("Set by"), 100),
                                       ("at", N_("Set at"), 100)):
            texts.heading(self.adjust_tree, column, english)
            self.adjust_tree.column(column, width=width)
        self.adjust_tree.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.adjust_tree.bind("<<TreeviewSelect>>", lambda e: self._show_buttons(), add="+")
        row += 1
        adjust = ttk.Frame(self)
        adjust.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.adjust_button = ttk.Button(adjust, command=self._edit_adjustment)
        texts.set(self.adjust_button, N_("Edit adjustment..."))
        self.adjust_button.pack(side="left")
        self.hint(adjust, N_("Ignored during a test print.")).pack(side="left", padx=(8, 0))
        row += 1
        test = ttk.Frame(self)
        test.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.test_button = ttk.Button(test, command=self._test_print)
        texts.set(self.test_button, N_("Test print..."))
        self.test_button.pack(side="left")
        self.test_hint = self.hint(test, N_("Save the film first."))
        self.test_hint.pack(side="left", padx=(8, 0))

    # filling

    def prepare(self, record):
        data = self.services.data.current
        fonts = list(data.company.standard_fonts) if data is not None else []
        self.font_box.set_records([(str(i), f"{i}: {name}") for i, name in enumerate(fonts, start=1)])

    def fill_extra(self, record):
        data = self.services.data.current
        product = data.products.get(record.product_id) if data is not None else None
        self.texts.set(self.product_label, N_("Product: {name}"), name=product.name if product is not None else "")
        self.texts.set(self.revision_text, N_("Settings revision {n}"), n=record.revision)
        if self.page.expected_revision is None:
            self.new_note.grid()
        else:
            self.new_note.grid_remove()
        self.pt_ids = list(record.print_text_ids)
        self._last_preprinted = record.origin_preprinted
        self.unticked_note.show([])
        self._show_print_texts()
        self.default_box.set_record_id(record.default_print_text_id)
        self._show_files()
        self._load_thumbs()
        self._show_adjustments()
        self.add_kind_box.set_key(self._next_kind())

    def _film(self):
        return self.page.working

    def _show_print_texts(self):
        """A Checkbutton per print text that suits the origin setting; the default offers the ticked ones."""
        texts = self.texts
        _clear(self.print_texts_frame, texts)
        self.pt_vars = {}
        data = self.services.data.current
        film = copy.deepcopy(self._film())
        film.origin_preprinted = bool(self.fields["origin_preprinted"].get())
        choices = print_text_choices(film, data) if data is not None else []
        # A ticked print text that no longer suits the film is listed too, so it can be unticked
        listed = {pt.id for pt in choices}
        choices += [data.print_texts[i] for i in self.pt_ids
                    if data is not None and i in data.print_texts and i not in listed]
        for i, pt in enumerate(choices):
            var = tk.BooleanVar(value=pt.id in self.pt_ids)
            check = ttk.Checkbutton(self.print_texts_frame, variable=var,
                                    text=f"{pt.name} ({pt.code}) · {_choice_text(STATUS_CHOICES, pt.status)}",
                                    command=lambda pid=pt.id: self._pt_toggled(pid))
            check.grid(row=i, column=0, sticky="w")        # one per row: long names fit a narrow editor
            self.pt_vars[pt.id] = var
        ticked = [data.print_texts[i] for i in self.pt_ids if data is not None and i in data.print_texts]
        chosen = self.default_box.record_id()
        self.default_box.set_records([(pt.id, pt.name) for pt in ticked])
        self.default_box.set_record_id(chosen if chosen in self.pt_ids else "")

    def _pt_toggled(self, pt_id):
        if self.pt_vars[pt_id].get():
            if pt_id not in self.pt_ids:
                self.pt_ids.append(pt_id)       # ticking appends at the end
        else:
            self.pt_ids = [i for i in self.pt_ids if i != pt_id]
        self._show_print_texts()
        self.changed()

    def on_change(self):
        preprinted = bool(self.fields["origin_preprinted"].get())
        self.texts.set(self.origin_hint, N_("Only print texts without an origin item can be used.") if preprinted
                       else N_("Only print texts with an origin item can be used."))
        _enable(self.preprinted_entry, preprinted)
        self.origin_preview.set(f"{self.fields['preprinted_origin'].get()} (pre-printed)" if preprinted else "")
        if self._last_preprinted is not None and preprinted != self._last_preprinted:
            # Print texts that no longer suit the film are unticked (and the default with them)
            self._last_preprinted = preprinted
            data = self.services.data.current
            film = copy.deepcopy(self._film())
            film.origin_preprinted = preprinted
            keep = [i for i in self.pt_ids if i in data.print_texts and origin_rule_ok(film, data.print_texts[i])]
            if keep != self.pt_ids:
                self.pt_ids = keep
                self.unticked_note.show([Msg(N_("Print texts that don't suit this setting were unticked."))])
            self._show_print_texts()
        self._show_buttons()

    def read_extra(self, record, problems):
        record.print_text_ids = list(self.pt_ids)
        if record.default_print_text_id not in record.print_text_ids:
            record.default_print_text_id = ""

    # files and photos

    def _next_kind(self):
        kinds = {f.kind for f in self._film().files} if self._film() is not None else set()
        for kind in ("front_photo", "back_photo", "artwork"):
            if kind not in kinds:
                return kind
        return "other"

    def _show_files(self):
        tree = self.files_tree
        tree.delete(*tree.get_children())
        film = self._film()
        for i, ref in enumerate(film.files if film is not None else []):
            tree.insert("", "end", iid=str(i), values=(tr(FILE_KIND_TEXTS.get(ref.kind, N_("Other file"))), ref.name,
                                                       f"{ref.size / 1e6:.1f} MB", ref.added_by, when(ref.added_at)))

    def _load_thumbs(self, pngs=None):
        """The film's last front and back photo, 160 px tall; previews come from the PC's cache or the
        share, on a worker."""
        services = self.services
        film = self._film()
        for kind, thumb in self.thumbs.items():
            refs = [f for f in film.files if f.kind == kind] if film is not None else []
            if pngs is not None and kind in pngs:
                self._show_thumb(kind, pngs[kind])
                continue
            if not refs:
                self._show_thumb(kind, None)
                continue
            ref = refs[-1]

            def done(png, error, kind=kind, sha=ref.sha256):
                current = [f for f in self._film().files if f.kind == kind] if self._film() is not None else []
                if not _alive(self) or not current or current[-1].sha256 != sha:
                    return
                self._show_thumb(kind, png if error is None else None)

            services.bg.run(lambda film_id=film.id, sha=ref.sha256: services.data.preview_bytes(film_id, sha), done)

    def _show_thumb(self, kind, png):
        thumb = self.thumbs[kind]
        photo = None
        if png:
            try:
                photo = photo_from_png(thumb, png, height=THUMB_PX)
            except Exception:
                photo = None
        thumb.unbind("<Button-1>")
        if photo is None:
            self._photos.pop(kind, None)
            thumb.configure(image="")
            self.texts.set(thumb, N_("No photo"))
            return
        self.texts.forget(thumb)
        self._photos[kind] = (photo, png)
        thumb.configure(image=photo, text="")
        film = self._film()
        caption = f"{film_name(film)}: " if film is not None else ""
        thumb.bind("<Button-1>", lambda e: PhotoWindow(self.window, self.services, png=png,
                                                       caption=caption + text(Msg(FILE_KIND_TEXTS[kind]))))

    def _add_file(self):
        film = self._film()
        if film is None:
            return
        kind = self.add_kind_box.key() or "other"
        path = filedialog.askopenfilename(parent=self, title=tr("Choose a file to add"),
                                          filetypes=[(tr("Pictures"), PICTURE_TYPES), (tr("PDF files"), "*.pdf"),
                                                     (tr("All files"), "*.*")])
        if not path:
            return
        services = self.services
        film_id = film.id

        def work():
            ref = services.data.save_file(film_id, kind, path)
            png = png_preview(path)
            if png:
                services.data.save_preview(film_id, ref.sha256, png)
            return ref, png

        def done(result, error):
            if not _alive(self) or self._film() is None or self._film().id != film_id:
                return
            if error is not None:
                self.files_note.configure(style="Bad.TLabel")
                self.files_note.show([_error_msg(error)])
                return
            ref, png = result
            working = self._film()
            if kind in ("front_photo", "back_photo"):
                # A film has one front and one back photo: the older reference goes (the original stays)
                working.files = [f for f in working.files if f.kind != kind]
            working.files.append(ref)
            self.page.mark_dirty()
            self._show_files()
            if kind in self.thumbs:
                self._show_thumb(kind, png)
            self.add_kind_box.set_key(self._next_kind())
            self.files_note.configure(style="Muted.TLabel")
            self.files_note.show([Msg(N_("The file is added. Save the film to keep it."))])

        services.bg.run(work, done)

    def _chosen_file(self):
        chosen = self.files_tree.selection()
        film = self._film()
        if not chosen or film is None:
            return None
        i = int(chosen[0])
        return film.files[i] if 0 <= i < len(film.files) else None

    def _open_file(self):
        ref = self._chosen_file()
        if ref is None:
            return
        services = self.services

        def opened(_result, error):
            if error is not None and _alive(self):
                self.files_note.configure(style="Bad.TLabel")
                self.files_note.show([_error_msg(error)])

        def found(path, error):
            if not _alive(self):
                return
            if error is not None or not path:
                self.files_note.configure(style="Bad.TLabel")
                self.files_note.show([Msg(N_("Stripes can't reach the server, so the file can't be opened now."))])
                return
            if sys.platform == "win32":
                services.bg.run(lambda: os.startfile(path), opened)
            else:
                services.bg.run(lambda: webbrowser.open("file://" + path), opened)

        services.bg.run(lambda: services.data.share_path("templates/" + ref.path), found)

    def _remove_file(self):
        ref = self._chosen_file()
        if ref is None:
            return
        film = self._film()
        film.files = [f for f in film.files if f is not ref]
        self.page.mark_dirty()
        self._show_files()
        self._load_thumbs()

    # adjustments and the test print

    def _show_adjustments(self):
        tree = self.adjust_tree
        tree.delete(*tree.get_children())
        data = self.services.data.current
        film = self._film()
        if data is None or film is None:
            return
        lines = {ln.id for ln in data.lines.values() if ln.active and ln.cartridge == film.cartridge}
        machines = [m for m in data.machines.values() if m.active and m.line_id in lines]
        for m in _sorted(machines):
            adj = data.adjustment(m.id, film.id)
            tree.insert("", "end", iid=m.id, values=(
                m.name, _choice_text(list(PLACEMENT_TEXTS.items()), m.placement), _adjustment_text(m, adj),
                adj.updated_by if adj is not None else "", when(adj.updated_at) if adj is not None else ""))
        self._show_buttons()

    def _show_buttons(self):
        page = self.page
        saved = page.expected_revision is not None
        _enable(self.adjust_button, saved and bool(self.adjust_tree.selection()))
        testable = saved and not page.dirty
        _enable(self.test_button, testable)
        # "Save the film first." says why the button is disabled, so it shows only then
        if testable:
            self.test_hint.pack_forget()
        elif not self.test_hint.winfo_manager():
            self.test_hint.pack(side="left", padx=(8, 0))

    def _edit_adjustment(self):
        chosen = self.adjust_tree.selection()
        film = self._film()
        if not chosen or film is None or self.page.expected_revision is None:
            return
        services = self.services
        window = services.nav.window(f"adjust:{chosen[0]}__{film.id}")
        if window is not None:
            window.show()
            return
        AdjustmentDialog(self.window, services, machine_id=chosen[0], film_id=film.id)

    def _test_print(self):
        film = self._film()
        if film is None or self.page.expected_revision is None or self.page.dirty:
            return
        self.services.nav.open_test_print(film_id=film.id)

    def refresh(self):
        self._show_adjustments()

    def refresh_texts(self):
        self._show_print_texts()
        self._show_files()
        self._show_adjustments()

    # checks

    def checks(self, record):
        return hx_message.validate_film(record, self.services.data.current)

    def warnings(self, record):
        out = []
        if record.status == "active":
            if record.delay_ms is None:
                out.append(Msg(N_("Delay machines can't send this film until it has a delay.")))
            if record.length_mm is None or record.position_mm is None:
                out.append(Msg(N_("Encoder machines can't send this film until it has a film length and a print "
                                  "position.")))
        return out


class ProductsPage(AdminPage):
    """Products and their films, in one tree (ui.md §11.6)."""
    key = "products"
    tab_text = N_("Products and films")
    new_text = N_("New product")
    columns = (("name", N_("Name"), 200), ("customer", N_("Customer"), 110), ("status", N_("Status"), 100),
               ("active", N_("Active"), 50), ("revision", N_("Settings revision"), 90))
    tree_show = "tree headings"

    def extra_buttons(self, top):
        self.new_product_button = self.new_button
        self.new_version_button = ttk.Button(top, command=self._new_version)
        self.texts.set(self.new_version_button, N_("Add new version"))
        self.new_version_button.pack(side="left", padx=(8, 0))

    def build_editors(self):
        self.product_editor = ProductEditor(self.form_holder, self)
        self.film_editor = FilmEditor(self.form_holder, self)
        self.product_editor.grid_remove()
        self.film_editor.grid_remove()
        self.editor = None

    def editor_for(self, record):
        return self.film_editor if isinstance(record, Film) else self.product_editor

    def collection(self, data):
        return {**data.products, **data.films}

    def find_any(self, record_id):
        data = self.services.data.current
        if data is None:
            return None
        return data.films.get(record_id) or data.products.get(record_id)

    def record_name(self, record):
        return film_name(record) if isinstance(record, Film) else record.name

    def fill_tree(self, data):
        show_all = self.show_inactive_var.get()
        films = {}
        for film in data.films.values():
            films.setdefault(film.product_id, []).append(film)
        for product in _sorted(data.products.values()):
            if not product.active and not show_all:
                continue
            self.tree.insert("", "end", iid=product.id, text=product.name,
                             values=(product.customer, "", _yes_no(product.active), ""), open=True)
            for film in sorted(films.get(product.id, []), key=_film_key):
                if film.status == "inactive" and not show_all:
                    continue
                self.tree.insert(product.id, "end", iid=film.id, text=film_name(film),
                                 values=("", _choice_text(STATUS_CHOICES, film.status), "", film.revision))

    def new_record(self, *, product_id="", **kw):
        if product_id:
            film = new_film(product_id)
            film.status = "development"
            return film
        return Product(id=new_id("pd"), name="")

    def _new_version(self):
        record = self.working
        if record is None:
            return
        product_id = record.product_id if isinstance(record, Film) else record.id
        if self.services.data.current is None or product_id not in self.services.data.current.products:
            return
        self.start_new(product_id=product_id)

    def update_save_state(self):
        super().update_save_state()
        if hasattr(self, "new_version_button"):
            _enable(self.new_version_button, self.working is not None and not self._saving and (
                isinstance(self.working, Film) or self.expected_revision is not None))
        if isinstance(self.editor, FilmEditor) and self.working is not None:
            self.editor._show_buttons()


# --------------------------------------------------------------------------
# Print texts
# --------------------------------------------------------------------------

class PrintTextEditor(_Form):
    """The print-text designer: header, items, the item editor, the longest-value preview and the
    films allowed (ui.md §11.8)."""

    # The items' number fields, with their labels, in the order their problems are listed
    ITEM_NUMBERS = (("max_len", N_("Most characters")), ("offset", N_("Offset")), ("gap", N_("Gap")))

    def __init__(self, master, page):
        super().__init__(master, page)
        texts = self.texts
        self.items: list[PrintTextItem] = []
        self.item_fields: dict[str, tk.Variable | tk.Widget] = {}
        self.film_vars: dict[str, tk.BooleanVar] = {}
        self.film_checks: dict[str, ttk.Checkbutton] = {}
        self._wanted: set[str] = set()
        self._item_loading = 0
        self._editing_item = None       # the item the item editor shows
        self._typed: dict[tuple[str, str], str] = {}    # (item id, field) -> a bad or empty number, as typed
        # The header a row each, as in the film editor, so each hint sits by its own field and wraps
        self.entry(0, "name", N_("Name"), width=26)
        self.entry(1, "code", N_("Code"), width=5,
                   hint=N_("Up to 3 capital letters or digits, used in the printer's message name."))
        self.label(2, N_("Status"))
        self.choice(self, "status", STATUS_CHOICES, width=16).grid(row=2, column=1, sticky="w", pady=2)
        self.revision_text = ttk.Label(self)
        self.revision_text.grid(row=3, column=1, sticky="w", pady=2)
        self.side_hint(3, N_("Goes up on every save. The send log records which one printed."))

        columns = ("n", "item", "before", "data", "after", "placement", "gap")
        self.items_tree = ttk.Treeview(self, columns=columns, show="headings", height=6, selectmode="browse")
        for column, english, width in (("n", "#", 30), ("item", N_("Item"), 120), ("before", N_("Text before"), 110),
                                       ("data", N_("Data"), 130), ("after", N_("Text after"), 90),
                                       ("placement", N_("Placement"), 80), ("gap", N_("Gap"), 50)):
            if english == "#":
                self.items_tree.heading(column, text="#")
            else:
                texts.heading(self.items_tree, column, english)
            self.items_tree.column(column, width=width)
        self.items_tree.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.items_tree.bind("<<TreeviewSelect>>", lambda e: self._show_item_editor(), add="+")
        self.fields["items"] = self.items_tree
        bar = ttk.Frame(self)
        bar.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.add_kind_box = ChoiceBox(bar, list(ITEM_TEXTS.items()), width=18)
        self.add_kind_box.set_key("fixed")
        self.add_kind_box.pack(side="left")
        self.add_item_button = ttk.Button(bar, command=self._add_item)
        texts.set(self.add_item_button, N_("Add item"))
        self.add_item_button.pack(side="left", padx=(6, 16))
        self.up_button = ttk.Button(bar, command=lambda: self._move(-1))
        texts.set(self.up_button, N_("Move up"))
        self.up_button.pack(side="left")
        self.down_button = ttk.Button(bar, command=lambda: self._move(1))
        texts.set(self.down_button, N_("Move down"))
        self.down_button.pack(side="left", padx=(6, 6))
        self.remove_item_button = ttk.Button(bar, command=self._remove_item)
        texts.set(self.remove_item_button, N_("Remove item"))
        self.remove_item_button.pack(side="left")
        self.add_problem = _Lines(self, style="Warn.TLabel", fit=True)
        self.add_problem.grid(row=6, column=0, columnspan=3, sticky="ew")
        self.add_problem.show([])
        self.item_editor = ttk.Frame(self, padding=(0, 6))
        self.item_editor.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.item_editor.columnconfigure(2, weight=1)       # the item's hints wrap in what is left

        caption = ttk.Label(self, style="Hx.Section.TLabel")
        texts.set(caption, N_("Preview with the longest values"))
        caption.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self.preview = BagText(self, size=12, family=mono_font_family(self), wrap_px=None)
        self.preview.grid(row=9, column=0, columnspan=3, sticky="w")
        self.lengths_label = ttk.Label(self, style="Muted.TLabel")
        self.lengths_label.grid(row=10, column=0, columnspan=3, sticky="w")
        self.side_hint(11, N_("Approximate: the test print shows exactly how it prints."), column=0, columnspan=3)

        caption = ttk.Label(self, style="Hx.Section.TLabel")
        texts.set(caption, N_("Films that may use this print text"))
        caption.grid(row=12, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self.films_frame = ttk.Frame(self)
        self.films_frame.grid(row=13, column=0, columnspan=3, sticky="w")
        self.films_note = _fit_wrap(ttk.Label(self, style="Muted.TLabel", wraplength=_WRAP, justify="left"))
        self.films_note.grid(row=14, column=0, columnspan=3, sticky="ew")

    # filling

    def fill_extra(self, record):
        self.items = copy.deepcopy(record.items)
        self._typed = {}
        self.texts.set(self.revision_text, N_("Revision {n}"), n=record.revision)
        data = self.services.data.current
        self._wanted = {f.id for f in data.films.values() if record.id in f.print_text_ids} if data else set()
        self.add_problem.show([])
        self._show_items()
        self._show_item_editor()
        self._show_films()

    def read_extra(self, record, problems):
        record.items = copy.deepcopy(self.items)
        problems += self.number_problems()

    def number_problems(self) -> list[Msg]:
        """A bad or empty number typed in an item's field is a problem, as in the rest of the form
        (ui.md §11.1 step 5.1): the item still has its old value, so saving would write that."""
        problems = []
        for n, item in enumerate(self.items):
            for name, english in self.ITEM_NUMBERS:
                typed = self._typed.get((item.id, name))
                if typed is None or (name == "gap" and n == 0):     # the first item has no gap field
                    continue
                if typed.strip() == "":
                    problems.append(Msg(N_("{field} is needed."), {"field": Msg(english)}))
                else:
                    problems.append(Msg(N_("{field}: type a whole number."), {"field": Msg(english)}))
        return problems

    def before_save(self):
        # The first item with a bad number is shown in the item editor, so the admin sees which
        for n, item in enumerate(self.items):
            if any((item.id, name) in self._typed and not (name == "gap" and n == 0)
                   for name, _english in self.ITEM_NUMBERS):
                if self._selected() is not item:
                    self.items_tree.selection_set(item.id)
                    self._show_item_editor()
                return

    def wanted(self) -> set[str]:
        return set(self._wanted)

    def on_change(self):
        self._show_preview()
        self._show_live_problems()

    def _working_pt(self):
        record = copy.deepcopy(self.page.working)
        problems = []
        if record is None:
            return None
        self.read(record, problems)
        return record

    # items

    def _item_data(self, item):
        kind = item.kind
        if kind == "fixed":
            return f'"{item.text}"'
        if kind == "packed_date":
            return item.date_format
        if kind == "best_by_date":
            unit = _choice_text(UNIT_CHOICES, item.offset_unit)
            return f"{item.date_format} +{item.offset} {unit}"
        if kind == "lot":
            return tr("[lot]")
        if kind == "origin":
            return tr("[countries]")
        if kind == "free_text":
            return "[" + item.label + "]"
        if kind == "machine_code":
            return tr("[machine code]")
        if kind == "line_code":
            return tr("[line code]")
        return ""

    def _row_values(self, n, item):
        quoted = item.kind not in ("fixed", "origin")
        placement = "–" if n == 1 else (tr("Right") if item.placement == "right" else tr("Below"))
        return (n, tr(ITEM_TEXTS.get(item.kind, item.kind)), f'"{item.before}"' if quoted else "",
                self._item_data(item), f'"{item.after}"' if quoted else "", placement, item.gap)

    def _show_items(self, select=None):
        tree = self.items_tree
        chosen = select if select is not None else (tree.selection()[0] if tree.selection() else "")
        tree.delete(*tree.get_children())
        for n, item in enumerate(self.items, start=1):
            tree.insert("", "end", iid=item.id, values=self._row_values(n, item))
        if chosen and tree.exists(chosen):
            tree.selection_set(chosen)

    def _show_item_row(self, item):
        """One item's row again, in place: the selection and the item editor stay as they are."""
        if item in self.items and self.items_tree.exists(item.id):
            self.items_tree.item(item.id, values=self._row_values(self.items.index(item) + 1, item))

    def _selected(self):
        chosen = self.items_tree.selection()
        if not chosen:
            return None
        for item in self.items:
            if item.id == chosen[0]:
                return item
        return None

    def _next_item_id(self):
        used = {it.id for it in self.items}
        n = 1
        while f"i{n}" in used:
            n += 1
        return f"i{n}"

    def _add_item(self):
        kind = self.add_kind_box.key()
        if not kind:
            return
        if kind == "origin" and any(it.kind == "origin" for it in self.items):
            self.add_problem.show([Msg(N_("A print text has at most one origin item."))])
            return
        self.add_problem.show([])
        data = self.services.data.current
        item = PrintTextItem(id=self._next_item_id(), kind=kind, placement="right")
        if kind in ("packed_date", "best_by_date"):
            item.date_format = "MM/DD/YY"
        if kind == "best_by_date":
            item.offset = 1
        if kind == "free_text":
            item.max_len = min(10, data.company.free_text_max) if data is not None else 10
            item.label = ""
        self.items.append(item)
        self._show_items(select=item.id)
        self._show_item_editor()
        self._items_changed()

    def _move(self, step):
        item = self._selected()
        if item is None:
            return
        i = self.items.index(item)
        j = i + step
        if not 0 <= j < len(self.items):
            return
        self.items[i], self.items[j] = self.items[j], self.items[i]
        self._show_items(select=item.id)
        self._show_item_editor(force=True)      # the first item has no placement or gap
        self._items_changed()

    def _remove_item(self):
        item = self._selected()
        if item is None:
            return
        self.items.remove(item)
        # Its typed numbers go too: a new item may get its id
        self._typed = {key: typed for key, typed in self._typed.items() if key[0] != item.id}
        self._show_items(select="")
        self._show_item_editor()
        self._items_changed()

    def _items_changed(self):
        self.changed()
        self._show_films()

    def _show_item_editor(self, force=False):
        """The selected item's fields, by kind (ui.md §11.8.2 item 4). The item already shown isn't
        built again (unless forced), so the field being typed in stays."""
        texts = self.texts
        item = self._selected()
        if item is not None and item is self._editing_item and not force:
            return
        _clear(self.item_editor, texts)
        self.item_fields = {}
        self._editing_item = item
        if item is None:
            return
        frame = self.item_editor
        row = 0
        self._item_loading += 1
        try:
            def text_field(name, english, width=20):
                nonlocal row
                caption = ttk.Label(frame)
                texts.set(caption, english)
                caption.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=1)
                var = tk.StringVar(value=getattr(item, name))
                var.trace_add("write", lambda *a: self._item_set(item, name, var.get()))
                ttk.Entry(frame, width=width, textvariable=var).grid(row=row, column=1, sticky="w", pady=1)
                self.item_fields[name] = var
                row += 1

            def number_field(name, english, lo, hi):
                nonlocal row
                caption = ttk.Label(frame)
                texts.set(caption, english)
                caption.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=1)
                var = tk.StringVar(value=self._typed.get((item.id, name), str(getattr(item, name))))
                var.trace_add("write", lambda *a: self._item_number(item, name, var.get()))
                spin = ttk.Spinbox(frame, from_=lo, to=hi, increment=1, width=6, textvariable=var)
                spin.grid(row=row, column=1, sticky="w", pady=1)
                self.item_fields[name] = var
                row += 1
                return spin

            kind = item.kind
            if kind == "fixed":
                text_field("text", N_("Text"), width=40)
            elif kind == "origin":
                note = _fit_wrap(ttk.Label(frame, style="Muted.TLabel", wraplength=_WRAP, justify="left"))
                texts.set(note, N_("Always prints \"Product of\" and the countries chosen at Send. It has no text "
                                   "before or after."))
                note.grid(row=row, column=0, columnspan=3, sticky="ew")
                row += 1
            else:
                if kind == "free_text":
                    text_field("label", N_("Question for the sender"), width=30)
                    data = self.services.data.current
                    top = data.company.free_text_max if data is not None else 100
                    number_field("max_len", N_("Most characters"), 1, top)
                    var = tk.BooleanVar(value=item.required)
                    var.trace_add("write", lambda *a: self._item_set(item, "required", bool(var.get())))
                    check = ttk.Checkbutton(frame, variable=var)
                    texts.set(check, N_("Required"))
                    check.grid(row=row, column=1, sticky="w")
                    self.item_fields["required"] = var
                    row += 1
                text_field("before", N_("Text before"))
                if kind in ("packed_date", "best_by_date"):
                    caption = ttk.Label(frame)
                    texts.set(caption, N_("Date format"))
                    caption.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=1)
                    formats = ttk.Combobox(frame, state="readonly", values=list(hx_message.DATE_FORMATS), width=14)
                    formats.set(item.date_format)
                    formats.grid(row=row, column=1, sticky="w", pady=1)
                    formats.bind("<<ComboboxSelected>>", lambda e: self._item_set(item, "date_format", formats.get()),
                                 add="+")
                    self.item_fields["date_format"] = formats
                    example = ttk.Frame(frame)
                    example.grid(row=row, column=2, sticky="w", padx=(8, 0))
                    caption = ttk.Label(example, style="Muted.TLabel")
                    texts.set(caption, N_("prints"))
                    caption.pack(side="left")
                    self.date_example = BagText(example, size=10)
                    self.date_example.pack(side="left", padx=(4, 0))
                    row += 1
                    if kind == "best_by_date":
                        caption = ttk.Label(frame)
                        texts.set(caption, N_("Offset"))
                        caption.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=1)
                        offset = ttk.Frame(frame)
                        offset.grid(row=row, column=1, columnspan=2, sticky="w")
                        var = tk.StringVar(value=self._typed.get((item.id, "offset"), str(item.offset)))
                        var.trace_add("write", lambda *a: self._item_number(item, "offset", var.get()))
                        top = hx_message.OFFSET_MAX.get(item.offset_unit, 999)
                        self.offset_spin = ttk.Spinbox(offset, from_=1, to=top, increment=1, width=6,
                                                       textvariable=var)
                        self.offset_spin.pack(side="left")
                        unit = ChoiceBox(offset, UNIT_CHOICES, width=10)
                        unit.set_key(item.offset_unit)
                        unit.pack(side="left", padx=(6, 0))
                        unit.bind("<<ComboboxSelected>>", lambda e: self._unit_set(item, unit.key()), add="+")
                        self.item_fields["offset"] = var
                        self.item_fields["offset_unit"] = unit
                        row += 1
                text_field("after", N_("Text after"))
            if self.items and item is not self.items[0]:
                placement = tk.StringVar(value=item.placement)
                placement.trace_add("write", lambda *a: self._item_set(item, "placement", placement.get()))
                radios = ttk.Frame(frame)
                radios.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
                for value, english in (("right", N_("Right of the previous item")),
                                       ("below", N_("Below the previous item"))):
                    radio = ttk.Radiobutton(radios, variable=placement, value=value)
                    texts.set(radio, english)
                    radio.pack(side="left", padx=(0, 12))
                self.item_fields["placement"] = placement
                row += 1
                number_field("gap", N_("Gap"), 0, hx_message.GAP_MAX)
                self.hint(frame, N_("Extra space before the item, in printer canvas units."), fit=True).grid(
                    row=row - 1, column=2, sticky="ew", padx=(8, 0))
        finally:
            self._item_loading -= 1
        self._show_date_example(item)

    def _item_set(self, item, name, value):
        if self._item_loading:
            return
        setattr(item, name, value)
        self._show_item_row(item)
        self._show_date_example(item)
        if name == "placement":
            self._show_preview()
        self.changed()

    def _item_number(self, item, name, value):
        """A whole number typed in an item's field. A bad or empty one is kept as typed (shown again
        when the item is) and is a problem until it is fixed, never silently ignored."""
        if self._item_loading:
            return
        try:
            number = parse_number(value, whole=True)
        except ValueError:
            number = None
        if number is None:
            self._typed[(item.id, name)] = value
            self.changed()
            return
        self._typed.pop((item.id, name), None)
        self._item_set(item, name, number)

    def _unit_set(self, item, unit):
        self._item_set(item, "offset_unit", unit)
        if hasattr(self, "offset_spin") and _alive(self.offset_spin):
            self.offset_spin.configure(to=hx_message.OFFSET_MAX.get(unit, 999))

    def _show_date_example(self, item):
        example = getattr(self, "date_example", None)
        if example is None or not _alive(example) or item.kind not in ("packed_date", "best_by_date"):
            return
        try:
            add = {f"add_{item.offset_unit}": item.offset} if item.kind == "best_by_date" else {}
            shown = hx_message.render_date(hx_message.DATE_FORMATS[item.date_format], self._today(), **add)
        except (KeyError, ValueError, TypeError, OverflowError):
            shown = "?"
        example.set(shown)

    def _today(self):
        return self.services.clock.now().date()

    # preview and films

    def _show_preview(self):
        pt = self._working_pt()
        data = self.services.data.current
        if pt is None or data is None:
            return
        lines = preview_lines(pt, longest_values(pt, data.company, self._today()))
        self.preview.set("\n".join(lines))
        self.texts.set(self.lengths_label, N_("Longest line: {n} characters."),
                       n=max((len(line) for line in lines), default=0))

    def _show_films(self):
        texts = self.texts
        _clear(self.films_frame, texts)
        self.film_vars = {}
        self.film_checks = {}
        pt = self._working_pt()
        data = self.services.data.current
        if pt is None or data is None:
            return
        n = sum(1 for it in pt.items if it.kind == "origin")
        choices = film_choices(pt, data) if n <= 1 else []
        for row, film in enumerate(choices):
            product = data.products.get(film.product_id)
            var = tk.BooleanVar(value=film.id in self._wanted)
            check = ttk.Checkbutton(self.films_frame, variable=var,
                                    text=f"{product.name if product else ''} · {film_name(film)}",
                                    command=lambda fid=film.id: self._film_toggled(fid))
            check.grid(row=row, column=0, sticky="w")
            self.film_checks[film.id] = check
            ttk.Label(self.films_frame, text=_choice_text(STATUS_CHOICES, film.status),
                      style="Muted.TLabel").grid(row=row, column=1, sticky="w", padx=(10, 10))
            if film.default_print_text_id == pt.id:
                default = ttk.Label(self.films_frame, style="Muted.TLabel")
                texts.set(default, N_("Default for this film"))
                default.grid(row=row, column=2, sticky="w")
            self.film_vars[film.id] = var
        if n == 1:
            texts.set(self.films_note, N_("Only films whose origin isn't pre-printed are listed, because this print "
                                          "text has an origin item."))
        elif n == 0:
            texts.set(self.films_note, N_("Only films with a pre-printed origin are listed, because this print text "
                                          "has no origin item."))
        else:
            texts.forget(self.films_note)
            self.films_note.configure(text="")

    def _film_toggled(self, film_id):
        if self.film_vars[film_id].get():
            self._wanted.add(film_id)
        else:
            self._wanted.discard(film_id)
        self.changed()

    def membership(self, record):
        """(films to save, problems) for the films' side of the change."""
        data = self.services.data.current
        films, problems = membership_saves(record, self.wanted(), data)
        # A film with unsaved edits in the film editor would be overwritten
        products = self.window.pages.get("products")
        if products is not None and products.dirty and isinstance(products.working, Film):
            for film in films:
                if film.id == products.working.id:
                    problems.append(Msg(N_("Save or revert your changes to {film} first."),
                                        {"film": film_name(products.working)}))
        return films, problems

    def _show_live_problems(self):
        pt = self._working_pt()
        data = self.services.data.current
        if pt is None or data is None:
            return
        problems = self.number_problems()
        if not problems:
            problems = hx_message.validate_print_text(pt, data.company)
        if not problems:
            problems = self.membership(pt)[1]
        self.page.problems_label.show(problems)

    def checks(self, record):
        data = self.services.data.current
        problems = hx_message.validate_print_text(record, data.company)
        if problems:
            return problems
        return self.membership(record)[1]

    def refresh(self):
        # Another PC (or this one) may have changed which films allow it: follow, unless being edited
        data = self.services.data.current
        record = self.page.working
        if data is not None and record is not None and not self.page.dirty:
            self._wanted = {f.id for f in data.films.values() if record.id in f.print_text_ids}
        self._show_films()

    def refresh_texts(self):
        self._show_items()
        self._show_films()


class PrintTextsPage(AdminPage):
    key = "print_texts"
    tab_text = N_("Print texts")
    new_text = N_("New print text")
    columns = (("name", N_("Name"), 170), ("code", N_("Code"), 50), ("status", N_("Status"), 100),
               ("revision", N_("Revision"), 70))

    def build_editors(self):
        self.set_editor(PrintTextEditor(self.form_holder, self))

    def collection(self, data):
        return data.print_texts

    def values(self, pt):
        return (pt.name, pt.code, _choice_text(STATUS_CHOICES, pt.status), pt.revision)

    def new_record(self, **kw):
        return PrintText(id=new_id("pt"), name="", code="")

    def save_record(self, record):
        """The print text first, then each film whose list changes, each with its own revision; a film
        that fails doesn't stop the others (ui.md §11.8.5)."""
        services = self.services
        expected = self.expected_revision
        wanted = self.editor.wanted()
        films, problems = self.editor.membership(record)
        if problems:
            self.problems_label.show(problems)
            return
        self._saving = True
        self.update_save_state()
        self.status_label.configure(style="Muted.TLabel")
        self.texts.set(self.status_label, N_("Saving..."))

        def work():
            saved = services.data.save(record, expected_revision=expected)
            failed = []
            for film in films:
                try:
                    services.data.save(film, expected_revision=film.revision)
                except (HxError, ValueError) as e:
                    failed.append((film, e))
            return saved, failed

        def done(result, error):
            if not _alive(self):
                return
            self._saving = False
            if not self.editing(record):
                self.answer_elsewhere(record, error)
                self.window.save_answered(self, error is None)
                return
            if error is not None:
                self.save_failed(error)
                self.update_save_state()
                self.window.save_answered(self, False)
                return
            saved, failed = result
            self.saved(saved)
            # Until the next DataUpdate the data still has the old lists: show what was saved
            for film, _e in failed:
                if film.id in wanted:
                    wanted.discard(film.id)
                else:
                    wanted.add(film.id)
            self.editor._wanted = wanted
            self.editor._show_films()
            if failed:
                self.problems_label.show(
                    [Msg(N_("{film}: {problem}"), {"film": film_name(f), "problem": e.msg if isinstance(e, HxError)
                                                   else Msg(N_("Stripes couldn't save this: {error}"),
                                                            {"error": str(e)})}) for f, e in failed],
                    header=Msg(N_("The print text was saved, but these films weren't:")))
            self.update_save_state()
            self.window.save_answered(self, not failed)

        services.bg.run(work, done)


# --------------------------------------------------------------------------
# Machine + film adjustments
# --------------------------------------------------------------------------

class AdjustmentsPage(AdminPage):
    """The machine + film adjustments, filtered; each opens in the adjustment dialog (ui.md §11.9)."""
    key = "adjustments"
    tab_text = N_("Machine + film")
    new_text = N_("New adjustment...")
    columns = (("machine", N_("Machine"), 150), ("film", N_("Film"), 110), ("placement", N_("Placement"), 80),
               ("adjustment", N_("Adjustment"), 90), ("by", N_("Set by"), 120), ("at", N_("Set at"), 120))

    def extra_buttons(self, top):
        self.machine_filter = RecordBox(top, width=22)
        self.machine_filter.pack(side="left", padx=(16, 4))
        self.machine_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_list(), add="+")
        self.film_filter = RecordBox(top, width=18)
        self.film_filter.pack(side="left", padx=(4, 0))
        self.film_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_list(), add="+")

    def _build(self):
        super()._build()
        self.editor_scroller.grid_remove()
        self.show_inactive_check.pack_forget()
        self.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self._open_chosen(), add="+")

    def collection(self, data):
        return data.adjustments

    def records(self, data):
        machine_id = self.machine_filter.record_id()
        film_id = self.film_filter.record_id()
        found = [a for a in data.adjustments.values()
                 if (not machine_id or a.machine_id == machine_id) and (not film_id or a.film_id == film_id)]

        def key(a):
            m, f = data.machines.get(a.machine_id), data.films.get(a.film_id)
            return (hx_state.natural_key(m.name if m else ""),) + (_film_key(f) if f else ([], [], "")) + (a.id,)
        return sorted(found, key=key)

    def values(self, adj):
        data = self.services.data.current
        m, f = data.machines.get(adj.machine_id), data.films.get(adj.film_id)
        return (m.name if m else adj.machine_id, film_name(f) if f else adj.film_id,
                _choice_text(list(PLACEMENT_TEXTS.items()), m.placement) if m else "", _adjustment_text(m, adj),
                adj.updated_by, when(adj.updated_at))

    def inactive(self, record):
        return False

    def refresh_list(self):
        data = self.services.data.current
        if data is not None and hasattr(self, "machine_filter"):
            self.machine_filter.set_records([(m.id, m.name) for m in _sorted(data.machines.values())],
                                            none_text=N_("All machines"))
            self.film_filter.set_records([(f.id, film_name(f)) for f in sorted(data.films.values(), key=_film_key)],
                                         none_text=N_("All films"))
            if self.machine_filter.current() < 0:
                self.machine_filter.current(0)
            if self.film_filter.current() < 0:
                self.film_filter.current(0)
        super().refresh_list()

    def _on_select(self):
        pass                    # a double-click opens the dialog

    def _open_chosen(self):
        chosen = self.tree.selection()
        data = self.services.data.current
        if not chosen or data is None:
            return
        adj = data.adjustments.get(chosen[0])
        if adj is not None:
            self.open_dialog(adj.machine_id, adj.film_id)

    def start_new(self, **kw):
        self.open_dialog(self.machine_filter.record_id(), self.film_filter.record_id())

    def open_dialog(self, machine_id, film_id):
        services = self.services
        window = services.nav.window(f"adjust:{machine_id}__{film_id}")
        if window is not None:
            window.show()
            return window
        return AdjustmentDialog(self.window, services, machine_id=machine_id, film_id=film_id)

    def select_record(self, record_id):
        data = self.services.data.current
        adj = data.adjustments.get(record_id) if data is not None else None
        if adj is not None:
            self.open_dialog(adj.machine_id, adj.film_id)


class AdjustmentDialog(HxWindow):
    """One machine + film adjustment: a delay in ms on delay machines, a position in mm on encoder
    machines; ignored during a test print (ui.md §11.9)."""

    def __init__(self, master, services, *, machine_id: str, film_id: str):
        self.key = f"adjust:{machine_id}__{film_id}"
        super().__init__(master, services, title=N_("Machine + film adjustment"))
        self.geometry("480x340")
        self.resizable(True, False)
        texts = self.texts
        data = services.data.current
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        caption = ttk.Label(body)
        texts.set(caption, N_("Machine"))
        caption.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.machine_box = RecordBox(body, width=26)
        self.machine_box.set_records([(m.id, m.name) for m in _sorted(m for m in data.machines.values() if m.active)])
        self.machine_box.set_record_id(machine_id)
        self.machine_box.grid(row=0, column=1, sticky="w", pady=2)
        self.machine_box.bind("<<ComboboxSelected>>", lambda e: self._chosen(), add="+")
        self.mode_label = ttk.Label(body, style="Muted.TLabel")
        self.mode_label.grid(row=0, column=2, sticky="w", padx=(8, 0))
        caption = ttk.Label(body)
        texts.set(caption, N_("Film"))
        caption.grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.film_box = RecordBox(body, width=26)
        self.film_box.set_records([(f.id, film_name(f)) for f in sorted(data.films.values(), key=_film_key)])
        self.film_box.set_record_id(film_id)
        self.film_box.grid(row=1, column=1, sticky="w", pady=2)
        self.film_box.bind("<<ComboboxSelected>>", lambda e: self._chosen(), add="+")
        self.value_caption = ttk.Label(body)
        self.value_caption.grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self.value_var = tk.StringVar()
        self.value_entry = ttk.Entry(body, width=10, textvariable=self.value_var)
        self.value_entry.grid(row=2, column=1, sticky="w", pady=(6, 0))
        self.value_var.trace_add("write", lambda *a: self._show_working())
        hint = ttk.Label(body, style="Muted.TLabel")
        texts.set(hint, N_("Can be negative. 0 means none."))
        hint.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        self.working_label = ttk.Label(body, wraplength=440)
        self.working_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ignored = ttk.Label(body, style="Muted.TLabel")
        texts.set(ignored, N_("Ignored during a test print."))
        ignored.grid(row=4, column=0, columnspan=3, sticky="w")
        self.set_by_label = ttk.Label(body, style="Muted.TLabel")
        self.set_by_label.grid(row=5, column=0, columnspan=3, sticky="w")
        self.problems_label = _Lines(body, style="Bad.TLabel", wrap=440)
        self.problems_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.problems_label.show([])
        # Why Save is off: the admin window's own texts (ui.md §11.1 items 9 and 10)
        self.state_label = _Lines(body, style="Warn.TLabel", wrap=440)
        self.state_label.grid(row=7, column=0, columnspan=3, sticky="w")
        self.state_label.show([])
        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.save_button = ttk.Button(buttons, style="Hx.Main.TButton", command=self._save)
        texts.set(self.save_button, N_("Save"))
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(buttons, command=self.destroy)
        texts.set(self.cancel_button, N_("Cancel"))
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self.destroy())
        self.existing = None
        self._saving = False
        self._chosen()
        # Save follows the rule of every admin page's Save (ui.md §11.1 items 9 and 10)
        self.subscribe(DataUpdate, lambda e: self._update_save_state())
        self.subscribe(ShareHealth, lambda e: self._update_save_state())
        self._update_save_state()

    def can_save(self) -> bool:
        services = self.services
        data = services.data.current
        return (not self._saving and data is not None and services.store.available and data.source != "local"
                and roles(services).admin)

    def _update_save_state(self):
        services = self.services
        data = services.data.current
        _enable(self.save_button, self.can_save())
        why = []
        if not services.store.available or data is None or data.source == "local":
            why.append(Msg(N_("The server can't be reached. Changes can't be saved now.")))
        if not roles(services).admin:
            why.append(Msg(N_("Only Stripes Administrators can change this.")))
        self.state_label.show(why)

    def refresh_texts(self) -> None:
        _render_lines(self)

    def _machine(self):
        data = self.services.data.current
        return data.machines.get(self.machine_box.record_id()) if data is not None else None

    def _film(self):
        data = self.services.data.current
        return data.films.get(self.film_box.record_id()) if data is not None else None

    def _chosen(self):
        data = self.services.data.current
        machine, film = self._machine(), self._film()
        self.existing = data.adjustment(machine.id, film.id) if machine is not None and film is not None else None
        encoder = machine is not None and machine.placement == "encoder"
        if machine is None:
            self.texts.forget(self.mode_label)
            self.mode_label.configure(text="")
        else:
            self.texts.set(self.mode_label, N_("Encoder machine") if encoder else N_("Delay machine"))
        self.texts.set(self.value_caption, N_("Position adjustment (mm)") if encoder else N_("Delay adjustment (ms)"))
        value = 0.0
        if self.existing is not None:
            value = self.existing.position_mm if encoder else self.existing.delay_ms
            self.texts.set(self.set_by_label, N_("Set by {user} at {time}"), user=self.existing.updated_by,
                           time=when(self.existing.updated_at))
        else:
            self.texts.forget(self.set_by_label)
            self.set_by_label.configure(text="")
        self.value_var.set(_num(float(value)) if value else "0")
        self._show_working()

    def adjustment(self):
        """The adjustment with the typed value; the other value of an existing record is kept."""
        machine, film = self._machine(), self._film()
        value = parse_number(self.value_var.get())
        value = 0.0 if value is None else float(value)
        base = self.existing
        position = base.position_mm if base is not None else 0.0
        delay = base.delay_ms if base is not None else 0.0
        if machine is not None and machine.placement == "encoder":
            position = value
        else:
            delay = value
        m = machine.id if machine is not None else ""
        f = film.id if film is not None else ""
        return MachineFilmAdjustment(id=adjustment_id(m, f), machine_id=m, film_id=f, position_mm=position,
                                     delay_ms=delay)

    def _show_working(self):
        machine, film = self._machine(), self._film()
        shown = ""
        if machine is not None and film is not None:
            try:
                shown = hx_message.explain_position(machine, film, self.adjustment()).text
            except (PlanError, ValueError):
                shown = ""
        self.working_label.configure(text=shown)        # English working: data

    def _save(self):
        services = self.services
        if not self.can_save():
            return
        try:
            adj = self.adjustment()
        except ValueError:
            self.problems_label.show([Msg(N_("{field}: type a number."), {"field": Msg(self.texts_caption())})])
            return
        problems = adjustment_problems(adj, services.data.current)
        self.problems_label.show(problems)
        if problems:
            return
        expected = self.existing.revision if self.existing is not None else None
        self._saving = True
        self._update_save_state()

        def done(saved, error):
            if not _alive(self):
                return
            self._saving = False
            self._update_save_state()
            if error is not None:
                self.problems_label.show([_error_msg(error)])
                return
            self.destroy()

        services.bg.run(lambda: services.data.save(adj, expected_revision=expected), done)

    def texts_caption(self):
        machine = self._machine()
        return N_("Position adjustment (mm)") if machine is not None and machine.placement == "encoder" \
            else N_("Delay adjustment (ms)")


# --------------------------------------------------------------------------
# The admin window
# --------------------------------------------------------------------------

class AdminWindow(HxWindow):
    """MapleJet admin: one page per kind of record (ui.md §11.1)."""
    key = "admin"

    def __init__(self, master, services, *, page: str = "", record_id: str = ""):
        super().__init__(master, services, title=N_("MapleJet admin"))
        self.geometry("1180x820")
        self.minsize(900, 600)
        texts = self.texts
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        heading = ttk.Label(body, style="Hx.Head.TLabel")
        texts.set(heading, N_("MapleJet admin"))
        heading.grid(row=0, column=0, sticky="w")
        self.problems_frame = _Lines(body, style="Bad.TLabel", wrap=1100)
        self.problems_frame.grid(row=1, column=0, sticky="w")
        self.share_banner = ttk.Label(body, style="Warn.TLabel")
        texts.set(self.share_banner, N_("The server can't be reached. Changes can't be saved now."))
        self.share_banner.grid(row=2, column=0, sticky="w")
        self.role_label = ttk.Label(body, style="Bad.TLabel")
        texts.set(self.role_label, N_("Only Stripes Administrators can change this."))
        self.role_label.grid(row=3, column=0, sticky="w")
        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        self._close_after_save = False      # closing was asked for while a save was in flight
        self.pages: dict[str, AdminPage] = {}
        for cls in (CompanyPage, LinesPage, MachinesPage, PrintersPage, ProductsPage, PrintTextsPage, AdjustmentsPage):
            page_ = cls(self.notebook, self)
            self.notebook.add(page_)
            texts.tab(self.notebook, page_, cls.tab_text)
            self.pages[cls.key] = page_
        self._shown_page = self.pages["company"]
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed(), add="+")
        self.subscribe(DataUpdate, lambda e: self._data_changed())
        self.subscribe(ShareHealth, lambda e: self._show_state())
        self.subscribe(BoardUpdate, lambda e: [p.board_changed() for p in self.pages.values()])
        self._show_state()
        self.select(page, record_id)

    def current_page(self) -> AdminPage:
        return self.nametowidget(self.notebook.select())

    def _tab_changed(self):
        # A tab selected here (select(), or going back after "Keep editing") is already _shown_page
        new = self.current_page()
        old = self._shown_page
        if new is old:
            return
        if not old.leave_page():
            self.notebook.select(old)
            return
        self._shown_page = new

    def select(self, page: str = "", record_id: str = "") -> None:
        """Show a page and, when given, a record on it (for products, a film id selects the film)."""
        if page and page in self.pages:
            target = self.pages[page]
            if target is not self._shown_page:
                if not self._shown_page.leave_page():
                    return
                self._shown_page = target
                self.notebook.select(target)
        if record_id:
            self._shown_page.select_record(record_id)

    def _data_changed(self):
        for page in self.pages.values():
            page.data_changed()
        self._show_state()

    def _show_state(self):
        services = self.services
        data = services.data.current
        self.problems_frame.show(list(services.data.problems))
        down = not services.store.available or (data is not None and data.source == "local")
        self.share_banner.grid() if down else self.share_banner.grid_remove()
        admin = roles(services).admin
        self.role_label.grid_remove() if admin else self.role_label.grid()
        for page in self.pages.values():
            page.update_save_state()

    def refresh_texts(self) -> None:
        for page in self.pages.values():
            page.retranslate()
        _render_lines(self)

    def close(self) -> None:
        """Each dirty page asks in turn, as when leaving it for another tab (ui.md §11.1 step 4): a
        discarded edit is dropped at once, so a later "Keep editing" doesn't leave it on its form for
        the next Save. While a save is in flight nothing is asked: the window closes once it's saved,
        and stays open showing the problem when it fails."""
        if any(page._saving for page in self.pages.values()):
            self._close_after_save = True
            return
        self._close_after_save = False
        for page in self.pages.values():
            if not page.leave_page():
                self._shown_page = page
                self.notebook.select(page)
                return
        self.destroy()

    def save_answered(self, page: "AdminPage", ok: bool) -> None:
        """A page's save has its answer: a close asked for meanwhile goes on when it was saved."""
        if not self._close_after_save or any(p._saving for p in self.pages.values()):
            return
        if ok:
            self.close()
            return
        self._close_after_save = False
        if page is not self._shown_page:
            self._shown_page = page
            self.notebook.select(page)


# --------------------------------------------------------------------------
# Test prints and calibration runs
# --------------------------------------------------------------------------

class TestRunPanel(ttk.Frame):
    """The progress of a test print or calibration run (ui.md §12.4)."""

    def __init__(self, master, window: HxWindow):
        super().__init__(master, padding=(0, 6))
        self.window = window
        self.plan = None
        self.cancel_token = None
        texts = window.texts
        self.columnconfigure(1, weight=1)
        self.step_label = ttk.Label(self, style="Hx.Section.TLabel")
        self.step_label.grid(row=0, column=0, sticky="w")
        self.label_text = BagText(self, size=14, bold=True)
        self.label_text.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.status_label = ttk.Label(self, wraplength=760, justify="left")
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="w")
        self.picture = PictureView(self, width=768)
        self.picture.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 4))
        self.stop_button = ttk.Button(self, command=self.stop)
        texts.set(self.stop_button, N_("Stop test"))
        self.stop_button.grid(row=3, column=1, sticky="e")

    def start(self, plan, cancel_token) -> None:
        self.plan = plan
        self.cancel_token = cancel_token
        texts = self.window.texts
        texts.set(self.step_label, N_("Step {step} of {of}"), step=1, of=len(plan.steps))
        self.label_text.set(plan.steps[0].label if plan.steps else "")
        texts.set(self.status_label, N_("Building and checking the next message..."))
        self.status_label.configure(style="TLabel")
        self.picture.set_bmp(None)
        self.stop_button.grid()
        _enable(self.stop_button, True)

    def show_event(self, event) -> None:
        texts = self.window.texts
        plan = self.plan
        if event.step_index and plan is not None:
            texts.set(self.step_label, N_("Step {step} of {of}"), step=event.step_index, of=event.of)
            if 1 <= event.step_index <= len(plan.steps):
                self.label_text.set(plan.steps[event.step_index - 1].label)
        style = "TLabel"
        status = event.status
        if status == "building":
            texts.set(self.status_label, N_("Building and checking the next message..."))
        elif status == "printing":
            if event.bags is None:
                texts.set(self.status_label, N_("Printing..."))
            else:
                texts.set(self.status_label, N_("Printing: {bags} of {per} bags"), bags=event.bags,
                          per=plan.bags_per_step if plan is not None else "?")
        elif status == "ok":
            texts.set(self.status_label, N_("Step done."))
        elif status == "failed":
            texts.msg(self.status_label, event.msg)
            style = "Bad.TLabel"
        elif status == "stopping":
            texts.set(self.status_label, N_("Stopping: printing off, then emptying slots 9 and 10."))
        elif status == "cleared":
            texts.set(self.status_label, N_("Printing is off and slots 9 and 10 are empty."))
        self.status_label.configure(style=style)
        if event.design is not None and event.design.preview_bmp:
            self.picture.set_bmp(event.design.preview_bmp)

    def stop(self) -> None:
        """Stop test: after the current request, then the end procedure always runs."""
        if self.cancel_token is None:
            return
        self.cancel_token.cancel()
        _enable(self.stop_button, False)
        self.window.texts.set(self.status_label, N_("Stopping: printing off, then emptying slots 9 and 10."))

    def finished(self) -> None:
        self.stop_button.grid_remove()


class _TestRunner:
    """What the test print and calibration windows share: running a TestPlan on its hx-test thread,
    its events, and the red "Stop it at the printer now" page."""

    def _init_runner(self):
        self.run = None
        self.run_plan = None
        self.cancel_token = None
        self.running_now = False
        self.red_page = None
        self.red_problem = None
        self.end_try_button = None
        self.subscribe(TestEvent, self._on_test_event)
        self.subscribe(TestFinished, self._on_test_finished)
        self.subscribe(WorkFailed, self._on_test_failed)

    def _machine_name(self, machine_id):
        data = self.services.data.current
        machine = data.machines.get(machine_id) if data is not None else None
        return machine.name if machine is not None else machine_id

    def start_run(self, plan) -> bool:
        services = self.services
        name = self._machine_name(plan.base.machine.id)
        if not hx_ui_common.ask(self, Msg(N_("Start the test print on {machine}? It takes the printer for the whole "
                                             "test, about {bags} bags, and ends with printing off and slots 9 and 10 "
                                             "empty."), {"machine": name, "bags": hx_message.test_bag_estimate(plan)}),
                                yes=N_("Start"), no=N_("Go back")):
            return False
        self.run = run = services.test_run(plan)
        self.run_plan = plan
        self.cancel_token = token = CancelToken()
        self.running_now = True
        self.panel.start(plan, token)
        self.panel.grid()
        key = f"test:{plan.run_id}"
        mark_running(key, Msg(N_("A test print on {machine} is still running."), {"machine": name}))
        start_thread(services, f"hx-test-{plan.run_id}", plan.run_id,
                     _marked(key, lambda: run.run(services.bus.post, cancel=token)))
        return True

    def _on_test_event(self, event):
        if self.run_plan is not None and event.run_id == self.run_plan.run_id:
            self.panel.show_event(event)

    def _on_test_finished(self, event):
        if self.run_plan is None or event.result.run_id != self.run_plan.run_id:
            return
        mark_done(f"test:{self.run_plan.run_id}")
        self.running_now = False
        self.panel.finished()
        result = event.result
        if not result.printing_off:
            self.show_red(result)
        else:
            self.hide_red()
            self.run_finished(result)

    def _on_test_failed(self, event):
        if self.run_plan is None or event.ref != self.run_plan.run_id:
            return
        mark_done(f"test:{self.run_plan.run_id}")
        self.running_now = False
        self.panel.finished()
        if self.red_page is not None:
            # Try again failed: printing still isn't known to be off, so the red page stays, says why
            # and offers Try again again (ui.md §2.6 rule 2, §12.5 item 5)
            self.texts.msg(self.red_problem, event.msg)
            _enable(self.end_try_button, True)
            return
        self.run_failed(event.msg)

    def run_finished(self, result):
        pass

    def run_failed(self, msg):
        pass

    # printing not confirmed off

    def show_red(self, result):
        """Stop it at the printer now (ui.md §12.5 item 5): full screen and red, with Try again."""
        texts = self.texts
        if self.red_page is not None:
            _clear(self.red_page, texts)
            self.red_page.destroy()
        name = self._machine_name(self.run_plan.base.machine.id)
        page = self.red_page = tk.Frame(self, background=RED_BG, padx=32, pady=24)
        page.place(x=0, y=0, relwidth=1, relheight=1)
        heading = _red_label(page, 28, True)
        texts.set(heading, N_("Stop it at the printer now"))
        heading.pack(anchor="w")
        couldnt = _red_label(page, 16)
        texts.set(couldnt, N_("Stripes couldn't stop printing on {machines}."), machines=name)
        couldnt.pack(anchor="w", pady=(8, 0))
        press = _red_label(page, 14)
        texts.set(press, N_("Press Print On/Off on that printer now. Its card shows \"Stop it at the printer\" until "
                            "Stripes reads that printing is off."))
        press.pack(anchor="w", pady=(8, 8))
        if result.msg is not None:
            problem = _red_label(page, 14)
            texts.msg(problem, result.msg)
            problem.pack(anchor="w")
        self.red_problem = _red_label(page, 14, True)     # why Try again failed, when it does
        self.red_problem.pack(anchor="w", pady=(8, 0))
        buttons = tk.Frame(page, background=RED_BG)
        buttons.pack(fill="x", pady=(16, 0))
        close = ttk.Button(buttons, style="Hx.Big.TButton", command=self.destroy)
        texts.set(close, N_("Close"))
        close.pack(side="right")
        self.end_try_button = ttk.Button(buttons, style="Hx.Big.TButton", command=self._end_again)
        texts.set(self.end_try_button, N_("Try again"))
        self.end_try_button.pack(side="right", padx=(0, 12))
        page.lift()
        _full_screen(self)
        self.show()

    def hide_red(self):
        if self.red_page is None:
            return
        _clear(self.red_page, self.texts)
        self.red_page.destroy()
        self.red_page = None
        self.red_problem = None
        self.end_try_button = None
        _full_screen(self, False)

    def _end_again(self):
        """The end procedure again, under a new lock (send.md §15.5); an administrator who isn't a
        sender may use it."""
        run, plan = self.run, self.run_plan
        if run is None or self.running_now:
            return
        services = self.services
        self.running_now = True
        _enable(self.end_try_button, False)
        if self.red_problem is not None:
            self.texts.forget(self.red_problem)
            self.red_problem.configure(text="")
        key = f"test:{plan.run_id}"
        mark_running(key, Msg(N_("A test print on {machine} is still running."),
                              {"machine": self._machine_name(plan.base.machine.id)}))
        start_thread(services, f"hx-test-{plan.run_id}", plan.run_id,
                     _marked(key, lambda: run.end_again(services.bus.post)))

    def close_run(self) -> bool:
        """True when the window may close; Close is always allowed on the red page."""
        if self.red_page is not None:
            return True
        if self.running_now:
            if hx_ui_common.ask(self, Msg(N_("Stop the test print?")), yes=N_("Stop test"), no=N_("Keep it running")):
                self.panel.stop()
            return False
        return True


class TestPrintWindow(_TestRunner, HxWindow):
    """Test print: find where and how a film should print, on one machine running empty film (ui.md §12)."""

    def __init__(self, master, services, *, film_id: str, machine_id: str = "", kind: str = "test"):
        if kind != "test":
            raise ValueError(f"the test print window runs kind 'test', not {kind!r}")
        self.key = f"test:{film_id}"
        super().__init__(master, services, title=N_("Test print"))
        self.geometry("1000x760")
        self.minsize(760, 560)
        self.film_id = film_id
        self.plan = None
        self.bold_vars: dict[int, tk.BooleanVar] = {}
        self.width_vars: dict[int, tk.BooleanVar] = {}
        self.resolution_vars: dict[int, tk.BooleanVar] = {}
        self.font_rows: list[tuple[ttk.Frame, RecordBox, tk.StringVar]] = []
        self._choosing: list = []
        self._init_runner()
        self._build(machine_id)
        self.subscribe(DataUpdate, lambda e: self.recompute())
        self.subscribe(ShareHealth, lambda e: self.recompute())
        self.subscribe(BoardUpdate, lambda e: self._show_machine_state())
        self.recompute()

    def _film(self):
        data = self.services.data.current
        return data.films.get(self.film_id) if data is not None else None

    def _build(self, machine_id):
        texts = self.texts
        data = self.services.data.current
        film = self._film()
        self.scroller = Scroller(self)
        self.scroller.pack(fill="both", expand=True)
        body = self.body = ttk.Frame(self.scroller.inner, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        heading = ttk.Label(body, style="Hx.Head.TLabel")
        product = data.products.get(film.product_id) if film is not None else None
        texts.set(heading, N_("Test print: {film} ({product})"), film=film_name(film) if film is not None else "",
                  product=product.name if product is not None else "")
        heading.grid(row=0, column=0, columnspan=3, sticky="w")
        caption = ttk.Label(body)
        texts.set(caption, N_("Machine"))
        caption.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.machine_box = RecordBox(body, width=28)
        machines = [m for m in data.machines.values() if m.active and _printer_ready(data, m)]
        self.machine_box.set_records([(m.id, m.name) for m in _sorted(machines)])
        self.machine_box.set_record_id(machine_id)
        self.machine_box.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.machine_box.bind("<<ComboboxSelected>>", lambda e: self._machine_chosen(), add="+")
        self.machine_state = ttk.Label(body, style="Muted.TLabel")
        self.machine_state.grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        self.empty_film_var = tk.BooleanVar(value=False)
        self.empty_film_check = ttk.Checkbutton(body, variable=self.empty_film_var, command=self.recompute)
        self.empty_film_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        section = ttk.Label(body, style="Hx.Section.TLabel")
        texts.set(section, N_("What to vary"))
        section.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 2))
        vary = ttk.Frame(body)
        vary.grid(row=4, column=0, columnspan=3, sticky="w")
        self.vary_label = ttk.Label(vary)
        self.vary_label.pack(side="left", padx=(0, 6))
        self.start_var, self.stop_var, self.step_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        entries = []
        for english, var in ((N_("from"), self.start_var), (N_("to"), self.stop_var),
                             (N_("in steps of"), self.step_var)):
            caption = ttk.Label(vary)
            texts.set(caption, english)
            caption.pack(side="left", padx=(6, 4))
            entry = ttk.Entry(vary, width=8, textvariable=var)
            entry.pack(side="left")
            var.trace_add("write", lambda *a: self.recompute())
            entries.append(entry)
        self.start_entry, self.stop_entry, self.step_entry = entries
        row = 5
        for english, values, store in ((N_("Bold"), list(BOLD_RANGE), self.bold_vars),
                                       (N_("Width"), list(WIDTH_RANGE), self.width_vars),
                                       (N_("Resolution"), list(RESOLUTIONS), self.resolution_vars)):
            caption = ttk.Label(body)
            texts.set(caption, english)
            caption.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            frame = ttk.Frame(body)
            frame.grid(row=row, column=1, columnspan=2, sticky="w")
            for value in values:
                var = tk.BooleanVar(value=False)
                check = ttk.Checkbutton(frame, variable=var, command=self.recompute)
                if store is self.resolution_vars:
                    texts.set(check, RESOLUTION_CHOICES[value][1])
                else:
                    check.configure(text=str(value))
                check.pack(side="left", padx=(0, 6))
                store[value] = var
                self._choosing.append(check)
            if store is self.bold_vars:
                self.hint_label(frame, N_("None ticked: the film's.")).pack(side="left", padx=(8, 0))
            row += 1
        caption = ttk.Label(body)
        texts.set(caption, N_("Fonts side by side"))
        caption.grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
        fonts = ttk.Frame(body)
        fonts.grid(row=row, column=1, columnspan=2, sticky="w")
        self.fonts_frame = ttk.Frame(fonts)
        self.fonts_frame.pack(side="left")
        self.add_font_button = ttk.Button(fonts, command=self._add_font)
        texts.set(self.add_font_button, N_("Add font"))
        self.add_font_button.pack(side="left", padx=(8, 4))
        self.remove_font_button = ttk.Button(fonts, command=self._remove_font)
        texts.set(self.remove_font_button, N_("Remove font"))
        self.remove_font_button.pack(side="left")
        row += 1
        self.estimate_label = _Lines(body, style="TLabel", sep=" ", wrap=860)
        self.estimate_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.estimate_label.show([])
        row += 1
        self.problems_label = _Lines(body, style="Bad.TLabel", wrap=860)
        self.problems_label.grid(row=row, column=0, columnspan=3, sticky="w")
        self.problems_label.show([])
        row += 1
        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(buttons, style="Hx.Main.TButton", command=self._start)
        texts.set(self.start_button, N_("Start test print"))
        self.start_button.pack(side="right")
        self.close_button = ttk.Button(buttons, command=self.close)
        texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right", padx=(0, 8))
        row += 1
        self.panel = TestRunPanel(body, self)
        self.panel.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.panel.grid_remove()
        row += 1
        self.result_label = _Lines(body, style="TLabel", wrap=860)
        self.result_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.result_label.show([])
        row += 1
        self.cleared_label = _Lines(body, style="Ok.TLabel", wrap=860)
        self.cleared_label.grid(row=row, column=0, columnspan=3, sticky="w")
        self.cleared_label.show([])
        row += 1
        columns = ("step", "mark", "value", "bold", "width", "resolution", "fonts")
        self.steps_tree = ttk.Treeview(body, columns=columns, show="headings", height=6)
        for column, english, width in (("step", N_("Step"), 50), ("mark", N_("Mark"), 150),
                                       ("value", N_("Position or delay"), 110), ("bold", N_("Bold"), 50),
                                       ("width", N_("Width"), 50), ("resolution", N_("Resolution"), 90),
                                       ("fonts", N_("Fonts"), 160)):
            texts.heading(self.steps_tree, column, english)
            self.steps_tree.column(column, width=width)
        self.steps_tree.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.steps_tree.grid_remove()
        row += 1
        self.film_row = ttk.Frame(body)
        self.film_row.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.film_button = ttk.Button(self.film_row, command=self._open_film)
        texts.set(self.film_button, N_("Open the film"))
        self.film_button.pack(side="left")
        self.hint_label(self.film_row, N_("Type the chosen values into the film. That's a normal edit; the film's "
                                          "status doesn't change.")).pack(side="left", padx=(8, 0))
        self.film_row.grid_remove()
        self._choosing += [self.machine_box, self.empty_film_check, self.start_entry, self.stop_entry, self.step_entry,
                           self.add_font_button, self.remove_font_button]
        self._machine_chosen(defaults=True)

    def hint_label(self, master, english):
        label = ttk.Label(master, style="Muted.TLabel", wraplength=_WRAP, justify="left")
        self.texts.set(label, english)
        return label

    def _machine(self):
        data = self.services.data.current
        return data.machines.get(self.machine_box.record_id()) if data is not None else None

    def _machine_chosen(self, defaults=True):
        machine = self._machine()
        film = self._film()
        texts = self.texts
        self.empty_film_var.set(False)
        if machine is None:
            texts.forget(self.empty_film_check)
            self.empty_film_check.configure(text="")
            texts.forget(self.vary_label)
            self.vary_label.configure(text="")
        else:
            texts.set(self.empty_film_check, N_("{machine} is running empty film, with no product."),
                      machine=machine.name)
            encoder = machine.placement == "encoder"
            texts.set(self.vary_label, N_("Print position (mm)") if encoder else N_("Delay (ms)"))
            if defaults and film is not None:
                value = film.position_mm if encoder else film.delay_ms
                step = 10.0 if encoder else 20.0
                if value is None:
                    self.start_var.set("")
                    self.stop_var.set("")
                else:
                    # Worked out, so rounded (1050.1 - 40 is 1010.0999999999999 in floating point)
                    self.start_var.set(_num(round(float(value - 2 * step), 3)))
                    self.stop_var.set(_num(round(float(value + 2 * step), 3)))
                self.step_var.set(_num(step))
        self._show_machine_state()
        self.recompute()

    def _show_machine_state(self):
        machine = self._machine()
        if machine is None:
            self.machine_state.configure(text="")
            return
        mode = tr("Encoder machine") if machine.placement == "encoder" else tr("Delay machine")
        view = None
        board = self.services.watcher.latest
        for line in board.lines if board is not None else []:
            for v in line.machines:
                if v.machine_id == machine.id:
                    view = v
        self.machine_state.configure(text=mode + (f" · {text(view.headline)}" if view is not None else ""))

    def _add_font(self):
        if len(self.font_rows) >= hx_message.MAX_FONT_SAMPLES:
            return
        data = self.services.data.current
        film = self._film()
        frame = ttk.Frame(self.fonts_frame)
        frame.pack(side="left", padx=(0, 8))
        box = RecordBox(frame, width=16)
        box.set_records([(str(i), f"{i}: {name}") for i, name in enumerate(data.company.standard_fonts, start=1)])
        box.set_record_id(str(film.font if film is not None else 2))
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self.recompute(), add="+")
        size = tk.StringVar(value=str(film.size if film is not None else 4))
        size.trace_add("write", lambda *a: self.recompute())
        ttk.Spinbox(frame, from_=1, to=10, width=4, textvariable=size).pack(side="left", padx=(2, 0))
        self.font_rows.append((frame, box, size))
        self.recompute()

    def _remove_font(self):
        if not self.font_rows:
            return
        frame, _, _ = self.font_rows.pop()
        frame.destroy()
        self.recompute()

    def ranges(self, problems):
        machine = self._machine()
        encoder = machine is not None and machine.placement == "encoder"
        field = Msg(N_("Print position (mm)") if encoder else N_("Delay (ms)"))
        values = []
        for var in (self.start_var, self.stop_var, self.step_var):
            try:
                value = parse_number(var.get())
            except ValueError:
                problems.append(Msg(N_("{field}: type a number."), {"field": field}))
                value = None
            else:
                if value is None:
                    problems.append(Msg(N_("{field} is needed."), {"field": field}))
            values.append(value)
        fonts = []
        for _, box, size in self.font_rows:
            try:
                n = parse_number(size.get(), whole=True)
            except ValueError:
                n = None
            fonts.append((int(box.record_id() or 0), n if n is not None else 0))
        if any(v is None for v in values):
            return None
        return TestRanges(vary="position" if encoder else "delay", start=float(values[0]), stop=float(values[1]),
                          step=float(values[2]), bold=[b for b, v in self.bold_vars.items() if v.get()],
                          width=[w for w, v in self.width_vars.items() if v.get()],
                          resolution=[r for r, v in self.resolution_vars.items() if v.get()], fonts=fonts)

    def recompute(self) -> None:
        """The plan after every change: its problems, or how many steps and bags."""
        if not hasattr(self, "start_button"):
            return
        services = self.services
        self.plan = None
        problems = []
        machine = self._machine()
        data = services.data.current
        if machine is None or data is None:
            self.estimate_label.show([])
            self.problems_label.show([])
            self._update_start()
            return
        ranges = self.ranges(problems)
        if ranges is not None and not problems:
            try:
                self.plan = hx_message.plan_test(machine.id, self.film_id, ranges, data, services.identity,
                                                 kind="test")
            except PlanError as e:
                problems = list(e.problems)
            except NotAllowed as e:
                problems = [e.msg]
        if self.plan is not None:
            estimate = [Msg(N_("{steps} steps · about {bags} bags."),
                            {"steps": len(self.plan.steps), "bags": hx_message.test_bag_estimate(self.plan)})]
            if self.plan.timed:
                estimate.append(Msg(N_("Each step runs for about {seconds} s, because no counter counts bags."),
                                    {"seconds": round(machine.seconds_per_bag * self.plan.bags_per_step)}))
            else:
                estimate.append(Msg(N_("Each step runs for {n} bags, counted by the printer."),
                                    {"n": self.plan.bags_per_step}))
            self.estimate_label.show(estimate)
        else:
            self.estimate_label.show([])
        if not services.store.available:
            problems.append(Msg(N_("The server can't be reached, so a test print can't take the printer's lock.")))
        self.problems_label.show(problems)
        self._update_start()

    def _update_start(self):
        services = self.services
        ok = (self.plan is not None and bool(self.empty_film_var.get()) and roles(services).admin
              and services.store.available and not self.running_now)
        _enable(self.start_button, ok)

    def _set_choosing(self, on):
        for widget in self._choosing:
            _enable(widget, on)

    def _start(self):
        if self.plan is None or self.running_now:
            return
        plan = self.plan
        if not self.start_run(plan):
            return
        self._set_choosing(False)
        self.result_label.show([])
        self.cleared_label.show([])
        self.steps_tree.grid_remove()
        self.film_row.grid_remove()
        self._update_start()

    def run_finished(self, result):
        """ui.md §12.5: the outcome, whether the slots are empty, every step's values and the film."""
        name = self._machine_name(self.run_plan.base.machine.id)
        if result.ok:
            self.result_label.show([Msg(N_("Finished: {done} of {of} steps."), {"done": result.steps_done,
                                                                                 "of": result.of})])
        else:
            self.result_label.show([Msg(N_("Stopped after {done} of {of} steps: {problem}"),
                                        {"done": result.steps_done, "of": result.of,
                                         "problem": result.msg if result.msg is not None else ""})])
        if result.cleared:
            self.cleared_label.configure(style="Ok.TLabel")
            self.cleared_label.show([Msg(N_("Printing is off and slots 9 and 10 are empty. {machine} needs a normal "
                                            "Send before production."), {"machine": name})])
        else:
            self.cleared_label.configure(style="Bad.TLabel")
            self.cleared_label.show([Msg(N_("Stripes couldn't empty slots 9 and 10. Check the printer, and send to it "
                                            "before production."))])
        self._show_steps()
        self.film_row.grid()
        self._set_choosing(True)
        self._update_start()

    def run_failed(self, msg):
        self.result_label.show([msg])
        self._set_choosing(True)
        self._update_start()

    def _show_steps(self):
        tree = self.steps_tree
        tree.delete(*tree.get_children())
        plan = self.run_plan
        data = self.services.data.current
        fonts = data.company.standard_fonts if data is not None else []
        for step in plan.steps:
            value = step.position_mm if step.position_mm is not None else step.delay_ms
            unit = "mm" if step.position_mm is not None else "ms"
            samples = ", ".join(f"{fonts[f - 1] if 0 < f <= len(fonts) else f} {s}" for f, s in step.fonts)
            tree.insert("", "end", values=(step.index, step.label, f"{_num(value)} {unit}", step.bold, step.width,
                                           _choice_text(RESOLUTION_CHOICES, str(step.resolution)), samples))
        tree.grid()

    def _open_film(self):
        self.services.nav.open_admin(page="products", record_id=self.film_id)

    def refresh_texts(self) -> None:
        _render_lines(self)
        self._show_machine_state()
        if self.run_plan is not None and self.steps_tree.winfo_manager():
            self._show_steps()

    def close(self) -> None:
        if self.close_run():
            self.destroy()


class CalibrationWindow(_TestRunner, HxWindow):
    """Calibrate an encoder machine once, at installation (OPEN; ui.md §13, send.md §16)."""

    def __init__(self, master, services, *, machine_id: str):
        self.key = f"calibration:{machine_id}"
        data = services.data.current
        machine = data.machines.get(machine_id) if data is not None else None
        super().__init__(master, services)
        self.machine_id = machine_id
        if machine is None or machine.placement != "encoder":
            # Delay machines aren't calibrated: no window
            self.withdraw()
            hx_ui_common.tell(master, Msg(N_("Delay machines aren't calibrated.")))
            self.destroy()
            return
        self.texts.title(self, N_("Calibrate {machine}"), machine=machine.name)
        self.geometry("1000x760")
        self.minsize(760, 560)
        self.spacing_plan = None
        self.head_plan = None
        self.spacing_run = None
        self.head_run = None
        self._spacing_done = False
        self._head_done = False
        self._gap = None
        self._head_mm = None
        self._init_runner()
        self._build()
        self.subscribe(DataUpdate, lambda e: self.recompute())
        self.subscribe(ShareHealth, lambda e: self.recompute())
        self.recompute()

    def _machine(self):
        data = self.services.data.current
        return data.machines.get(self.machine_id) if data is not None else None

    def _calibration(self):
        data = self.services.data.current
        return data.calibrations.get(self.machine_id) if data is not None else None

    def _build(self):
        texts = self.texts
        data = self.services.data.current
        machine = self._machine()
        self.scroller = Scroller(self)
        self.scroller.pack(fill="both", expand=True)
        body = self.body = ttk.Frame(self.scroller.inner, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        row = 0
        intro = ttk.Label(body, style="Muted.TLabel", wraplength=860, justify="left")
        texts.set(intro, N_("Calibrate once, at installation, with empty film. This procedure is still being confirmed "
                            "on the test unit."))
        intro.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        caption = ttk.Label(body)
        texts.set(caption, N_("Film loaded for calibration"))
        caption.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.film_box = RecordBox(body, width=36)
        films = [f for f in data.films.values() if f.length_mm is not None]

        def product_name(f):
            p = data.products.get(f.product_id)
            return p.name if p is not None else ""
        films.sort(key=lambda f: (hx_state.natural_key(product_name(f)),) + _film_key(f))
        self.film_box.set_records([(f.id, f"{film_name(f)} ({_num(f.length_mm)} mm)") for f in films])
        if films:
            self.film_box.set_record_id(films[0].id)
        self.film_box.grid(row=row, column=1, sticky="w", pady=(8, 0))
        self.film_box.bind("<<ComboboxSelected>>", lambda e: self.recompute(), add="+")
        row += 1
        self.empty_film_var = tk.BooleanVar(value=False)
        self.empty_film_check = ttk.Checkbutton(body, variable=self.empty_film_var, command=self.recompute)
        texts.set(self.empty_film_check, N_("{machine} is running empty film, with no product."), machine=machine.name)
        self.empty_film_check.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 1. Rough head distance
        row += 1
        self._section(body, row, N_("1. Rough head distance"))
        row += 1
        caption = ttk.Label(body)
        texts.set(caption, N_("Rough head distance by tape measure (mm)"))
        caption.grid(row=row, column=0, sticky="w", padx=(0, 8))
        rough = ttk.Frame(body)
        rough.grid(row=row, column=1, columnspan=2, sticky="w")
        cal = self._calibration()
        self.rough_var = tk.StringVar(value=_num(cal.rough_head_distance_mm) if cal is not None else "")
        ttk.Entry(rough, width=8, textvariable=self.rough_var).pack(side="left")
        self.rough_var.trace_add("write", lambda *a: self.recompute())
        self.save_rough_button = ttk.Button(rough, command=self._save_rough)
        texts.set(self.save_rough_button, N_("Save rough distance"))
        self.save_rough_button.pack(side="left", padx=(8, 0))
        row += 1
        self._hint(body, row, N_("From the print head back along the film path to where a bag's bottom edge sits when "
                                 "the film stops. It can be longer than one bag."))
        row += 1
        self.rough_status = ttk.Label(body)
        self.rough_status.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.rough_problem = _Lines(body, style="Bad.TLabel", wrap=860)
        self.rough_problem.grid(row=row, column=0, columnspan=3, sticky="w")
        self.rough_problem.show([])

        # 2. Distance scale
        row += 1
        self._section(body, row, N_("2. Distance scale"))
        row += 1
        self.spacing_text = ttk.Label(body, wraplength=860, justify="left")
        self.spacing_text.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.print_spacing_button = ttk.Button(body, command=lambda: self._print("spacing"))
        texts.set(self.print_spacing_button, N_("Print the two marks"))
        self.print_spacing_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1
        self.spacing_problem = _Lines(body, style="Bad.TLabel", wrap=860)
        self.spacing_problem.grid(row=row, column=0, columnspan=3, sticky="w")
        self.spacing_problem.show([])
        row += 1
        marks = ttk.Frame(body)
        marks.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.mark_a_var, self.mark_b_var = tk.StringVar(), tk.StringVar()
        self.mark_entries = []
        for english, var in ((N_("Mark A from the bag's bottom edge (mm)"), self.mark_a_var),
                             (N_("Mark B (mm)"), self.mark_b_var)):
            caption = ttk.Label(marks)
            texts.set(caption, english)
            caption.pack(side="left", padx=(0, 4))
            entry = ttk.Entry(marks, width=8, textvariable=var)
            entry.pack(side="left", padx=(0, 12))
            var.trace_add("write", lambda *a: self._show_gap())
            self.mark_entries.append(entry)
        row += 1
        self.marks_hint = ttk.Label(body, style="Muted.TLabel")
        texts.set(self.marks_hint, N_("Print the marks first, then measure them."))
        self.marks_hint.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.spacing_result = _Lines(body, style="TLabel", sep=" ", wrap=860)
        self.spacing_result.grid(row=row, column=0, columnspan=3, sticky="w")
        self.spacing_result.show([])

        # 3. Head distance
        row += 1
        self._section(body, row, N_("3. Head distance"))
        row += 1
        self.head_text = ttk.Label(body, wraplength=860, justify="left")
        self.head_text.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.print_head_button = ttk.Button(body, command=lambda: self._print("head_distance"))
        texts.set(self.print_head_button, N_("Print the mark"))
        self.print_head_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1
        self.head_problem = _Lines(body, style="Bad.TLabel", wrap=860)
        self.head_problem.grid(row=row, column=0, columnspan=3, sticky="w")
        self.head_problem.show([])
        row += 1
        measured = ttk.Frame(body)
        measured.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        caption = ttk.Label(measured)
        texts.set(caption, N_("Measured from the bag's bottom edge (mm)"))
        caption.pack(side="left", padx=(0, 4))
        self.head_measured_var = tk.StringVar()
        self.head_entry = ttk.Entry(measured, width=8, textvariable=self.head_measured_var)
        self.head_entry.pack(side="left")
        self.head_measured_var.trace_add("write", lambda *a: self._show_head())
        row += 1
        self.head_hint = ttk.Label(body, style="Muted.TLabel")
        texts.set(self.head_hint, N_("Print the mark first, then measure it."))
        self.head_hint.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.head_result = _Lines(body, style="TLabel", wrap=860)
        self.head_result.grid(row=row, column=0, columnspan=3, sticky="w")
        self.head_result.show([])

        row += 1
        ttk.Separator(body).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        row += 1
        self.history_label = _Lines(body, style="Muted.TLabel", wrap=860)
        self.history_label.grid(row=row, column=0, columnspan=3, sticky="w")
        self.history_label.show([])
        row += 1
        self.panel = TestRunPanel(body, self)
        self.panel.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.panel.grid_remove()
        row += 1
        self.run_label = _Lines(body, style="TLabel", wrap=860)
        self.run_label.grid(row=row, column=0, columnspan=3, sticky="w")
        self.run_label.show([])
        row += 1
        self.save_status = _Lines(body, style="Ok.TLabel", wrap=860)
        self.save_status.grid(row=row, column=0, columnspan=3, sticky="w")
        self.save_status.show([])
        row += 1
        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.save_button = ttk.Button(buttons, style="Hx.Main.TButton", command=self._save)
        texts.set(self.save_button, N_("Save calibration"))
        self.save_button.pack(side="right")
        self.close_button = ttk.Button(buttons, command=self.close)
        texts.set(self.close_button, N_("Close"))
        self.close_button.pack(side="right", padx=(0, 8))

    def _section(self, body, row, english):
        label = ttk.Label(body, style="Hx.Section.TLabel")
        self.texts.set(label, english)
        label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))

    def _hint(self, body, row, english):
        label = ttk.Label(body, style="Muted.TLabel", wraplength=860, justify="left")
        self.texts.set(label, english)
        label.grid(row=row, column=0, columnspan=3, sticky="w")

    # what is saved and what the plans send

    def _film(self):
        data = self.services.data.current
        return data.films.get(self.film_box.record_id()) if data is not None else None

    def _saved_rough(self):
        cal = self._calibration()
        return cal.rough_head_distance_mm if cal is not None else None

    def _typed_rough(self):
        try:
            return parse_number(self.rough_var.get())
        except ValueError:
            return None

    def recompute(self) -> None:
        """The plans with the saved rough distance, and what may be pressed now."""
        if not hasattr(self, "save_button"):
            return
        services = self.services
        data = services.data.current
        texts = self.texts
        film = self._film()
        saved = self._saved_rough()
        typed = self._typed_rough()
        rough_ready = saved is not None and typed is not None and float(typed) == float(saved)
        if saved is not None:
            texts.set(self.rough_status, N_("Saved: {mm} mm."), mm=_num(saved))
        else:
            texts.forget(self.rough_status)
            self.rough_status.configure(text="")
        # What a run kept must still be what is saved and loaded, or its marks must be printed again
        for kept in (self.spacing_run, self.head_run):
            if kept is not None and (kept[1] != (film.id if film is not None else "") or kept[3] != saved):
                self._clear_runs()
                break
        self.spacing_plan = self.head_plan = None
        spacing_problems, head_problems = [], []
        if not rough_ready:
            need = [Msg(N_("Save the rough distance first."))]
            spacing_problems, head_problems = need, need
        elif film is not None and data is not None:
            ranges = TestRanges(vary="position", start=0.0, stop=0.0, step=1.0)
            for kind in ("spacing", "head_distance"):
                try:
                    plan = hx_message.plan_test(self.machine_id, film.id, ranges, data, services.identity, kind=kind)
                except PlanError as e:
                    (spacing_problems if kind == "spacing" else head_problems)[:] = list(e.problems)
                    continue
                except NotAllowed as e:
                    (spacing_problems if kind == "spacing" else head_problems)[:] = [e.msg]
                    continue
                if kind == "spacing":
                    self.spacing_plan = plan
                else:
                    self.head_plan = plan
        cal = self._calibration()
        spacing = cal.spacing_sent_mm if cal is not None else 300
        if self.spacing_plan is not None:
            a, b = self.spacing_plan.steps[0].position_mm, self.spacing_plan.steps[1].position_mm
            texts.set(self.spacing_text, N_("Stripes prints two marks {mm} mm apart, at {a} mm and {b} mm after the "
                                            "trigger."), mm=spacing, a=_num(a), b=_num(b))
        else:
            texts.forget(self.spacing_text)
            self.spacing_text.configure(text="")
        if self.head_plan is not None:
            texts.set(self.head_text, N_("Stripes prints one mark {h} mm after the trigger."),
                      h=_num(self.head_plan.steps[0].position_mm))
        else:
            texts.forget(self.head_text)
            self.head_text.configure(text="")
        self.spacing_problem.show(spacing_problems)
        self.head_problem.show(head_problems)
        self._show_history()
        self._update_buttons()

    def _clear_runs(self):
        self.spacing_run = None
        self.head_run = None
        self._spacing_done = False
        self._head_done = False
        for var in (self.mark_a_var, self.mark_b_var, self.head_measured_var):
            var.set("")
        self._gap = None
        self._head_mm = None
        self.spacing_result.show([])
        self.head_result.show([])

    def _update_buttons(self):
        services = self.services
        can_run = (bool(self.empty_film_var.get()) and roles(services).admin and services.store.available
                   and not self.running_now)
        _enable(self.print_spacing_button, can_run and self.spacing_plan is not None)
        _enable(self.print_head_button, can_run and self.head_plan is not None)
        typed = self._typed_rough()
        _enable(self.save_rough_button, typed is not None and roles(services).admin and not self.running_now)
        marks_on = self.spacing_run is not None and self._spacing_done
        for entry in self.mark_entries:
            _enable(entry, marks_on)
        self.marks_hint.grid_remove() if marks_on else self.marks_hint.grid()
        head_on = self.head_run is not None and self._head_done
        _enable(self.head_entry, head_on)
        self.head_hint.grid_remove() if head_on else self.head_hint.grid()
        _enable(self.save_button, (self._gap is not None or self._head_mm is not None) and not self.running_now
                and roles(services).admin)

    def _show_history(self):
        cal = self._calibration()
        lines = []
        if cal is not None and cal.spacing_measured_mm is not None:
            lines.append(Msg(N_("Last spacing check: {mm} mm by {user} at {time}."),
                             {"mm": _num(cal.spacing_measured_mm), "user": cal.spacing_by,
                              "time": when(cal.spacing_at)}))
        if cal is not None and cal.head_distance_mm is not None:
            lines.append(Msg(N_("Last head distance: {mm} mm by {user} at {time}."),
                             {"mm": _num(cal.head_distance_mm), "user": cal.head_by, "time": when(cal.head_at)}))
        self.history_label.show(lines)

    # 1. the rough distance

    def _save_rough(self):
        services = self.services
        typed = self._typed_rough()
        if typed is None:
            return
        cal = copy.deepcopy(self._calibration())
        expected = cal.revision if cal is not None else None
        if cal is None:
            cal = Calibration(id=self.machine_id, machine_id=self.machine_id)
        cal.rough_head_distance_mm = float(typed)
        _enable(self.save_rough_button, False)

        def done(saved, error):
            if not _alive(self):
                return
            if error is not None:
                self.rough_problem.show([_error_msg(error)])
            else:
                self.rough_problem.show([])
            self._update_buttons()

        services.bg.run(lambda: services.data.save(cal, expected_revision=expected), done)

    # 2 and 3. the prints

    def _print(self, kind):
        plan = self.spacing_plan if kind == "spacing" else self.head_plan
        film = self._film()
        if plan is None or film is None or self.running_now:
            return
        saved = self._saved_rough()
        if not self.start_run(plan):
            return
        if kind == "spacing":
            self.spacing_run = (plan, film.id, film.length_mm, saved)
            self._spacing_done = False
            self.mark_a_var.set("")
            self.mark_b_var.set("")
            self._gap = None
            self.spacing_result.show([])
        else:
            self.head_run = (plan, film.id, film.length_mm, saved, int(plan.steps[0].position_mm))
            self._head_done = False
            self.head_measured_var.set("")
            self._head_mm = None
            self.head_result.show([])
        self.run_label.show([])
        self._update_buttons()

    def run_finished(self, result):
        plan = self.run_plan
        if result.ok:
            self.run_label.show([Msg(N_("Finished: {done} of {of} steps."), {"done": result.steps_done,
                                                                              "of": result.of})])
        else:
            self.run_label.show([Msg(N_("Stopped after {done} of {of} steps: {problem}"),
                                     {"done": result.steps_done, "of": result.of,
                                      "problem": result.msg if result.msg is not None else ""})])
        if self.spacing_run is not None and plan is self.spacing_run[0] and result.steps_done == 2:
            self._spacing_done = True
        if self.head_run is not None and plan is self.head_run[0] and result.steps_done >= 1:
            self._head_done = True
        self._update_buttons()

    def run_failed(self, msg):
        self.run_label.show([msg])
        self._update_buttons()

    def _show_gap(self):
        """Gap: the two marks' distance (mod the film length kept with the run), and what it means."""
        self._gap = None
        run = self.spacing_run
        if run is None or not self._spacing_done:
            self.spacing_result.show([])
            self._update_buttons()
            return
        try:
            a, b = parse_number(self.mark_a_var.get()), parse_number(self.mark_b_var.get())
        except ValueError:
            a = b = None
        if a is None or b is None:
            self.spacing_result.show([])
            self._update_buttons()
            return
        length = run[2]
        gap = round(hx_message.spacing_gap_mm(float(a), float(b), length), 3)
        self._gap = gap
        cal = self._calibration()
        sent = cal.spacing_sent_mm if cal is not None else 300
        shown = [Msg(N_("Gap: {gap} mm."), {"gap": _num(gap)})]
        machine = self._machine()
        if abs(gap - sent) <= hx_message.SPACING_TOLERANCE_MM:
            shown.append(Msg(N_("The gap is within {mm} mm: the wheel size is right."),
                             {"mm": _num(hx_message.SPACING_TOLERANCE_MM)}))
            self.spacing_result.configure(style="Ok.TLabel")
        else:
            wheel = None
            if machine is not None and machine.encoder_wheel_mm is not None:
                wheel = hx_message.wheel_suggestion_mm(machine.encoder_wheel_mm, sent, gap)
            if wheel is not None:
                shown.append(Msg(N_("Correct the wheel size on the printer to {wheel} mm and set Expected wheel size "
                                    "on this machine's page to {wheel} mm, then print the marks again."),
                                 {"wheel": wheel}))
            else:
                shown.append(Msg(N_("The gap is {measured} mm instead of {sent} mm. Correct the wheel size on the "
                                    "printer and set the same value as Expected wheel size on this machine's page, "
                                    "then print the marks again."), {"measured": _num(gap), "sent": sent}))
            self.spacing_result.configure(style="Warn.TLabel")
        self.spacing_result.show(shown)
        self._update_buttons()

    def _show_head(self):
        """The head distance from the head print that ran (its DLYMM, film length and rough distance)."""
        self._head_mm = None
        run = self.head_run
        if run is None or not self._head_done:
            self.head_result.show([])
            self._update_buttons()
            return
        try:
            measured = parse_number(self.head_measured_var.get())
        except ValueError:
            measured = None
        if measured is None:
            self.head_result.show([])
            self._update_buttons()
            return
        _plan, _film_id, length, rough, sent = run
        try:
            mm = hx_message.head_distance_from_print(sent, float(measured), length, rough)
        except PlanError as e:
            self.head_result.configure(style="Bad.TLabel")
            self.head_result.show(list(e.problems))
            self._update_buttons()
            return
        self._head_mm = mm
        self.head_result.configure(style="TLabel")
        self.head_result.show([Msg(N_("Head distance: {mm} mm"), {"mm": _num(mm)})])
        self._update_buttons()

    # saving

    def _save(self):
        services = self.services
        cal = copy.deepcopy(self._calibration())
        if cal is None:
            return
        identity = services.identity
        now = services.clock.time()
        head = None
        if self._gap is not None and self.spacing_run is not None:
            cal.spacing_measured_mm = float(self._gap)
            cal.spacing_by = identity.user
            cal.spacing_at = now
        if self.head_run is not None and self._head_mm is not None:
            head = float(self._head_mm)
            cal.head_print_sent_mm = int(self.head_run[4])
            cal.head_print_measured_mm = float(parse_number(self.head_measured_var.get()))
            cal.head_distance_mm = head
            cal.head_by = identity.user
            cal.head_at = now
        name = self._machine().name
        _enable(self.save_button, False)

        def done(saved, error):
            if not _alive(self):
                return
            if error is not None:
                self.save_status.configure(style="Bad.TLabel")
                self.save_status.show([_error_msg(error)])
            else:
                self.save_status.configure(style="Ok.TLabel")
                if head is not None:
                    self.save_status.show([Msg(N_("Saved. The head distance of {machine} is now {mm} mm."),
                                               {"machine": name, "mm": _num(head)})])
                else:
                    self.save_status.show([Msg(N_("Saved {name}."), {"name": name})])
            self._update_buttons()

        services.bg.run(lambda: services.data.save(cal, expected_revision=cal.revision), done)

    def refresh_texts(self) -> None:
        _render_lines(self)

    def close(self) -> None:
        if self.close_run():
            self.destroy()


def _printer_ready(data, machine):
    printer = data.printers.get(machine.printer_id)
    return printer is not None and printer.active and printer.token != ""
