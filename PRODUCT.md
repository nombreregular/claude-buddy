# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

El dueño del proyecto. La pantalla vive en su escritorio, al lado del Mac, a unos 60 cm. La mira de reojo
mientras trabaja con Claude desktop y la toca cuando una sesión pide permiso. Usuario único: no hay otros perfiles
confirmados.

## Product Purpose

Claude Buddy es un compañero físico para Claude desktop: una Raspberry Pi 4 con pantalla táctil de 7" que habla el
protocolo Hardware Buddy por Bluetooth. Muestra qué están haciendo las sesiones, deja aprobar o denegar permisos
tocando la pantalla y tiene un personaje con ánimos, nivel y contadores. Éxito: el dueño sabe de un vistazo si Claude
lo necesita, decide un permiso en segundos sin cambiar de ventana, y le da gusto tenerlo en el escritorio.

## Positioning

Es mitad compañero, mitad herramienta, con igual peso: se mira como mascota y se usa como control. A diferencia del
firmware oficial (M5StickC Plus, pantalla diminuta), tiene una pantalla de 800x480 táctil donde el pedido de permiso
se lee entero y se decide con un dedo.

## Operating Context

- Escritorio del dueño, junto al Mac. Lectura a ~60 cm, en vistazos cortos.
- Chromium en modo kiosco sobre labwc (Wayland), Pi OS Trixie, Pi 4 de 2 GB. Pantalla DSI 800x480, táctil.
- Claude desktop manda latidos (sesiones, trabajando, esperando, mensaje, últimas entradas, tokens de hoy) y,
  cuando un permiso es aprobable desde un dispositivo, un `prompt` con herramienta y pista.
- Solo llegan permisos de herramientas reales (Bash, Edit, etc.) en sesiones que piden permiso. Claude desktop no
  reenvía AskUserQuestion ni pedidos que requieren escribir una respuesta.
- Los latidos solo traen totales. Qué conversaciones están vivas, con su título y su grupo (como la barra lateral de
  Claude desktop), lo cuenta un hook (`tools/context_hook.py`) que corre en cada sesión de Claude Code de la Mac y le
  habla al listener del Pi. Casi todas las sesiones del dueño corren en su carpeta personal, así que el nombre de la
  carpeta no dice nada: lo que sirve es el título de la conversación.

## Capabilities and Constraints

- Pantallas: arranque, esperando a Claude desktop, Bluetooth no listo, emparejamiento (código de 6 dígitos),
  conectado (personaje + sesiones + los hilos vivos, agrupados, con lo que dijo cada uno al tocarlo), pedido de
  permiso (con cola), avisos de aprobado/denegado y sin conexión.
- Ánimos del personaje: durmiendo, tranquilo, trabajando, te necesita, terminó, mareado, contento.
- Fase 4: Claude desktop puede mandar packs de personajes (GIFs de 96 px). El personaje propio y los packs
  **conviven**: el propio es el de siempre y, si hay pack, se puede usar ese. El diseño tiene que funcionar con los dos.
- Fase 5: Hermes Agent será una segunda fuente de aprobaciones en la misma cola (se muestra la fuente).
- Escenario fijo de 800x480 escalado a la ventana. Un solo archivo HTML, sin dependencias, sin nada externo (sin CDN,
  sin fuentes web). CSP `default-src 'none'`.
- Rendimiento Pi 4: animaciones solo con `transform` y `opacity`.
- Privacidad: los títulos, los grupos y hasta 300 caracteres de lo último que dijo cada hilo viajan de la Mac al Pi por
  la red (mejor la tailnet), y se apagan con variables de entorno del hook; detalle en `CLAUDE.md` > Hechos de los hilos.
- Seguridad: el texto recibido se pinta siempre con `textContent`. Los botones de decidir se activan 600 ms después
  de aparecer el pedido (anti toque fantasma). `POST /decide` con token por arranque. Al reiniciar el daemon la
  página se recarga sola.

## Brand Commitments

- El personaje es propio ("Buddy"): no es Clawd ni ningún mascot conocido.
- Textos de UI en español (con voseo) e inglés, elegibles con `--lang es|en`. Sin em dashes.
- Referencias visuales fijadas por el dueño (17 oct 2026, Pinterest): personaje blando de luz con degradé y aura sobre
  fondo casi negro, ojos grandes saltones, burbuja de diálogo clara, píldoras redondas, tipografía geométrica gorda,
  chispas. Nada frío ni técnico: ni rieles, ni líneas finas, ni mayúsculas espaciadas.

## Evidence on Hand

- `ui/index.html`: implementación actual con modo demo (`?demo=cycle` o `?demo=<estado>`).
- Datos reales vistos en hardware: nombre `Claude-XXXX`, sesiones reales y un permiso de Bash aprobado
  tocando la pantalla (17 oct 2026).
- No hay logo, tipografía ni paleta de marca comprometidas.

## Product Principles

1. Un vistazo alcanza: el estado (tranquilo, trabajando, te necesita) se entiende sin leer.
2. Decidir un permiso es seguro antes que rápido: qué herramienta, qué hace y quién lo pide, siempre a la vista.
3. El compañero tiene vida propia, pero nunca tapa una decisión pendiente.
4. Lo que llega de afuera es dato, nunca formato.

## Accessibility & Inclusion

Objetivos táctiles grandes (dedo, no cursor; 44 px de alto como mínimo en las filas de una lista, 94 px y 116 px en dígitos
y botones de decidir) y contraste suficiente para leer de reojo a ~60 cm.
