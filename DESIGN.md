---
name: Claude Buddy
description: Un cuerpo blando de luz en un cuarto a oscuras que dice en una burbuja lo que hace Claude; pantalla táctil fija de 800x480.
colors:
  ground: "#0E0D14"
  paper: "#F7F2EA"
  ink: "#1B1626"
  ink-2: "#6E6679"
  bone: "#F3EEF7"
  bone-2: "rgba(243,238,247,.72)"
  bone-3: "rgba(243,238,247,.5)"
  sleep: "#A78BFA"
  idle: "#FF9E7A"
  busy: "#4FD1C5"
  attn: "#FFCF5C"
  celebrate: "#8BE76A"
  dizzy: "#A9B8CF"
  heart: "#FF8FA3"
  ok: "#7CE38B"
  no: "#FF6B6B"
  pet-bulb: "#FFF8EC"
  pet-cheek: "#FF7A8A"
  pet-heart: "#E0427A"
  pet-tongue: "#FF6F7D"
  pet-spark: "#FFF3B8"
  pet-shadow: "#000000"
  pack-dark: "#141414"
  pair-ok: "#1F7A44"
  digit-cell: "rgba(255,255,255,.06)"
typography:
  display:
    fontFamily: "Nunito Sans, URW Gothic, Avenir Next, Trebuchet MS, sans-serif"
    fontSize: "44px"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Nunito Sans, URW Gothic, Avenir Next, Trebuchet MS, sans-serif"
    fontSize: "30px"
    fontWeight: 800
    lineHeight: 1.08
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Nunito Sans, URW Gothic, Avenir Next, Trebuchet MS, sans-serif"
    fontSize: "24px"
    fontWeight: 800
    lineHeight: 1
  body:
    fontFamily: "Nunito Sans, URW Gothic, Avenir Next, Trebuchet MS, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.35
  label:
    fontFamily: "Nunito Sans, URW Gothic, Avenir Next, Trebuchet MS, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    lineHeight: 1.2
  mono:
    fontFamily: "DejaVu Sans Mono, Noto Sans Mono, SF Mono, Menlo, monospace"
    fontSize: "21px"
    fontWeight: 400
    lineHeight: 1.36
rounded:
  track: "4px"
  chip: "8px"
  digit: "16px"
  bubble: "28px"
  pill: "999px"
spacing:
  pill-gap: "10px"
  inner: "12px"
  col-gap: "14px"
  gutter: "24px"
  bar: "56px"
  tap-floor: "44px"
  thread-gap: "10px"
  thread-indent: "18px"
components:
  pill:
    backgroundColor: "color-mix(in srgb, var(--accent) 16%, rgba(255,255,255,.04))"
    textColor: "{colors.bone-2}"
    rounded: "{rounded.pill}"
    padding: "9px 16px"
  pill-hot:
    backgroundColor: "{colors.attn}"
    textColor: "rgba(27,22,38,.72)"
    rounded: "{rounded.pill}"
    padding: "9px 16px"
  pill-sm:
    backgroundColor: "color-mix(in srgb, var(--accent) 16%, rgba(255,255,255,.04))"
    textColor: "{colors.bone-2}"
    rounded: "{rounded.pill}"
    padding: "6px 12px"
  bubble:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.bubble}"
    padding: "20px 24px 18px"
  mood-pill:
    backgroundColor: "var(--accent)"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "8px 18px"
  button-ok:
    backgroundColor: "{colors.ok}"
    textColor: "{colors.ink}"
    typography: "{typography.headline}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "116px"
  button-no:
    backgroundColor: "transparent"
    textColor: "{colors.bone}"
    typography: "{typography.headline}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "116px"
  digit:
    backgroundColor: "rgba(255,255,255,.06)"
    textColor: "{colors.bone}"
    rounded: "{rounded.digit}"
    width: "56px"
    height: "94px"
  digit-done:
    backgroundColor: "{colors.ok}"
    textColor: "{colors.ink}"
    rounded: "{rounded.digit}"
    width: "56px"
    height: "94px"
  chip:
    backgroundColor: "rgba(27,22,38,.08)"
    textColor: "{colors.ink}"
    rounded: "{rounded.chip}"
    padding: "0 8px"
  card:
    backgroundColor: "color-mix(in srgb, var(--accent) 12%, rgba(255,255,255,.04))"
    rounded: "{rounded.bubble}"
  thread-row:
    backgroundColor: "color-mix(in srgb, var(--accent) 22%, transparent)"
    textColor: "color-mix(in srgb, var(--ink) 78%, var(--paper))"
    rounded: "{rounded.pill}"
    padding: "0 14px 0 18px"
    height: "{spacing.tap-floor}"
  thread-row-idle:
    backgroundColor: "color-mix(in srgb, var(--ink) 7%, transparent)"
    textColor: "color-mix(in srgb, var(--ink) 78%, var(--paper))"
    rounded: "{rounded.pill}"
    padding: "0 14px 0 18px"
    height: "{spacing.tap-floor}"
  group-caption:
    textColor: "{colors.ink-2}"
    padding: "0 0 0 18px"
    height: "16px"
  volver:
    backgroundColor: "color-mix(in srgb, var(--accent) 30%, transparent)"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0 20px 0 14px"
    height: "{spacing.tap-floor}"
  toast:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "12px 24px"
---

# Design System: Claude Buddy

## Overview

**Creative North Star: "El cuarto a oscuras y la burbuja"**

Buddy es un cuerpo blando de luz sentado en un cuarto casi negro. Su color es su ánimo y tiñe el cuarto: un
resplandor radial detrás de él, y todas las píldoras de la pantalla llevan un 16% de ese mismo color. Lo que hace
Claude no lo dice la pantalla, lo dice Buddy: todo texto que viene de la sesión (mensaje, pista del permiso,
instrucciones) va dentro de una burbuja de papel claro con cola hacia él. Lo demás son píldoras redondas.

La pantalla es un escenario fijo de 800x480 escalado a la ventana; nunca hay layout fluido ni breakpoints. Se lee
de reojo a 60 cm, así que la jerarquía es gorda y corta: Nunito Sans 800 para lo que se mira, 600 para lo que se
lee. El sistema vive en dos modos sobre los mismos tokens: con el Buddy SVG propio (el color del ánimo lo define
`body[data-mood]`), o con un pack GIF (`body.pack`) donde el manifest del pack pone `--ground`, `--bone`, `--bone-2`
y `--accent` inline y la burbuja pasa a ser un tinte del texto sobre el fondo.

Rechazos confirmados por el usuario (17 oct 2026): el riel de líneas finas, el dashboard de tarjetas con métricas,
mayúsculas espaciadas y en general cualquier look frío o técnico.

**Key Characteristics:**
- Fondo casi negro teñido por un resplandor radial del color del ánimo.
- Siete colores de ánimo; `--accent` sigue al ánimo y de él se tiñen píldoras, placa, barra de XP y tarjeta. Mientras
  Buddy reacciona a un toque, el ánimo que se ve es el del toque pero todo lo que informa estado sigue al del servidor
  (`--accent-real`): ver Buddy (signature).
- Burbuja de papel con cola para todo lo que Buddy "dice"; píldoras 999px para todo lo que se cuenta.
- Las conversaciones de Claude que siguen vivas son cápsulas de 44px dentro de la burbuja, agrupadas como en la barra
  lateral de Claude desktop; lo que dijo cada una aparece solo al tocarla.
- Nunito Sans 800/700/600, tabular en todo número; mono solo para el comando del permiso.
- Solo `transform` y `opacity` se animan; el aterrizaje de gelatina al cambiar de ánimo es la firma.

## Colors

Un negro violáceo como cuarto, papel cálido como voz, y siete luces de ánimo que mandan sobre todo lo demás.

### Primary
- **Luz del ánimo** (`--accent`, uno de los siete de abajo): color del cuerpo de Buddy, del resplandor, de la
  píldora de ánimo, del relleno de la barra de XP y de los numeradores de pasos. Toda píldora translúcida es
  `color-mix(in srgb, var(--accent) 16%, rgba(255,255,255,.04))`; la tarjeta de dígitos usa 12%; la pista de XP 22%.
  Dentro de la burbuja, sobre papel, la fila de un hilo que trabaja usa 22% y el botón Volver 30% (ver la excepción
  de la regla del 16%).
- **Lavanda dormido** (`sleep`): ánimo por defecto, y forzado en las pantallas de espera y Bluetooth caído.
- **Arena tranquilo** (`idle`): pálido, casi papel, para que el cuarto quede en calma.
- **Menta trabajando** (`busy`): el cuerpo lleva degradé menta a azul (#BDF8EE, #4FD1C5, #2F6FE8).
- **Amarillo te necesita** (`attn`): también es `--ask`; enciende la píldora "esperan" y la de cola.
- **Lima terminó** (`celebrate`), **gris azul mareado** (`dizzy`), **rosa contento** (`heart`).

### Secondary
- **Verde aprobar** (`ok`): botón Aprobar relleno con resplandor, dígito ya tecleado, aviso "Aprobado".
- **Rojo denegar** (`no`): aro de 3px del botón Denegar, aviso "Denegado", red caída en la barra, fondo al 14% del
  bloque de motivo.

### Neutral
- **Cuarto** (`ground`): fondo único; los avisos lo repiten al 94% de opacidad para tapar la pantalla.
- **Papel** (`paper`) y **Tinta** (`ink`, `ink-2`): burbuja y toast; `ink-2` es la segunda voz dentro del papel.
- **Hueso** (`bone`, `bone-2`, `bone-3`): texto sobre el cuarto en tres pesos de opacidad; `bone-3` solo para la
  etiqueta de demo.

### Named Rules
**La regla del papel.** Lo que viene de la sesión (mensaje, acciones, pista, pregunta) se pinta siempre en tinta
sobre papel dentro de una burbuja; nunca directo sobre el cuarto. Lo que es dato propio del buddy (contadores,
nivel, ánimo, hora) va en hueso sobre el cuarto.

**La regla del 16%.** Ninguna píldora, placa o tarjeta tiene color propio: se tiñe del `--accent` vía `color-mix`.
Un solo cambio de ánimo recolorea toda la pantalla sin tocar un componente. Excepción: el 16% es de las superficies
que están sobre el cuarto. Lo que vive sobre el papel de la burbuja necesita más cuerpo para verse: la fila de un hilo
que trabaja es `color-mix(in srgb, var(--accent) 22%, transparent)` y Volver 30%; la fila de un hilo que terminó no
lleva acento sino tinta al 7%. Ninguno inventa un color: salen del acento o de la tinta.

**La regla del pack.** Con `body.pack` los únicos valores que entran de afuera son `bg`, `text`, `textDim`, `body`,
ya filtrados a `#rrggbb` por el servidor, y entran como `--ground`, `--bone`, `--bone-2`, `--accent`. El papel pasa a
`color-mix(in srgb, var(--bone) 9%, var(--ground))`, la tinta a `--bone`, y el texto sobre superficies de acento
pasa a `--ground`. El resplandor se oculta.

## Typography

**Display Font:** Nunito Sans (con URW Gothic, Avenir Next, Trebuchet MS, sans-serif)
**Body Font:** Nunito Sans (misma pila)
**Label/Mono Font:** DejaVu Sans Mono (con Noto Sans Mono, SF Mono, Menlo, monospace)

**Character:** Una sola geométrica redonda y gorda, instalada en el Pi: nada se descarga. El peso hace la
jerarquía, no el tamaño ni las mayúsculas. Todo número lleva `font-variant-numeric: tabular-nums`.

### Hierarchy
- **Display** (800, 44px, 1, -0.02em): solo el nombre de la herramienta en el pedido de permiso; cae a 30px/1.05
  (`.long`) si pasa de 12 caracteres. Máximo 2 líneas.
- **Headline** (800, 30px, 1.08, -0.01em): títulos de burbuja, mensaje actual (2 líneas, recortado), reloj, texto
  de los botones de decidir y del aviso, y el nombre del hilo en el detalle (una línea; si no cabe baja a 21px/22px
  en dos líneas, `#task-view.long`). Variante 28px para "sin novedades" y la pantalla sin conexión.
- **Title** (800, 24px, 1): el número dentro de una píldora; 18px en `.sm`. El dígito del código es 70px/800, un
  tamaño propio de esa pieza.
- **Body** (600, 18-19px, 1.3-1.4): notas en `ink-2`, pasos de emparejar e intención de la pista; 17px/600 para
  acciones recientes, el motivo de Bluetooth y el texto de la fila de un hilo; 16px/700 para la palabra de la píldora;
  18px/600 en tinta para lo que dijo un hilo en el detalle (`white-space: pre-line`, respeta los saltos de párrafo).
- **Label** (700, 13-15px): nombre de red, antigüedad del pedido, nombre y nivel en la placa; 14px en la píldora
  chica, en el rótulo de grupo y en la edad de la fila; 15px en la segunda línea del detalle; 13px para "sin cifrado".
  "Antes dijo:" en el detalle es 14px/800 en `ink-2`.
- **Mono** (400, 21px, 1.36): solo la pista cruda del permiso (el comando). Cae a 17px/1.4 si pasa de 220
  caracteres. Si no hay pista, el contexto de la sesión vuelve a Nunito Sans 600/19px.

### Named Rules
**La regla de los dos pesos.** Lo que se mira va en 800; lo que se lee va en 600; 700 solo para etiquetas
chicas y píldoras. Nada en 400 salvo el comando mono.

**La regla del vistazo.** Nunca mayúsculas espaciadas ni letter-spacing positivo; los títulos van apretados
(-0.01em / -0.02em). Si no se lee a 60 cm, el tamaño sube, no el contraste.

## Layout

Escenario absoluto de 800x480 (`#stage`), centrado y escalado con `transform: scale(var(--s))` al tamaño de la
ventana; no hay grilla fluida ni breakpoints. Barra superior de 56px (red a la izquierda a 20px, reloj a la
derecha a 24px). Debajo, dos zonas: la izquierda para Buddy (192px en x 74 / y 144, el mismo tamaño y lugar en
todas las pantallas) con su placa a y 370 (nombre,
píldora de ánimo, nivel con barra de 120x8); la derecha, una columna en x 344 a 776 (24px de margen) con `gap`
14px entre burbuja, fila de píldoras y pie. Dentro de una burbuja el ritmo es 12px; entre píldoras 10px.

El pedido de permiso rompe la columna: herramienta y antigüedad en un bloque de 296px a la izquierda, Buddy
quieto donde estaba (no se achica ni se mueve: antes saltaba a la mitad con cada pedido), la pista en burbuja de
280px de alto a la derecha, y abajo dos botones de 116px de alto que ocupan todo el ancho con 24px entre ellos y
22px al borde. El resplandor tampoco se mueve.

Todo objetivo táctil mide al menos 94px de alto cuando es un dígito y 116px cuando decide un permiso: dedo, no
cursor. El piso para todo lo demás es 44px (`tap-floor`): las filas de hilo y el botón Volver. Una lista de varias
filas no cabe en la burbuja con filas de 94px; 44px es lo mínimo para un dedo y nada táctil baja de ahí.

Los hilos viven dentro de la misma burbuja del feed, que mide 284px fijos con o sin ellos. La lista tiene una ventana
de 219px (201 de contenido más 18 que sangran sobre el borde de abajo de la burbuja; cuatro filas a la vista) y hace scroll vertical con el
dedo, sin encadenar el scroll a la página (`overscroll-behavior: contain`) ni imanes (`scroll-snap`). Que hay más de
lo que cabe se dice siempre y no por suerte de la aritmética de filas y rótulos (con tres filas de un solo grupo la
cuarta queda del todo escondida): una barra de 16px que un dedo agarra (pulgar de 12px de tinta al 25% con 2px de borde transparente, hueso al 30%
en modo pack, sobre un riel al 6%/8%; igual en el detalle) que se dibuja mientras la lista desborda, y la última fila visible se desvanece en 16px hacia el borde de
la burbuja (`mask-image`, estático, sin animar), así ninguna fila termina plana contra el borde ni asoma un filo de
3px. El ancho de la barra se reserva siempre (`scrollbar-gutter: stable`) y vive en el margen de la burbuja (la lista
sale 10px a la derecha y deja 4px de aire), así las filas miden 384px con dos hilos o con ocho. Filas
de 44px con 10px entre ellas; el rótulo de grupo mide 16px y se pega a las filas de su grupo (queda a 4px de la
primera). Un hilo hijo se sangra 18px y mide 366px de ancho en vez de 384. El detalle de un hilo ocupa el lugar de la
lista dentro de esa misma burbuja: mismo alto, misma cola, y nada de lo que está debajo (píldoras, pie) se mueve.

## Elevation & Depth

No hay sombras de caja estructurales. La profundidad es luz: el resplandor radial detrás de Buddy (620px, opacidad
.2 respirando entre .16 y .26 cada 6s; .3 y 1.4s en atención; .12 dormido), el aura y los dos anillos del SVG
(`stroke` del acento a .14 y .07), y la sombra elíptica negra al 45% bajo el cuerpo. Las superficies se separan
por tono: papel sobre cuarto, píldora translúcida sobre cuarto, aviso al 94% sobre todo.

### Shadow Vocabulary
- **Resplandor de aprobar** (`box-shadow: 0 12px 44px rgba(124,227,139,.28)`): solo el botón Aprobar; es luz
  verde, no elevación.
- **Aro de denegar** (`box-shadow: inset 0 0 0 3px var(--no)`): borde sin `border`, para que el botón no cambie
  de tamaño.

### Named Rules
**La regla de la luz.** Lo que destaca brilla con su color; nada se levanta con sombra gris. Si hace falta
separar, se cambia el tono o se mete en una burbuja.

## Shapes

Todo es redondo. Las píldoras son cápsulas (999px); las burbujas y la tarjeta de dígitos llevan 28px; el dígito
16px; el bloque de motivo 16px; el chip inline 8px; la pista de XP 4px. La burbuja tiene cola: un cuadrado de 26px
rotado 45° con radio 5px, a 9px fuera del borde izquierdo (y 40px; y 150px en la burbuja de la pista), siempre
apuntando a Buddy. Los numeradores de pasos son círculos de 30px en el acento. Los íconos son trazos de 2.4 a
3.2px con remates redondos, dibujados inline en SVG (bluetooth, candado, check, equis); no hay fuente de íconos.
Buddy mismo es un óvalo con antena, bombillo, brazos de fideo de 17px y ojos saltones blancos; el cuerpo lleva un
degradé claro a saturado a profundo por ánimo más una sombra interior desde abajo (`#g-shade`).

## Components

### Buttons (decidir)
- **Shape:** cápsula de 116px de alto, `flex:1`, radio 999px, ícono de 32px más texto a 30px/800.
- **Aprobar una vez** (`.btn.ok`): relleno `ok`, texto `ink`, resplandor verde. En pack el texto es #141414.
- **Denegar** (`.btn.no`): sin relleno, aro interior de 3px en `no`, texto `bone`, ícono en `no`.
- **Siempre** (`.btn.always`): solo si el pedido trae una regla (Claude Code en terminal). Va entre Denegar y Aprobar,
  sin relleno, aro interior de 3px en `ok`, ícono de doble check en `ok`. Con tres botones (`.btns.three`) el texto baja a
  26px y Aprobar toma `flex:1.35`. La regla se lee completa en `#always-note` (16px/700, "Siempre" en `bone`) bajo la
  burbuja, que baja a 232px de alto para hacerle lugar: nunca un Siempre sin la regla a la vista.
- **Armado** (`.btns.hold`): al aparecer el pedido los botones quedan al 45% de opacidad y una barra de 3px en
  `currentColor` (al 80%) crece de izquierda a derecha en 600ms lineal (`@keyframes arm`); tocar antes no decide.
  No bajar los 600ms.
- **Enviando** (`.btns.busy`): el botón no elegido baja al 22%. `:active` encoge a .97 en 80ms.
- **Pregunta** (`.question`): si el pedido es `AskUserQuestion` no hay botones; se muestra `#ask-note` a 18px/700.

### Chips (píldoras)
- **Píldora** (`.pill`): acento al 16%, palabra a 16px/700 en `bone-2`, número a 24px/800 en `bone`, 9px 16px.
- **Chica** (`.pill.sm`): 6px 12px, 14px / 18px. Pie de tokens, aprobadas, denegadas; también la cola "+N en cola".
- **Encendida** (`.pill.hot`): relleno `attn`, texto tinta al 72%, número en tinta. Solo cuando hay sesiones
  esperando o hay cola. En pack el texto pasa a `ground`.
- **Red** (`.net`): misma receta a 15px/700, ícono de 16px; `bone` si está enlazado, `no` si está caída, "sin
  cifrado" a 13px en `attn`.
- **Ánimo** (`#mood`): relleno pleno del acento, tinta, 18px/800, 8px 18px. Es la única píldora opaca.
- **Chip inline** (`.chip`): tinta al 8% sobre papel, radio 8px, 800; nombre de red dentro de una frase.

### Cards / Containers
- **Burbuja** (`.bubble`): papel, tinta, 28px, 20px 24px 18px, cola a la izquierda. Contiene título 30px, lista de
  acciones 17px, nota 18px; ritmo 12px. El feed mide 284px fijos y recorta; con hilos, la lista hace scroll adentro
  (ver Layout). La burbuja de la pista mide 280px fijo y hace scroll interno (barra de 6px, tinta al 25%).
- **Tarjeta de dígitos** (`.card`): acento al 12%, 28px, centra seis dígitos de 56x94 con 6px entre ellos y un hueco
  de 14px al medio; el dígito tecleado se pone en `ok`.
- **Motivo** (`.reason`): rojo al 14%, 16px, 12px 14px, máximo 150px con scroll.

### Hilos (las conversaciones vivas)
Un hilo es una conversación de Claude, de desktop o de terminal, que sigue viva: trabaja ahora, o terminó su turno y
espera tu respuesta. No hay historial. Los hilos viven dentro de la burbuja del feed, agrupados como en la barra
lateral de Claude desktop; cada sesión de la Mac cuenta lo suyo por el hook y la página los recibe en `view.tasks`.

- **Fila** (`.trow`, un `<button>` dentro de `li.task`): cápsula de 44px y 384px de ancho, padding `0 14px 0 18px`,
  8px entre piezas. De izquierda a derecha: el nombre en 800 y tinta (hasta 150px, recorta), qué hace en 600
  (herramienta y resumen, recorta), la marca de visto de 14px (solo si terminó), la edad (`m:ss`, 14px/700, tabular)
  y un chevron de 14px. El texto secundario va
  en tinta al 78% sobre papel (contraste de 6.8 a 8.2). Con título, la fila es el título y se queda con todo el ancho;
  qué herramienta usa se lee en el detalle (en una fila caben unas 30 letras). Sin título, el nombre es la carpeta o,
  si tampoco hay, "sesión". Se toca la cápsula entera; `:active` la encoge a .98 en 80ms.
- **Trabajando**: acento al 22% sobre papel. **Terminó y espera tu respuesta** (`li.task.idle`): tinta al 7%, una
  marca de visto (`.ck`, trazo de 2.6 en tinta, 14px) antes de la edad y, en las filas sin título, un texto que empieza
  con "terminó:". La marca existe porque el tinte solo no alcanza: en algunos ánimos (el gris azulado de `dizzy`, el
  durazno de `idle`) el 22% del acento y el 7% de tinta se parecen, y con título la fila no dice "terminó". Dos
  señales, tinte y marca, y ninguna depende de leer el texto. Nada se atenúa con opacidad: el texto de un hilo terminado es justo el que se va a
  leer. Una fila cambia de aspecto cuando cambia su estado, no de lugar.
- **Rótulo de grupo** (`li.grp`): el nombre del grupo en 14px/700 `ink-2`, caja normal (nada de mayúsculas ni letras
  espaciadas), sin línea ni cápsula, sangrado 18px para quedar alineado con el texto de las filas. Los hilos sin grupo
  (una sesión de terminal que Claude desktop no conoce) caen bajo "Terminal". Tocar un rótulo no abre nada.
- **Hijo** (`li.task.kid`): un hilo que otro lanzó cuelga debajo de su padre, sangrado 18px (366px de ancho), dentro
  del grupo del padre. Solo cuelga si el padre también está vivo en la lista; si la cadena sube más de un nivel, todos
  cuelgan del más alto que sigue vivo, con una sola sangría. Un ciclo, o una cadena de más de 4 padres, deja al hilo
  como una fila corriente.
- **Detalle** (`#task-view`): se abre al tocar una fila y ocupa el lugar de la lista y del titular dentro de la misma
  burbuja (misma altura y cola; `#feed.reading` esconde todo lo demás). Arriba, a 44px de alto, el nombre del hilo
  (Headline 30px/800, o 21px en dos líneas si no cabe) y el botón Volver. Debajo, una línea de 15px/700 en `ink-2`:
  `hace m:ss · <grupo> · <qué hizo>` (la edad es el tiempo desde que el hilo habló por última vez). Después, lo que
  dijo, a 18px/600 en tinta, entre comillas (las pone el CSS, no son parte del texto) y con scroll adentro (barra de
  6px). Si el hilo está trabajando, lo que muestra es del turno anterior y arranca con "Antes dijo:". Sin mensaje: "Todavía
  no dijo nada." (trabajando) o "No dejó ningún mensaje." (terminó), en `ink-2` y sin comillas. Si el hilo sale de la
  lista mientras lo leés, el detalle conserva lo último que se supo y su edad sigue corriendo.
- **Volver** (`.tv-back`): cápsula de 44px, acento al 30%, chevron de 16px a la izquierda y "Volver" en 16px/800. Es la
  única forma táctil de cerrar. Un Volver que llega en los primeros 350ms de abierto el detalle es el rebote del dedo
  que abrió la fila, no una orden: no cierra.
- **Pack**: filas y detalle no llevan CSS propio con `body.pack`. Las tintas salen de `ink` y `accent`, que el pack
  remapea, y se invierten solas. Lo único aparte es el color del pulgar de la barra de `#tv-said` (hueso al 30%).

### Named Rules
**La regla de la lectura.** El texto de una sesión aparece solo tras un toque y se cierra solo a los 25 s. En la
lista nunca se ve lo que dijo un hilo, solo quién es y qué hace; al tocar su fila aparece lo que dijo. Sin tocar
nada (o sin tocar el texto: un `pointerdown` o un scroll dentro de él reinician la cuenta) el detalle vuelve a la lista
a los 25 s. Un permiso siempre gana: si llega uno mientras el detalle está abierto, el detalle se cierra y al resolver
el permiso queda la lista, no el detalle.

**La regla del lugar.** El orden de la pantalla no depende de lo que cambie en el servidor (estado, edad, orden de
llegada). Cada hilo y cada grupo conserva el lugar en que se vio por primera vez (en el primer dibujo, el orden del
servidor: trabajando primero, lo más nuevo arriba); lo que llega después va al final de su grupo y un grupo nuevo al
final de la lista. Nada salta bajo el dedo.

### Avisos
- **Flash** (`.flash`): cuarto al 94%, aro de 150px con borde 4px en `ok` o `no`, ícono de 70px, píldora de texto a
  30px/800 rellena del mismo color; entra con `popin` (.45s, rebote) y dura 850ms.
- **Sin conexión** (`#offline`): mismo velo, 28px/800 y nota 16px/700 en `bone-2`.
- **Toast** (`#toast`): píldora de papel al pie, 19px/700, aparece en 200ms y vive 2.6s.

### Eco del toque
- **Ping** (`#ping`): un solo elemento de 56px, acento al 25%, que crece de .6 a 1.15 y se desvanece en 140ms (solo
  `transform` y `opacity`), `pointer-events:none`, `aria-hidden`. Aparece donde el dedo toca una parte muerta de la
  pantalla (también las partes muertas de un permiso) y cuando Buddy rechaza el toque (`attention`, enfriamiento del
  mareo). Nunca sobre botones, filas, `#feed-list`, `#tv-said`, `.hint` ni `.reason`; un elemento táctil nuevo se agrega
  a `LIVE_TOUCH` o hará eco. Se posiciona desde el rectángulo del escenario escalado.

### Avisos (de Hermes)
- **Fila**: cápsula de 44px como las de los hilos, en el grupo "Avisos · N" arriba de la lista (hasta 3), con una campana de 18px
  (`currentColor`), el título y la edad a la derecha. El tinte sigue al tipo: `info` el acento (22%, como un hilo que trabaja),
  `ok` el verde `--ok` al 34%, `warn` el ámbar `--ask` al 38%; en modo pack 22%, más una marca de visto o de exclamación que no
  depende del color.
- **Detalle** (`#task-view`): cabecera de 44px con el título, **Listo** (una píldora sólida `--ok` con tinta oscura #1B1626, 7:1, en
  todos los temas) y **Volver** exactamente donde está en el detalle de un hilo (así el hábito de tocar ahí nunca descarta un
  aviso); Listo queda a su izquierda con 8px de hueco. El título baja de 30px a 21 y 19 según el largo (hasta cinco líneas), la
  línea de abajo dice "Aviso · de <source> · hace <edad>" y el cuerpo va con `white-space: pre-line`, sin comillas.
- **Aviso nuevo**: Buddy pasa a `attention` 4s por la maquinaria del toque (un solo temporizador); no repite al recargar.

### Modo noche
- **Capa `#dim`** (z-index 80, encima del toast 50, la etiqueta de demo 60, `#offline` 40, el flash 30 y el eco 25): una sola capa del
  tamaño del escenario, inerte (sin toques, transparente) salvo cuando el daemon manda `view.dim`. Con `view.backlight` la luz la
  baja el panel y la capa solo atrapa el toque, sin oscurecer (no se oscurece dos veces); sin backlight oscurece al 78% de negro
  (600ms de entrada, 300ms de salida, solo `opacity`). Un permiso (`ask`) la apaga de golpe: nunca tapa ni oscurece un permiso.
- **El toque que despierta** se consume en `click` y no llega a nada de abajo; después la capa sigue atajando 350ms (alargados hasta
  600ms desde que apoyó un dedo dentro de esa guarda). De noche nada responde al toque mientras está oscura. Do: cualquier capa
  nueva va por debajo de 80. Don't: poner un control encima de `#dim`, reaccionar al toque con la pantalla oscura, ni atenuar un permiso.

### Buddy (signature)
Un solo SVG de 300x310 compartido por todas las pantallas. El ánimo elige cuerpo, ojos, boca y efectos por clase
(`.b-*`, `.e-*`, `.m-*`, `.fx-*`). Al cambiar de ánimo, `.jelly.land` aplasta y estira el cuerpo en 620ms
(`scale(1.14,.82)` a `scale(.93,1.09)` a `scale(1.04,.97)` a reposo) desde la base (150px 282px): ese aterrizaje
es la firma del sistema. Cada ánimo tiene su ciclo de cuerpo (respirar 5s, flotar 3.4s, mecerse 1.6s, saltito
1.1s, salto .7s, tambalear 1s, balanceo 2.6s) y sus efectos (zetas, puntos, chispas, confeti, estrellas en órbita,
corazones). En pack, el GIF de 96px se muestra a 192px en todas las pantallas (2x: píxeles parejos), y el
SVG y el resplandor se ocultan. Las imágenes del pack llevan `?v=<rev>` en la URL (el `rev` del daemon): un pack ya en
pantalla se reemplaza recién cuando todas las nuevas cargaron o fallaron, así no hay un cuadro vacío.

**Al tacto.** Una zona transparente de 192x196 en x74/y140 (más de 120x120, a 34px de la placa y a 78px de la columna
derecha) cubre la figura en los dos modos; se oculta solo en `boot`. Dormido, el primer toque pone `idle` 2500ms (los
toques de esa ventana valen como corazones); despierto (menos `attention`), un toque pone `heart` 2500ms y otro lo
reinicia; cinco toques que reaccionan en 2000ms ponen `dizzy` 3000ms y se ignoran hasta que termina; al acabar vuelve
el ánimo actual del servidor. Cada toque aceptado aprieta a Buddy 160ms (`poke`, `transform`, origen en los pies) y la
clase se quita sola con el `animationend` de esa figura. El override cambia solo `data-mood` (cuerpo, resplandor, GIF);
`--accent-real` / `body[data-real]` es el ánimo del servidor y `.bar`, `#plate` y `main` re-apuntan `--accent` a él,
así la píldora de ánimo, los contadores, la barra de XP y las filas de hilos no mienten con el color del toque. Con un
permiso (`ask`) la zona queda muda e inerte: un dedo grande que roza bajo los pies de Buddy no puede reasignarse a
Denegar (la zona termina 6px sobre la fila de botones). Un permiso que llega corta el ánimo de toque y su temporizador.

## Do's and Don'ts

### Do:
- **Do** meté todo texto que viene de la sesión en una burbuja de papel, con `textContent`, nunca como HTML.
- **Do** tiñé cualquier superficie nueva con `color-mix(in srgb, var(--accent) 16%, rgba(255,255,255,.04))`
  en vez de inventarle color.
- **Do** usá cápsulas (999px) para contar y burbujas (28px, con cola hacia Buddy) para hablar.
- **Do** animá solo `transform` y `opacity`; la Pi 4 no da para más.
- **Do** mantené los 600ms de armado en cualquier botón que decida algo.
- **Do** escribí la UI en español con vos (y su par en inglés, en la tabla I18N) y sin em dashes.
- **Do** probá cada pieza nueva en los dos modos: Buddy propio y `body.pack` con el manifest de bufo (`?demo=idle-pack`
  y `?demo=threads-pack` ya traen el pack puesto).
- **Do** dejá cada fila de hilo donde apareció: un hilo cambia de aspecto cuando cambia su estado, nunca de lugar.
- **Do** decí siempre que hay más filas de las que caben (barra de 16px y desvanecido de 16px) y no confíes en que
  una fila asome por casualidad; un estado que importa (terminó y espera) se marca con algo que no sea solo un color.
- **Do** dale 44px de alto, como mínimo, a cualquier cosa nueva que se toque.

### Don't:
- **Don't** líneas finas, rieles, hairlines ni bordes de 1px: lo que separa es tono o burbuja.
- **Don't** mayúsculas espaciadas ni letter-spacing positivo.
- **Don't** tarjetas con métricas ni dashboards: los contadores son píldoras en una fila, nada más.
- **Don't** fuentes web, CDN ni recurso externo alguno; CSP `default-src 'none'`.
- **Don't** sombras grises de elevación; si algo brilla, brilla con su color.
- **Don't** atenuar con `opacity` un hilo que terminó: se distingue por el tinte (tinta al 7%), no por apagarlo; su
  texto es el que se va a leer.
- **Don't** mostrar en la lista lo que dijo un hilo, ni dejar el detalle abierto sin que nadie lo toque: el texto de
  una sesión sale solo tras un toque y se cierra solo.
- **Don't** Clawd ni ningún mascot conocido; Buddy es propio.
