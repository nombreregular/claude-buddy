# Claude Buddy para Raspberry Pi

Reimplementación del **Claude Desktop Buddy** (protocolo Hardware Buddy, BLE) para una Raspberry Pi 4 con
pantalla táctil DSI de 7" (800x480), en BlueZ + Python. El firmware oficial corre en un M5StickC Plus (ESP32);
este proyecto hace lo mismo sobre Pi OS. Dueño: el autor del proyecto. Idioma del código: inglés; textos de UI y terminal: español
(vos) e inglés, elegibles con `--lang es|en` (ver `docs/features/i18n.md`). Sin em dashes en textos.
Documentación de producto en `README.md` (inglés) y `README.es.md`, con el detalle en `docs/` y `docs/es/`. Los datos
operativos personales (IPs, cuentas, equipos, rutas de instalación) no van en el repo: viven en notas privadas fuera de git.

## Estado (v0.3, octubre 2026)

- Fase 1, BLE: verificado en hardware. El Mac ve `Claude-XXXX`, empareja y conecta.
- Fases 2 y 3, pantalla: verificado con el Mac conectado el 17 oct 2026, incluido un permiso real (Bash) aprobado
  tocando la pantalla. Arranque solo verificado el 17 oct 2026 con dos reinicios reales: el servicio de usuario `claude-buddy`
  (linger) anuncia a los ~20 s, `~/.config/labwc/autostart` abre el kiosco a los ~30 s, el Mac reengancha solo y la
  pantalla sigue encendida 10 min después (sin swayidle; `wlopm`: `DSI-1 on`).
- Fase 4, packs: la página dibuja el pack activo (`view.character`; imágenes por `GET /character/<pack>/<archivo>`,
  solo las que nombra el manifest; colores `bg/text/textDim/body` del manifest) con el Buddy SVG propio de respaldo.
  Envío real verificado el 17 oct 2026 a las 22:57: carpeta arrastrada en la ventana Hardware Buddy, 41.751 bytes en
  10 archivos, 14 s por BLE, instalada atómicamente y la pantalla la tomó sin reiniciar nada.
- Personaje: **Mochi**, un Buddy en pixel art propio: una gota blandita con cachetes, sin cosas pegadas (el pixel art lo
  aprobó el dueño en la pantalla real el 17 oct 2026; Mochi lo eligió el 18 oct entre varias propuestas, pidiendo más ternura
  y menos accesorios). Lo genera `tools/make_pixel_buddy.py` (grilla 32x32 ×3 → GIF de 96 px, siete ánimos, manifest con
  `render: pixel` y `accents` por ánimo, extensiones nuestras) en `characters/buddy-pixel`; en el Pi está instalado como
  pack `buddy` y `tests/test_pixel_buddy.py` exige que la carpeta del repo sea idéntica a lo que dibuja el generador.
  `bufo` fue solo prueba de la cañería (sus GIF son de bufo.zone, no van bajo la MIT). Si se cambia el dibujo, rerun del
  generador y copiar la carpeta al Pi (la página recarga las imágenes sola cuando cambia `character.rev`, sin relanzar el kiosco).
- Fase 5, Hermes: `buddy_hermes.py` abre un segundo listener (`--hermes-bind IP`, puerto 8766, token propio por
  `--hermes-token-file` o `$BUDDY_HERMES_TOKEN`) que mete los pedidos de Hermes en el mismo `ApprovalBroker`; la pantalla
  los muestra como `hermes · <herramienta>` y Hermes recibe la decisión por long polling (`GET /approval/<id>?wait=30`,
  sin callbacks). Verificado el 17 oct 2026 a las 22:40: `curl` desde la Mac al Pi (puerto 8766, `0.0.0.0`), el pedido
  salió en la pantalla real como `hermes · terminal`, se aprobó tocando y el long poll de la Mac volvió con
  `decided/once`. Tailscale en el Pi desde el 17 oct 2026 (nodo `buddy`, `<tailscale-ip>`, cuenta `<tailscale-account>`); el servicio
  instalado lleva `--hermes-bind <tailscale-ip> --hermes-token-file ~/.config/claude-buddy/hermes.token`. El plugin de Hermes
  (`hermes-plugin/claude-buddy/`) está instalado en el Hermes host como `<hermes-user>`, habilitado y seleccionado
  (`security.approval.transport: buddy`, `transport_fallback: builtin`); `hermes plugins doctor` OK y el Hermes host alcanza el
  8766 del Pi por Tailscale. Token en `<hermes-home>/claude-buddy.token` (0600, lo copió el dueño a las
  23:32). **Verificado de punta a punta el 17 oct 2026, 23:42 y 23:44:** una sesión interactiva de Hermes (TUI bajo
  pseudo-TTY) pidió correr `mkdir -p /tmp/prueba-buddy && rm -rf /tmp/prueba-buddy`, el pedido salió en la pantalla
  como `hermes · terminal` con el comando y "delete in root path", se aprobó (primera vez con un toque real, segunda
  por loopback) y Hermes corrió el comando (código 0). Ojo: `hermes chat -q --oneshot` es "unattended" y deniega sin
  consultar transportes; la prueba real es la TUI interactiva o Discord. Gotcha: en el Hermes host el gateway lo supervisa una unidad de
  sistema (`<hermes-unit>.service`); `hermes gateway restart` dice que el supervisor no lo relanzó, pero systemd sí lo
  relanza solo (`systemctl is-active <hermes-unit>.service`). Habilitar el plugin no necesita reinicio.
  Gotcha de Tailscale: `tailscale up` hay que dejarlo esperando hasta que se autorice el enlace; cada `up` interrumpido
  (timeout, Ctrl-C) genera un enlace nuevo e invalida el anterior, y el nodo queda en `NeedsLogin`.
- Diseño: rediseño del 17 oct 2026 según referencias fijadas por el dueño (ver `PRODUCT.md` > Brand Commitments). El
  primer intento (riel técnico) fue rechazado; no volver a lo frío ni a dashboards.
- Contexto por tarea (18 oct 2026): con varias sesiones a la vez la pantalla ya no dice solo "3 trabajan": cada
  sesión de Claude Code en la Mac reporta por `tools/context_hook.py` → `POST /session` qué hace (proyecto,
  herramienta, resumen) y la burbuja lista las tareas vivas. Verificado con sesiones reales en la pantalla.
- Hilos y `said` (18 oct 2026): decisión del dueño, la lista deja de ser "carpetas": muestra QUÉ CONVERSACIONES están
  vivas (trabajan ahora, o terminaron su turno y esperan su respuesta; sin historial), con el título real de cada una,
  agrupadas como la barra lateral de Claude desktop ("Proyecto Ejemplo", "Buddy"...) y con las sesiones de terminal
  que desktop no conoce bajo "Terminal". Un hilo que otro lanzó cuelga de su padre si el padre está vivo. Tocar una
  fila abre lo que dijo (`said`, 300 caracteres) y Volver regresa a la lista. Hermes y Discord quedan fuera. Hechos,
  contrato y límites de privacidad en "Hechos de los hilos". Estado: construido y probado en la Mac (442 pruebas, la
  página en Chrome headless, el hook contra el listener real y bajo el Python 3.9 de macOS) y el hook nuevo ya corre en
  vivo en esta Mac. SIN desplegar al Pi y sin mirar en la pantalla real: falta `rsync` + `systemctl --user restart
  claude-buddy` (mientras tanto el daemon viejo ignora los campos nuevos) y probar con el dedo lo que no se puede desde
  la Mac: arrastre real contra clic en la lista y en el texto del detalle (un golpe en el texto no debe mover la lista),
  cuántas líneas caben con Nunito Sans (en la Mac se midió con otra fuente), el modo pack con el pack real, un permiso
  que llega con el detalle abierto (aterriza en el permiso con los 600 ms intactos) y el `Stop` de una sesión real de
  desktop (nunca se vio su stdin crudo, se infiere del código compartido con la terminal: mirarlo una sola vez, solo
  los nombres de las llaves). Hecho en esta Mac (18 oct 2026): los cuatro comandos de `context_hook.py` de `~/.claude/settings.json` ya llevan el
  `2>/dev/null; exit 0` del final (con respaldo en `settings.json.bak-buddy-guard`); falta el del `PermissionRequest` de
  `cli_hook.py`, que cambia el dueño o una sesión con su permiso, no el código del repo. Tampoco se vio con el dedo en el Pi la
  barra de 16 px ni el desvanecido de la lista (`mask-image` estático): en la Mac se miden y se miran en Chrome, pero falta
  comprobar que el scroll siga fluido en la Pi 4 y que el dedo agarre la barra (si Chromium no deja arrastrar el pulgar
  con el dedo, va un tirador propio en el borde derecho); si la lista no es fluida, se quita la línea `-webkit-mask-image`
  de `#feed-list.tasks` (la barra se queda y la prueba que la exige se ajusta).

## Hechos verificados con el Mac real (17 oct 2026)

- Claude desktop solo manda `prompt` para permisos aprobables desde un dispositivo: herramientas reales en sesiones en
  modo manual. En modo `auto` no llega nada. `AskUserQuestion` sí llega, pero con pista vacía, y aprobarlo desde el
  buddy descarta la pregunta sin respuesta: la página lo muestra como pregunta, con el contexto de la sesión y sin botones.
- Tras reiniciar el daemon (o el Pi) el Mac puede quedar conectado por BT sin volver a suscribirse a TX ni mandar
  latidos: `linked` se queda en false y la pantalla dice "Durmiendo". Lo que lo arregla es desconectarlo: el Mac
  reconecta solo y abre el canal. Desde el 17 oct 2026 lo hace el daemon (`buddy_bluez.py`, watchdog del peer callado):
  a un dispositivo del buddy (en `peers.json`) que está conectado pero no se suscribe ni escribe en
  `QUIET_PEER_TIMEOUT_S` (20 s desde el arranque o desde que conectó) le manda `org.bluez.Device1.Disconnect`, hasta
  `QUIET_PEER_MAX_KICKS` (3) veces por racha de silencio (el contador vuelve a cero cuando se suscribe, escribe o se va
  solo); un passkey en pantalla pausa el reloj. Nunca toca dispositivos ajenos al buddy ni clásicos (con `Class`).
  Verificado el 17 oct 2026 a las 23:32 con `systemctl --user restart claude-buddy`: Bluetooth listo a +1 s, Mac
  conectado y callado, desconectado a +22 s, reconectó solo a +30 s, notificaciones y `linked:true` a +32 s sin tocar
  nada. En el journal (servicio sin `-v`) se ve como "Dispositivo desconectado" y ~8 s después "Dispositivo conectado"
  + "Claude desktop activó las notificaciones"; la línea INFO "está conectado pero callado ... lo desconecto" solo sale
  con `-v`. A mano sigue sirviendo `bluetoothctl disconnect <mac>`; si tras 3 intentos sigue callado (WARNING "no
  insisto más"), Connect en Open Hardware Buddy. Pruebas: `tests/test_bluez.py`, sección "a desktop that is connected
  but never opens the channel" (7 pruebas con el bluetoothd falso; `Disconnect` del fake vive ahí como subclase).

## Hechos de los hilos (18 oct 2026, Mac real)

Un hilo es una conversación de Claude, de desktop o de terminal, que sigue viva. Lo que sigue es lo que hay en el
código y en los archivos de esta Mac; lo que falta verificar en el Pi está en Estado.

- **Dónde viven los datos.** Una sesión de desktop es un archivo
  `~/Library/Application Support/Claude/claude-code-sessions/<orgA>/<orgB>/local_<uuid>.json` con `sessionId`
  (`local_<uuid>`), `cliSessionId` (es el `session_id` que reciben los hooks), `title` y, en las que otra sesión
  lanzó, `spawnedFrom.sessionId`. Pesan ~0.8 MB porque llevan también los esquemas de los conectores: el hook lee el
  principio del archivo y el objeto `spawnedFrom` (cerca del final) y solo parsea el archivo entero si eso no alcanza.
  Los grupos de la barra lateral están en otro archivo, `claude_desktop_config.json`:
  `preferences.epitaxyPrefs["dframe-group-scopes"]["<orgA>/<orgB>"]` con `groups: [{id, name}]` y
  `assignments: {"code:local_<uuid>": "<id del grupo>"}`. **Ese archivo también guarda los ajustes de los
  conectores: se leen SOLO esas dos llaves, y nunca se guarda, loguea ni manda nada más de él.**
- **Qué grupo, qué padre, qué título.** El título es el de la sesión de desktop (una línea, 60 caracteres). El grupo es
  la asignación de la propia sesión; un hijo sin grupo propio muestra el del ancestro más cercano que lo tenga (así lo
  hace la barra lateral); una sesión de desktop que no está en ningún grupo es "Sesiones"; si el config falta, está
  roto o cambió de forma el hook no manda grupo y la pantalla conserva el que tenía (un error de lectura nunca mueve una
  fila). El padre es el hilo que la lanzó (`spawnedFrom`), nombrado por su id de hook, subiendo hasta 4 niveles. Una
  sesión de terminal que Claude desktop no conoce no manda título ni grupo: la pantalla pone la carpeta y el grupo
  "Terminal". El enlace es solo por el id del hook (`cliSessionId`; o `hostSessionId` de `~/.claude/sessions/<pid>.json`,
  la lista de procesos vivos de Claude Code, mientras desktop no escribió el suyo): nada se adivina.
- **Estado vivo.** Lo dan los hooks ya registrados en `~/.claude/settings.json`: PreToolUse y UserPromptSubmit = trabaja,
  Stop = terminó y espera tu respuesta, SessionEnd = se fue (SubagentStop se ignora). El protocolo Hardware Buddy solo trae
  totales. `Stop` no corre si interrumpís a Claude: un hilo interrumpido sigue como "trabaja" hasta que habla otra vez
  o vence a los 10 min.
- **Vida en el tablero** (`TaskBoard`, solo en memoria: reiniciar el daemon lo vacía hasta que cada sesión vuelva a
  hablar). Un hilo que trabaja y no dice nada en 10 min se va (`TASK_TTL_S` 600); uno que terminó y espera se queda
  30 min (`TASK_IDLE_TTL_S` 1800). El tablero guarda 12 (`MAX_TASKS`, se van los más viejos) y la pantalla muestra 8
  (`UIHub.MAX_TASKS_SHOWN`): trabajando primero, luego los más nuevos. La edad que muestra la fila es el tiempo desde que
  el hilo habló por última vez.
- **`POST /session`** (puerto de Hermes, mismo token, nada aquí aprueba nada):
  `{"id", "project", "tool", "what", "state": "working"|"idle"|"gone", "said", "title", "group", "parent"}` →
  `200 {"ok": true, "id", "state"}`. `id` (1 a 64 de `A-Za-z0-9_.-`) es el `session_id` del hook y es lo único
  obligatorio; `state` es `working` si falta. `project` (40), `tool` (40) y `what` (120) se cortan a una línea;
  `project` vacío conserva el anterior, y un `idle` sin `tool` ni `what` conserva los del turno anterior. `said` (300,
  conserva saltos de párrafo), `title` (60), `group` (40) y `parent` (un id de ese mismo patrón) son strings
  opcionales con una sola regla: llave ausente = el tablero se queda con lo que tiene, `""` = borra, texto =
  reemplaza. Un valor que no es string, o un `parent` que no es un id, es 400; un hilo nunca es su propio padre;
  `gone` lo saca del tablero con todos sus campos. Cuerpos de hasta 4096 bytes (el hook se queda en 3900: si no cabe
  suelta primero `said`, luego acorta el título a 24, quita `parent` y acorta el grupo a 20; lo esencial nunca se toca).
  Lo que llega en estos campos no se escribe en ningún log.
- **`view.tasks`** (lo que ve la página): una lista de a lo sumo 8 objetos
  `{id, project, title?, group?, parent?, tool, what, state, age, said?}`; `title`, `group`, `parent` y `said` aparecen
  solo si no están vacíos (una sesión de terminal sigue viéndose como siempre). `state` es `working` o `idle`; `gone`
  nunca aparece.
- **La página** agrupa por `group` (vacío = "Terminal"), pone `title || project || "sesión"` como nombre de la fila y
  deja cada hilo y cada grupo en el lugar en que se vio por primera vez (el orden no salta cuando cambian los demás).
  Todo lo que se indexa por id o por nombre de grupo es un `Map`: `constructor` y `__proto__` son ids y grupos válidos.
  Una fila que terminó lleva, además del tinte, una marca de visto antes de la edad (con título la fila no dice
  "terminó"). La lista de 219 px siempre dibuja una barra de 16 px a la derecha mientras haya más de lo que cabe y su
  ancho está reservado, así las filas miden 384 px con 2 hilos o con 8; la última fila visible se desvanece en 16 px
  hacia el borde de la burbuja, así ninguna termina plana contra él. Diseño: `DESIGN.md` > Components > Hilos.
- **Qué viaja y qué lo apaga (privacidad).** Cada reporte lleva carpeta, herramienta y resumen de una línea (lo de
  siempre) más el título de la conversación, el nombre de su grupo, el id del padre y, en `Stop`, `said`: hasta 300
  caracteres de la respuesta con la que terminó el turno (los 200 del principio y los 97 del final, con el markdown
  aplanado, los bloques de código reemplazados por "(código)" y lo que parece un secreto por "[oculto]"). Ese filtro es
  de mejor esfuerzo, no una frontera (también se dispara con ids largos con forma de UUID). Va por HTTP plano al puerto
  de Hermes del Pi, con el token; por eso conviene que `--hermes-bind` sea la IP de Tailscale. Sin
  `last_assistant_message` en la entrada del hook, el hook lee el final del transcript de la sesión (solo un `.jsonl`
  tuyo); si no logra leerlo no manda `said` y la pantalla conserva el anterior, y manda `""` solo cuando el último mensaje
  verificablemente no tiene texto.
  - **Guardia del destino:** `said` solo sale si quien contesta es la propia Mac (loopback) o una IP de la tailnet
    (100.64.0.0/10 y fd7a:115c:a1e0::/48). Ojo: 100.64.0.0/10 es también el CGNAT de los proveedores y de otras VPN, así que
    prueba "no es la LAN de la casa", no "es Tailscale".
  - **Interruptores** (variables de entorno del hook, que corre en cada sesión): `CLAUDE_BUDDY_CONTEXT=off` apaga el
    hook entero; `CLAUDE_BUDDY_THREADS=off` no manda título, grupo ni padre y no lee nada de Claude desktop;
    `CLAUDE_BUDDY_SAID=off` no manda `said` ni abre el transcript; `CLAUDE_BUDDY_SAID_EXCLUDE=carpeta1,carpeta2` deja
    sin `said` a las sesiones de esas carpetas (el hook es global: las sesiones programadas y las headless también lo
    mandan, y esta lista es para ellas). `CLAUDE_BUDDY_SAID=off` en el entorno del DAEMON además le hace descartar `said`
    aunque un cliente lo mande (se lee una vez, al arrancar).
  - **El config de Claude desktop** se lee solo para `groups` y `assignments` de la carpeta de la sesión. El hook
    solo escribe dos archivos en `~/.cache/claude-buddy/` (carpeta 0700, archivos 0600, nuestros; si no son privados
    se ignoran). `threads.json`: id del hook, id de desktop, título y padre, que se vuelven a mirar a los 30 s o ante un
    id desconocido (uno que ninguna sesión reclama no se busca otra vez por 10 s); no guarda nombres de grupo ni nada del
    config. `down_until`: una hora (número) hasta la cual no se intenta hablar con el buddy, ver "Costo".
  - **Costo:** el hook se rinde rápido y nunca frena la sesión: sale con 0 y en silencio pase lo que pase. 1.5 s para
    conectar, 2 s esperando respuesta y, pase lo que pase con la red (un nombre que no resuelve, una respuesta que llega
    a gotas), un temporizador (`DEADLINE_S` 2.5, un hilo que hace `os._exit(0)`) lo termina a los 2.5 s; el `timeout` de
    Claude Code es 5. Tras un intento fallido (rechazado, sin ruta, vencido) el buddy se deja en paz 30 s (`DOWN_S`, el
    archivo `down_until`): `PreToolUse` y `UserPromptSubmit` ni siquiera lo intentan ni buscan el hilo, así que una Pi
    apagada o fuera de la tailnet le cuesta a la sesión una espera lenta cada 30 s y no una por herramienta. `Stop` y
    `SessionEnd` siempre lo intentan (un "terminó" perdido dejaría al hilo en "trabaja") y renuevan la marca si fallan; un
    intento que contesta, con el código HTTP que sea, la borra. Con la caché tibia no abre ningún archivo de sesión de
    desktop: solo la caché y el config (el grupo se lee en cada llamada, así que un hilo movido de grupo se ve al
    instante; un título cambiado, a los 30 s).
  - Variables solo para pruebas o para mover carpetas (tan de confianza como `CLAUDE_BUDDY_URL`, que ya decide adónde
    va el token): `CLAUDE_BUDDY_DESKTOP_DIR`, `CLAUDE_BUDDY_CACHE_DIR`, `CLAUDE_CONFIG_DIR`.

## Censo de corridas (18 oct 2026)

El hook solo habla cuando una sesión llama una herramienta: un `claude -p` callado, o un script que no es una sesión,
no aparecía en la pantalla. `tools/census.py` mira la tabla de procesos de la Mac cada 15 s y manda una foto a
`POST /census` (mismo puerto 8766, mismo token): la pantalla la muestra como el grupo **Corridas**.

- **Qué es una corrida**: un proceso que es TODO esto: el programa se llama `claude`; la palabra que sigue es una opción;
  `-p` o `--print` está entre las primeras 80 palabras; ahí no hay `--sdk-url`; nada arriba vive en Claude.app ni es otra
  corrida (un `claude -p` anidado es de la de afuera); lleva más de `min_age_s` (20 s). Los envoltorios (`bash -c ... claude -p`,
  `caffeinate`, `ssh`) nunca son corridas, su hijo sí. Los trabajos (scripts) solo salen si `census.json` los nombra.
- **Qué sale de la Mac**: nombre (la carpeta de la corrida, sin prefijo), segundos corriendo, segundos desde que su
  transcript cambió (`idle`), el `session` del hook (para mostrar una corrida una sola vez) y el nombre de la carpeta.
  Nunca una línea de comandos, argumento, entorno, ruta, prompt ni salida: se leen en memoria para clasificar y se
  tiran. El token solo viaja a esta Mac, la tailnet o loopback; si el nombre lleva a otra dirección el censo lo retiene.
- **Contrato** (`POST /census`): `{v:1, source, seq, ttl, more, rows[≤8]}`; cada fila `{id run-|job-…, name, group,
  state:"working", elapsed, idle?, session?, project?}`. Tipo equivocado = 400; número fuera de rango = se recorta, nunca
  se rechaza la foto. La foto reemplaza a la anterior de su `source` y caduca sola (`ttl`, 4 × el intervalo): si el censo
  muere, el grupo se va en un minuto. Tiene su propio cubo de pedidos (60 por minuto) para no ahogar a los hooks. Como
  `/session`, **nunca aprueba nada**.
- **En el daemon** (`TaskBoard.replace_census`): las filas del censo viven fuera de `MAX_TASKS`; una corrida y su hilo
  del hook se muestran como UNA fila (por `session`, o por la carpeta solo si la corrida no dice su sesión); si la corrida
  desaparece de una foto completa, su fila del hook se borra al instante; el `SessionEnd` del hook oculta la corrida 30 s.
  La vista suma `working_total`, `tasks_total`, `census_total` y `tasks_more` sobre TODO el tablero antes de recortar (la
  pantalla dibuja 8 filas, hasta 5 de Corridas y al menos 3 de hilos).
- **Instalar** (opcional, nada corre sin esto): `tools/census_agent.sh install` escribe el LaunchAgent
  `com.claude-buddy.census` (`/usr/bin/python3 -I -u tools/census.py run`, `KeepAlive` solo si se cae) y lo carga; `-n`
  muestra el plan sin tocar nada; `status` no toca la red (con `--ping` hace un `GET /healthz`); `uninstall` lo quita.
  El repo no puede estar en Documents, Desktop, Downloads ni iCloud (macOS los protege). Su config propia, fuera de git,
  es `~/.config/claude-buddy/census.json` (modelo en `tools/census.example.json`); sin ella solo ve `claude -p`.
- **Contra un daemon viejo**: `/census` da 404 y el censo espera 5 min y lo vuelve a intentar; el orden de despliegue no importa.
- **Límites conocidos**: el registro `~/.claude/sessions/<pid>.json` es de Claude Code y no está documentado (si cambia,
  se pierde el `session` exacto y queda el cruce por carpeta); una corrida de pura palabra puede confundir la ventana de 80
  palabras (`claude -p "explicá --sdk-url"`); un `claude -p` que lanza una conversación de desktop como herramienta no se ve
  a propósito; el cruce por carpeta puede absorber un hilo ajeno si la corrida no dijo su sesión.

## Toques en Buddy (18 oct 2026)

Pedido del dueño ("si lo toco no pasa nada"). Todo vive en la página (bloque `Buddy al tacto` de `ui/index.html`): sin
servidor, sin guardar nada, sin XP, sin texto. Solo cambia cómo se ve Buddy un rato.

- **Zona** (`#petzone`): un área transparente de 192x196 en x74/y140, encima de la figura en los dos modos (SVG y pack).
  Mide más de 120x120, queda a 34 px de la placa y a 78 px de la columna derecha. Se usa `click`: un arrastre no es un toque.
- **Reglas**: dormido, el primer toque lo despierta (`idle` 2500 ms; los toques de esa ventana valen como corazones).
  Despierto (cualquier ánimo menos `attention`), un toque pone `heart` 2500 ms y otro toque reinicia el plazo. Cinco toques
  que reaccionan en 2000 ms (ventana deslizante, inclusive) ponen `dizzy` 3000 ms y los toques se ignoran hasta que termina.
  Al acabar vuelve el ánimo ACTUAL del servidor. Cada toque aceptado aprieta a Buddy 160 ms (`poke`, solo `transform`).
- **Lo que no cambia**: el override toca solo `data-mood` (cuerpo, resplandor y el GIF del pack, con el mismo respaldo si
  falta el estado). La píldora de ánimo, el titular y los contadores dicen siempre el ánimo REAL (`--accent-real` /
  `body[data-real]`: `.bar`, `#plate` y `main` re-apuntan `--accent` al del servidor; Buddy, `#glow` y `#ping` siguen al ánimo mostrado).
- **Con un permiso en pantalla** (`ask`) la zona sigue ahí pero muda e inerte: así un dedo grande que roza justo debajo de
  los pies de Buddy no se reasigna a Denegar. Un permiso que llega termina cualquier ánimo de toque y su temporizador al
  instante; los 600 ms de armado de los botones no se tocan. `attention` en la pantalla principal tampoco reacciona.
- **Eco del toque** (`#ping`): un solo círculo de 56 px (acento al 25%) que crece de .6 a 1.15 y se desvanece en 140 ms
  (`transform` y `opacity`) donde tocaste en una parte muerta, también en las partes muertas de un permiso, y en los toques
  a Buddy que él rechaza (`attention`, enfriamiento del mareo); nunca en botones, filas, `#feed-list`, `#tv-said`, `.hint`,
  `.reason` ni sobre un permiso en la zona de Buddy. Un elemento táctil o con scroll nuevo se agrega a `LIVE_TOUCH`.
- **Refresco del pack**: `UIHub.character()` suma `rev` (12 hex, sha1 del manifest y del nombre, tamaño y `mtime_ns` de cada
  archivo que lista, releído en cada llamada: un GIF reemplazado con el mismo nombre lo cambia). La página agrega `?v=<rev>`
  a cada imagen; un pack ya en pantalla se reemplaza recién cuando todas las nuevas cargaron o fallaron (sin cuadro vacío) y
  mientras tanto un cambio de ánimo elige entre las viejas. Con un daemon sin `rev` todo sigue como antes. `rev` huella
  nombre, tamaño y mtime, no el contenido: un archivo del mismo tamaño y mtime pasa inadvertido.
- **Sin probar con el dedo en el Pi**: que un toque real llegue como `click` a la zona; si el eco al 25% se ve sobre el
  fondo oscuro (sobre el papel se ve bien); si el stack Wayland/labwc informa radio de contacto (el reasignado a Denegar se
  probó con CDP en Chrome de escritorio); el reflujo forzado por toque en una Pi 4.

## Modo noche (18 oct 2026)

Pedido del dueño: "pantalla tenue de noche, un toque la despierta y los permisos la despiertan". El Pi tiene backlight real
(`/sys/class/backlight/10-0045`, 0 a 255, `rw-rw-r-- root video`; el daemon corre como el usuario del servicio, que está en `video`), y un velo
negro sobre un LCD deja la luz de fondo prendida: lo que de verdad atenúa es el backlight; el velo de la página es el respaldo.

- **Ventana**: `--night HH:MM-HH:MM` (por defecto 23:00-07:00) o `--night off`. Hora local de la Mac (`core.local_time()`, la
  misma del reloj de la pantalla) y, si no la hay, la del propio Pi. Inicio inclusivo, fin exclusivo, cruza la medianoche,
  inicio = fin es siempre noche. Un valor inválido sale con error de argparse (código 2).
- **Despierta** 30 s (`AWAKE_S`) después de `UIHub.wake()`, que es el único camino: un toque en la pantalla oscura (`POST /wake`),
  un permiso pendiente (más 30 s después del último que se resuelve o vence), un código de emparejar en pantalla (y 30 s
  tras irse) y, de noche y despierta, cualquier toque sobre la pantalla (la página avisa con el mismo `POST /wake`, a lo sumo
  uno cada 5 s, y nunca bajo un permiso ni con la pantalla oscura). Sin ese aviso se apagaba a los 30 s del primer toque aunque
  siguieras leyendo un hilo. Un aviso de Hermes con `wake:true` despierta por el mismo `wake()` (ver "Avisos de Hermes").
- **Quién manda**: el daemon. La vista trae `night` (dentro de la ventana, despierta o no), `dim` (noche y nadie la despertó) y
  `backlight` (el daemon controla el panel de verdad); los tres siempre presentes y booleanos. Cuando se acaba el despertar o
  empieza o termina la ventana, el daemon republica solo (un único `call_later`, reprogramado en cada cambio y cancelado en `close()`).
- **`buddy_backlight.py`**: busca el primer panel escribible bajo `/sys/class/backlight/*` (o `--backlight auto|off|RUTA`).
  Día = `max_brightness`, noche = `--night-level N` (por defecto 12, entre 4 y el máximo: el piso 4 evita una pantalla negra).
  Escribe solo cuando el nivel cambia. Un OSError lo apaga para atenuar (la página oscurece sola) y se registra una línea; si
  era la escritura de vuelta a DÍA, la reintenta una vez por segundo hasta 60 veces para no dejar el panel tenue. Un panel
  cuya noche no queda por debajo del día (on/off por GPIO) no se toma. El nivel de día es el máximo, no "el que había al
  arrancar": un cierre en falso mientras estaba tenue no puede hacer que el próximo arranque lo tome por normal. Con
  `--night off`, `--no-ui` o el puerto de la pantalla ocupado, el modo noche y el backlight se apagan por completo.
- **Restauración**: `hub.close()` (vuelve a DÍA) es lo primero del `finally` interno de `run_daemon`, antes de `await
  transport.stop()`, y otra vez en el externo como red. Cubre SIGTERM (systemd), SIGINT, la tecla q, SIGHUP (una sesión de
  ssh que se cae; no si se arrancó con `nohup`), KeyboardInterrupt, cancelación y un arranque de Bluetooth que falla. SIGKILL,
  SIGQUIT y un corte de luz no restauran: el panel queda tenue hasta que systemd reinicia el daemon, y el primer sync escribe el nivel correcto.
- **Página** (`#dim`, z-index 80): una capa del tamaño del escenario, inerte salvo con `view.dim === true`. Con `backlight` solo
  atrapa el toque; sin él además oscurece al 78% (600 ms de entrada, 300 de salida, solo opacidad). El toque que despierta lo
  consume `#dim` en `click` (no en pointerdown), con `stopPropagation`: no abre una fila, no aprieta Volver, no mueve a Buddy, no
  deja eco ni toca un botón de permiso; avisa a `POST /wake` (sin esperar respuesta) y despierta también localmente 30 s por si
  el aviso se pierde. Tras despertar, `#dim` sigue atajando toques 350 ms, y un dedo que apoya dentro de esa guarda la alarga
  hasta 600 ms desde que apoyó (una sola vez). Un permiso (`ask`) nunca queda tapado ni oscuro, diga lo que diga la bandera
  (inerte en JS y en CSS); sus 600 ms de armado no cambian. Demos: `?demo=night`, `?demo=night-overlay`, `?demo=night-ask`.
- **`POST /wake`** (servidor de la pantalla, solo 127.0.0.1): mismo token `X-Buddy-Token`, Host y Origin que `/decide`, su propio
  límite (20 por 10 s, después del token), responde `{"ok":true,"dim":bool}`. Solo despierta: no aprueba ni cambia nada más.
- **Registro**: las líneas "noche: pantalla tenue", "noche: despierta (toque|permiso|emparejar)" y "noche: terminó, pantalla
  normal" salen por el `Terminal` y llegan al journal sin `-v`; la línea "Modo noche ..." al arrancar dice si encontró el panel.
  Tras un `rsync` y `systemctl --user restart claude-buddy`, buscarla en `journalctl --user-unit claude-buddy`: si dice "sin luz
  de pantalla que mover", el servicio no tiene el grupo `video` y solo hay velo.
- **No correr la suite en el Pi de noche**: los tests con el daemon real (test_cli, test_hermes) no pasan `--backlight off` y
  atenuarían el panel real un rato (se restaura al salir).
- **Sin probar con el dedo en el Pi**: el `click` sobre la capa transparente, el costo de compositor del velo, que 12/255 se
  vea bien en ese panel (`--night-level` lo ajusta), y el grupo `video` del servicio de usuario.

## Avisos de Hermes (18 oct 2026)

Hermes (o cualquier script) deja un mensaje corto en la pantalla: "la tanda terminó", "llegó un correo". Sale como el grupo
**Avisos** arriba de la lista, se cierra con un toque ("Listo"), caduca solo y, si se pide, despierta la pantalla de noche.
Viven en memoria: un reinicio del daemon los borra (y la página los olvida al recargarse).

- **`POST /notice`** (listener de Hermes, puerto 8766, mismo token Bearer, Host/Origin y bloqueo que `/session` y `/census`).
  Cuerpo JSON de hasta 4096 B: `title` (obligatorio, limpio, 1 a 60), `body` (opcional, se conservan los saltos, hasta 4
  líneas y 240 caracteres), `kind` (`info` por defecto, `ok`, `warn`; otro valor = 400), `ttl` (número recortado a 10..3600,
  por defecto 300; NaN, infinito, booleano o no número = 400), `wake` (booleano, por defecto falso), `id` (`[A-Za-z0-9_.:-]{1,64}`;
  sin id el daemon pone `n-<contador>`) y `source` (hasta 24, por defecto `hermes`). Tipo o forma equivocados = 400; claves
  desconocidas se ignoran; 404 si el daemon no tiene tablero. Responde `{"ok":true,"id":...,"active":n}`.
  El mismo `id` REEMPLAZA al aviso (el tiempo vuelve a empezar; `rev` sube solo si cambió título, cuerpo o tipo). Como mucho
  3 activos: un cuarto saca al más viejo (estado `evicted`). Cubo de pedidos propio (`NOTICE_RATE_MAX` 30 por 60 s): una
  inundación de avisos no ahoga a los hooks ni al revés.
- **`GET /notice/<id>`** responde `{"id":...,"state":"shown|dismissed|expired|withdrawn|evicted|unknown"}` (los terminados se
  recuerdan 1 h, hasta 50); **`DELETE /notice/<id>`** lo retira (idempotente; un aviso ya terminado conserva su estado).
- **`POST /dismiss`** en el servidor de la pantalla (solo 127.0.0.1): `{"id": str}`, `X-Buddy-Token`, Host y Origin como `/decide`
  y `/wake`, JSON obligatorio (415), límite propio de 20 por 10 s después del token; solo descarta (no aprueba ni despierta).
- **Vista**: `view.notices` (hasta 3, el más nuevo primero, `{id,title,body?,kind,source,age,rev}`, siempre presente); cambia
  al llegar, descartarse o retirarse sin otro evento, y al caducar en el siguiente refresco de 1 Hz.
- **De noche**: `wake:true` llama a `UIHub.wake("aviso")` (el mismo camino único de los toques y los permisos): enciende la pantalla
  30 s y el journal dice "noche: despierta (aviso)". Sin `wake` el aviso espera, visible la próxima vez que se despierte. De día
  no cambia nada visible. Un permiso pendiente sigue ganando la pantalla. **Un envío con `wake:true` despierta en cada POST**,
  también si el aviso no cambió, a propósito (así se puede escalar uno que esperaba): quien reenvía cada menos de 30 s con
  `--wake` mantiene el panel prendido; solo lo acotan el token y el cubo de 30 por minuto.
- **Página** (bloque `avisos` de `ui/index.html`): "Avisos · N" arriba de Corridas y de los grupos de hilos, filas de 44 px con
  campana, título y edad; el tinte sigue al tipo (`info` = el acento, `ok` = verde al 34%, `warn` = ámbar al 38%; 22% en modo
  pack, más una marca de visto o de exclamación que no depende del color). Sin hilos el titular dice "Un aviso" / "N avisos".
  Un toque abre el detalle (`#task-view`): título, "Aviso · de <source> · hace <edad>", el cuerpo sin comillas, y la cabecera
  lleva título, **Listo** (verde, 44 px, `POST /dismiss`, optimista, vuelve si falla) y **Volver** al mismo lugar que en un hilo
  (así un toque en el hábito de Volver nunca descarta un aviso). Mismas guardas que el detalle de un hilo (350 ms, cierre a
  los 25 s, un permiso gana). Un aviso NUEVO o CAMBIADO (id nunca visto o `rev` arriba) pone a Buddy en `attention` 4000 ms por
  la misma maquinaria del toque (`setTouch`): nunca en la primera vista tras cargar, con la pantalla oscura, bajo un permiso ni
  bajo el código de emparejar (ahí no se marca como visto: se anuncia al volver la lista). Orden de pantalla: emparejar, permiso,
  aviso vivo (también con la Mac sin enlazar o Bluetooth caído: la píldora roja sigue diciendo la falla), caída, espera.
  Demos: `?demo=notices`, `notices-pack`, `notices-only`.
- **`tools/notify.py`** es lo que Hermes ejecuta como comando (biblioteca estándar, Python 3.9, no importa nada del repo; corre
  en la Mac, el Pi o el Hermes host):

  ```bash
  python3 tools/notify.py --title 'La tanda terminó' --body '37 de 40 pasaron' --kind ok --ttl 600 --id tanda-21 --wake
  echo 'detalle largo' | python3 tools/notify.py --title 'Aviso' --body -     # el stdin tiene que cerrarse
  python3 tools/notify.py --status tanda-21      # shown, dismissed, expired, withdrawn, evicted o unknown
  python3 tools/notify.py --withdraw tanda-21
  ```

  `CLAUDE_BUDDY_URL` (por defecto `http://buddy:8766`; en el Hermes host, la IP de Tailscale a la que se ata el listener, p. ej.
  `http://<tailscale-ip>:8766`: el daemon solo acepta esa IP y el nombre de la propia máquina, así que un nombre MagicDNS
  `buddy.<tailnet>.ts.net` da 403 aunque la regla de la URL lo deje pasar) y `CLAUDE_BUDDY_TOKEN_FILE` (por defecto
  `~/.config/claude-buddy/hermes.token`; regular, tuyo, 0600, 16+ caracteres; en el Hermes host es el de `~/.hermes/claude-buddy.token`
  del plugin). Salida 0 si el buddy contestó (su JSON en stdout), 1 si no se pudo entregar (una frase fija en stderr con el nombre
  de la excepción o el estado HTTP; nunca el token ni el texto), 2 por mal uso. Los 3 s de tope cubren stdin, la resolución del
  nombre, la conexión y la respuesta; un solo intento. **No reintentar tras un 403**: cinco tokens malos en un minuto bloquean a
  ese cliente en todas las rutas, hooks incluidos. Con curl el token queda en `ps`: por eso se prefiere el script.
  Limpia como el daemon (los caracteres no imprimibles pasan a espacio; bidi, espacio de ancho cero y caracteres de etiqueta se
  quitan, salvo ZWJ/ZWNJ entre dos visibles, así los emoji compuestos quedan enteros) y corta el cuerpo en el mismo orden.
  `--insecure-url-ok` levanta la regla de la URL y también la revisión de la dirección a la que lleva el nombre.
- **Instalado en un Hermes host (ejemplo, probado el 19 oct 2026 con el OK del dueño)**: `<hermes-home>/.local/bin/buddy-notify.py` (copia
  exacta de `tools/notify.py`; hay que volver a copiarla si cambia) y `<hermes-home>/.local/bin/buddy-notify` (un `sh` de 4 líneas que
  fija `CLAUDE_BUDDY_URL=http://<tailscale-ip>:8766` y `CLAUDE_BUDDY_TOKEN_FILE=<hermes-home>/claude-buddy.token`, el token que ya usa
  el plugin de aprobaciones, y ejecuta `python3 -I buddy-notify.py "$@"`), dueños `<hermes-user>`, más el skill de Hermes
  `<hermes-home>/skills/buddy-pantalla/SKILL.md` (copia de `hermes-plugin/skills/buddy-pantalla/SKILL.md`: cuándo avisar,
  `--wake` solo para lo que no puede esperar, ids estables, qué hacer con 403/429). No se tocó `config.yaml`, `.env`, `MEMORY.md`
  ni ningún servicio. Probado: `sudo -u <hermes-user> buddy-notify ...` desde el Hermes host llegó al Pi (sin `--wake`), `--status` lo
  vio desde la Mac y se retiró. Sin probar: una sesión de Hermes usando el skill (se carga al abrir una sesión nueva, `/new`) y que su
  política de aprobaciones deje correr `buddy-notify` sin pedir permiso cada vez (`command_allowlist` está vacío; añadirlo es
  decisión del dueño). Para deshacerlo: borrar los dos archivos de `<hermes-home>/.local/bin/` y la carpeta
  `<hermes-home>/skills/buddy-pantalla`. Los comandos exactos de esta instalación están en las notas privadas, fuera del repo.
- **Privacidad**: el texto de un aviso (título, cuerpo, fuente, ni siquiera el id) no se escribe en ningún registro ni en un
  error; el daemon solo registra "aviso: <tipo>, N activos". La página lo pone con `textContent`. `/notice` y `/dismiss`
  nunca aprueban ni deniegan nada.
- **Sin probar**: un toque real en una fila de aviso y en Listo; los tramos de título con Nunito Sans (el de 19 px y hasta cinco
  líneas); la animación de atención con el pack real y de noche; que el verde de Listo se lea con el brillo de noche; y la
  cadena entera con `--wake` de noche (desde el Hermes host como `<hermes-user>` y desde la Mac ya llegaron avisos reales, sin `--wake`, y se
  retiraron; el despertar solo se probó con pruebas en loopback y con `POST /wake` en el panel real).

## Closet de disfraces (19 oct 2026, opcional)

Pedido del dueño: que Buddy use disfraces al azar, que "se los haga él" y tenga un closet de donde los saca; opcional y sin nada
personal en git. Los disfraces van **horneados en los GIF** (un velo en la página se desfasaría del cuerpo que rebota); el
closet vive FUERA de git; el daemon sigue siendo solo biblioteca estándar y nunca importa Pillow.

- **Hacerlos** (en la Mac, hace falta Pillow): `python3 tools/make_closet.py ~/closet-buddy --count 8 --sheet closet.png`
  (`--seed S`, `--base-name buddy`, `--list`). Imprime "Buddy se hizo: gorro de fiesta lila" por cada uno y escribe
  `CARPETA/<id>/` (un pack completo: `manifest.json` + los 9 GIF, ~60 KB) con `costume: {name, template, color, base}` en el
  manifest. Cada corrida agrega otros distintos (la semilla por defecto es cuántos ya hay); cuando están los 56 lo dice y sale con 0.
  `--sheet` dibuja todos los disfraces en los 7 ánimos para mirarlos. Sin Pillow: mensaje en español y salida 1.
- **La biblioteca** (`tools/closet_costumes.py`): 8 moldes (gorro de fiesta, corona, gorro de lana, sombrero de mago, gorro de
  chef, moño, flor, orejas de gato) x 7 colores (coral, menta, lila, rosa, lima, cielo, sol) = 56 looks; id `<molde>-<color>`;
  UNA pieza por look, contorno de tinta de 1 px (se lee sobre los 7 colores de cuerpo), centrada en la cabeza de cada cuadro y
  siguiendo el rebote y la inclinación del mareo; nunca tapa los extras (zetas, corazones, chispas, gotas). Para agregar un molde:
  un `Template(id, name_es, rows, sink)` más en `TEMPLATES`. El generador (`make_pixel_buddy.py`) tiene un gancho opcional
  `COSTUME` (se llama entre la cara y los extras); sin él el pack Mochi de `characters/buddy-pixel` sale byte por byte igual.
- **Al Pi**: `rsync -a ~/closet-buddy/ <user>@buddy.local:.local/state/claude-buddy/closet/`; un disfraz nuevo es otra carpeta.
- **Prenderlo** (por defecto apagado): `--closet off|auto|CARPETA` (`auto` = `<state-dir>/closet`) y `--closet-every HORAS` (0.25
  a 168, por defecto 3; 0 = elige uno solo al arrancar). En el servicio del Pi: agregar los flags al `ExecStart` y `systemctl
  --user restart claude-buddy`. Con `--closet off` no se escanea nada, no hay temporizador y `view.character` queda idéntico.
  Con `--no-ui` o el puerto de la pantalla ocupado el closet se apaga solo.
- **Cómo se porta**: `buddy_closet.py` revisa el closet (carpeta con `manifest.json` de hasta 64 KB, `costume` con `name` de
  hasta 40 y `base`, `states` con archivos regulares de hasta 256 KB, nombre de carpeta `[a-z0-9-]` de hasta 63, sin enlaces
  simbólicos, a lo sumo 64; lo demás se descarta y se cuenta una vez por tipo en el registro) y elige al azar entre los
  disfraces y "sin disfraz", nunca el mismo dos veces seguidas. Se elige al arrancar y cada H horas con UN temporizador
  (cancelado al cerrar); cada elección relee la carpeta, así que un disfraz copiado después entra en el sorteo. `character()`
  devuelve el pack del disfraz (nombre = id, más `costume` con el nombre en español) y el `rev` hace que la página cambie las
  imágenes sin cuadro vacío; `/character/<id>/<archivo>` sirve solo lo que lista su manifest. **Nunca cambia `state.character`**:
  es un override de la vista, y el disfraz solo se usa mientras el pack activo se llame como su `costume.base` (el del Pi se
  llama `buddy`): mantené el closet homogéneo. Un closet que desaparece o se corrompe en marcha vuelve al pack de abajo.
- **Registro** (sin -v): "Closet: N disfraces, cambia cada H h", "Closet: descarté N carpetas (tipos)", "Buddy se puso <nombre>",
  "Buddy se sacó el disfraz"; nunca contenido de archivos.
- **Probado en el Pi real (19 oct)**: con `--closet auto --closet-every 0.25` en un drop-in de systemd con auto-revert, Mochi salió
  con la flor sol en la pantalla. Sin ver con el dedo; las pruebas de `test_cli.py` del closet necesitan dbus (se saltan en la Mac).

## Idiomas (19 oct 2026)

La pantalla habla español (por defecto) e inglés. `--lang es|en` (o `$BUDDY_LANG` si falta el flag; un valor inválido sale con
código 2). El daemon pone `lang` en la vista (siempre presente) y la página lo usa; en las demos, `&lang=en`. Un idioma
desconocido cae en `es`.

- **Página**: una tabla `I18N = {es, en}` en `ui/index.html` (unas 95 claves por idioma) y `t(clave, params)` con `{marcadores}`;
  los plurales son claves `.one` / `.other`; una clave que falta se ve como `⟦clave⟧`. El texto estático lleva `data-i18n` y entra
  por `textContent` (nunca `innerHTML`). Para agregar un idioma: una columna nueva en la tabla, en `buddy_i18n.py` y en
  `LANGS`; `tests/test_i18n.py` exige las mismas claves y marcadores en todos, que no sobre ninguna y que cada demo se vea sin
  desborde a 800x480.
- **`buddy_i18n.py`** (solo stdlib): los textos que el daemon manda a la pantalla (hoy las razones de Bluetooth caído vía
  `ble_reason`). `buddy_bluez.py` todavía emite esas razones en español libre y la vista las reconoce por su plantilla; lo limpio
  sería emitir códigos.
- **Metadato**: la página servida lleva `<meta name="buddy-lang" content="__BUDDY_LANG__">` que el servidor rellena como el token,
  así "Conectando…" y el velo de sin conexión salen en el idioma antes del primer estado.
- **Sigue en español** (fuera de alcance por ahora): las líneas de la terminal y del journal, la ayuda de argparse, `tools/*`
  (frases de `notify.py`, `census_agent.sh`, `make_closet.py`), los textos del skill `buddy-pantalla`, y en `tools/cli_hook.py` las
  reglas de "Siempre" y la pista "en <carpeta>" que SÍ se ven en pantalla; `tools/context_hook.py` marca `(código)` y `[oculto]`.
- Las demos usan nombres inventados en los dos idiomas (Acme Studio, web-tienda, api-pagos, Ana), nunca datos reales.

## Operar el Pi por SSH (gotchas)

- Nunca `pkill -f <patrón>` dentro de un `ssh '...'` si el patrón aparece literal en ese mismo comando: mata la
  sesión (exit 255). Usar `pkill -f "[c]laude-buddy-kiosk"` y, al arrancar el daemon en la misma línea, romper el
  literal (`bud""dy.py`).
- Chromium headless en el Pi (pruebas, capturas) necesita `--password-store=basic`; si no, pide el llavero de GNOME en
  la pantalla y se cuelga 40 s. La barra de Google Translate no se quita con banderas en Chromium 154: `kiosk.sh`
  apaga `translate.enabled` en las Preferences del perfil del kiosco.
- Captura de la pantalla real: `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim out.png`.
- El daemon es el servicio de usuario `claude-buddy` (`~/.config/systemd/user/`, `Restart=always`, linger del usuario). Tras un
  deploy: `systemctl --user restart claude-buddy`. Para correr `buddy.py` a mano (`-vv --log-file`): `systemctl --user stop
  claude-buddy` antes y `start` al terminar; si lo matás con pkill vuelve a los 5 s y pelea por el puerto 8765 con el manual
  (pasó el 17 oct: otra sesión lo mató 5 s después del boot y arrancó uno a mano).
- Logs del servicio: `journalctl --user-unit claude-buddy -n 50`; `journalctl --user -u` dice `No journal files were found`
  en este Pi. Salida del kiosco: `~/.cache/claude-buddy-kiosk.log`.
- labwc corre con `-m` (merge-config): `~/.config/labwc/autostart` se suma a `/etc/xdg/labwc/autostart` (panel, escritorio,
  kanshi), no lo reemplaza. `raspi-config nonint do_blanking` mete o borra la línea `swayidle` en ese mismo archivo;
  `get_blanking` = 1 significa que la pantalla no se apaga. Autologin ya venía en lightdm (`autologin-user=<user>`,
  `get_autologin_desktop` = 0). `wlopm` (con XDG_RUNTIME_DIR y WAYLAND_DISPLAY) dice si `DSI-1 on`.
- `sudo reboot`: SSH vuelve a los ~45 s; servicio y escritorio arrancan juntos, el kiosco espera al daemon y abre a los ~30 s.
- Hermes en el Pi: token en `~/.config/claude-buddy/hermes.token` (0600; no lo imprimás). Para exponer la API local se usa
  `--hermes-bind <tailscale-ip>` (o `0.0.0.0` para toda la LAN, solo para probar; la IP de wlan0 es `<lan-ip>`). Para probar desde
  la Mac sin meter el token en la terminal: `TOKEN=$(ssh <user>@buddy.local cat .config/claude-buddy/hermes.token)` y
  `curl -H "Authorization: Bearer $TOKEN" ...` (nunca `curl -v`: imprime el header). La plantilla `claude-buddy.service` no lleva los
  flags de Hermes: se agregan al `ExecStart`.

## El hardware de referencia

- Pi 4 (2 GB), hostname `buddy`, usuario `<user>`, Pi OS Trixie 64 bit, Python 3.13, BlueZ 5.82, labwc (Wayland).
- Acceso: `ssh <user>@buddy.local`. El proyecto vive en `/home/<user>/claude-buddy` con su `.venv`.
- Mac: Claude desktop con Developer Mode; menú **Developer > Open Hardware Buddy…** en la barra de menús (no en Settings).
- El Bluetooth estaba bloqueado por rfkill la primera vez (`sudo rfkill unblock bluetooth`).

## Desplegar al Pi

```bash
# desde la Mac, en la carpeta del proyecto
rsync -av --exclude .venv --exclude __pycache__ --exclude .pytest_cache ./ <user>@buddy.local:claude-buddy/
ssh <user>@buddy.local 'cd claude-buddy && .venv/bin/pip install -q -r requirements.txt && .venv/bin/python buddy.py check'
```

En el Pi de referencia el daemon ya corre solo como servicio y el kiosco abre con el escritorio (ver gotchas); tras el rsync basta
`systemctl --user restart claude-buddy`. A mano (con el servicio parado): `ssh <user>@buddy.local` → `cd claude-buddy &&
.venv/bin/python buddy.py` (teclas `a d s u q`), y en otra sesión `./kiosk.sh` para abrir Chromium en la pantalla del Pi
(funciona desde SSH: busca el socket Wayland solo).
Logs útiles: `buddy.py -vv --log-file buddy.log`. Para ver la UI desde la Mac: `ssh -L 8765:localhost:8765 <user>@buddy.local`
y abrir `http://localhost:8765` (si el stream se traba por el túnel, `?poll=500`).
Con Hermes: `--hermes-bind <IP> --hermes-token-file ~/.config/claude-buddy/hermes.token` (a mano o en el `ExecStart`);
imprime `Hermes en http://<IP>:8766/approval`.

## Mapa del código

| Archivo | Qué es |
|---|---|
| `buddy_demo.py` | Modo demo (`buddy.py demo [--lang en]`): la pantalla real con una historia guionada de 100 s y datos inventados (hilos, corridas, avisos, un permiso que se responde tocando); sin Bluetooth, hooks ni listener, con un directorio de estado temporal; para videos y capturas. En el Pi: parar el servicio y correrlo con `systemd-run --user` con `RuntimeMaxSec` y un temporizador que vuelva a arrancar `claude-buddy` |
| `buddy.py` | CLI: `run` (daemon), `check`, `forget-all`, `demo`. `Terminal` (teclado), arranque de la pantalla (`start_screen`) y del listener de Hermes (`start_hermes`, `load_hermes_token`). Flags: `--lang`, `--closet`, `--closet-every` (disfraces), `--night`, `--night-level`, `--backlight` (modo noche), `--ui-port`, `--no-ui`, `--no-keys`, `--adv-mode`, `--insecure`, `--log-file`, `--hermes-bind`, `--hermes-port`, `--hermes-token-file` |
| `buddy_core.py` | Protocolo, solo stdlib. `BuddyCore` (líneas JSON, latidos, `persona()`, nivel cada 50K tokens, packs), `ApprovalBroker` (cola de aprobaciones, `decide(key, "once"\|"deny")`), `StateStore`, `TaskBoard` (los hilos vivos: id, proyecto, herramienta, qué hace, estado, `said`, `title`, `group`, `parent`; `TASK_TTL_S` 600, `TASK_IDLE_TTL_S` 1800, `MAX_TASKS` 12; `CLAUDE_BUDDY_SAID=off` en su entorno descarta `said`) |
| `buddy_backlight.py` | `Backlight`: busca el panel bajo `/sys/class/backlight`, `set_dim`, `restore`; día = `max_brightness`, noche = `--night-level` con piso 4, escribe solo al cambiar, nunca levanta una excepción hacia el daemon (ver "Modo noche") |
| `buddy_closet.py` | El closet de disfraces del daemon (solo stdlib): `Closet`, `Costume`, escaneo y validación de carpetas, sorteo con "sin disfraz", relectura y lectura de archivos sin seguir enlaces (ver "Closet de disfraces") |
| `buddy_i18n.py` | Textos del daemon que salen en pantalla, en `es` y `en` (`t`, `ble_reason`, `parse_lang`, `normalize`); ver "Idiomas" |
| `buddy_bluez.py` | BlueZ por D-Bus (dbus-fast): GATT Nordic UART, anuncio con `LocalName`, agente de passkey DisplayOnly, re-armado si el adaptador desaparece, watchdog del Mac callado (`QUIET_PEER_TIMEOUT_S`) |
| `buddy_ui.py` | Pantalla: `UIHub` (modelo de vista + cambios coalescidos; `character()` lee el manifest del pack activo), `HttpServer` (base HTTP/1.1 sobre asyncio: parseo estricto, límites, errores JSON) y `UIServer` (HTTP solo en 127.0.0.1: `GET /`, `GET /events` SSE, `GET /state`, `POST /decide`, `GET /character/<pack>/<archivo>` solo para archivos del manifest, la query `?v=<rev>` se ignora). `character()` también devuelve `rev` (ver "Toques en Buddy"). `UIHub` también lleva el modo noche (`wake()`, `close()`, `view.night`/`dim`/`backlight`, `AWAKE_S`) y el servidor atiende `POST /wake` y `POST /dismiss` (`view.notices`; `UIHub.dismiss`). `UIHub._tasks_view` arma `view.tasks` (hasta 8 hilos, `title`/`group`/`parent`/`said` solo si no están vacíos) |
| `buddy_hermes.py` | `HermesServer` (sobre `HttpServer`, solo stdlib): el listener para Hermes en otro puerto e IP (`POST /approval`, `GET /approval/<id>?wait=N`, `DELETE /approval/<id>`, `POST /session` con el contrato de "Hechos de los hilos", `GET /healthz`). Token Bearer propio, `Host` = IP conectada, `Origin` rechazado, rate limit y lockout por cliente, `ttl` por pedido (vence solo). Encola `Approval(source="hermes"\|"cli")` en el broker; `POST /session` alimenta el `TaskBoard` del core y valida `said`, `title`, `group` y `parent` (strings, `parent` con forma de id; 400 si no) |
| `tools/context_hook.py` | Hook de Claude Code en la Mac (PreToolUse, UserPromptSubmit, Stop, SessionEnd; en `~/.claude/settings.json` con `2>/dev/null; exit 0` al final; **corre en vivo en cada sesión**, se edita por copia de scratch y `mv`) que manda a `POST /session` la carpeta, la herramienta y un resumen de lo que hace cada sesión y, si Claude desktop la conoce, su `title`, su `group` y su `parent` (`thread_info`, con la caché `~/.cache/claude-buddy/threads.json`); en `Stop` manda además `said` (300 caracteres, limpiado, solo a loopback o tailnet). Lee de Claude desktop solo `claude-code-sessions/*/*/local_*.json` y, del config, solo grupos y asignaciones. Interruptores `CLAUDE_BUDDY_CONTEXT`, `CLAUDE_BUDDY_THREADS`, `CLAUDE_BUDDY_SAID`, `CLAUDE_BUDDY_SAID_EXCLUDE` (ver "Hechos de los hilos"). El core lo guarda en `TaskBoard` y la pantalla lista los hilos vivos en la burbuja: el protocolo del Mac solo trae totales. Nunca frena la sesión: exit 0 y silencio ante cualquier falla, un temporizador que lo termina a los 2.5 s y 30 s de calma (`down_until`) tras un intento fallido |
| `tools/census.py` | El censo de la Mac: mira `ps` cada 15 s y manda a `POST /census` las corridas headless de Claude (`claude -p`) y los trabajos que el dueño nombre en `census.json`. Solo biblioteca estándar y el Python 3.9 de macOS; no importa nada del repo. `run` (el bucle) y `once [--print]` (una pasada sin mandar). Ver "Censo de corridas" |
| `tools/census_agent.sh` | Instala, quita y revisa el LaunchAgent `com.claude-buddy.census` en el dominio de tu usuario (`install [-n] [--url]`, `uninstall [-n] [--purge]`, `status [--ping]`, `restart`, `print`). bash 3.2, idempotente, muestra el plan primero |
| `tools/census.example.json` | Modelo genérico de `~/.config/claude-buddy/census.json` (sin nombres personales) |
| `tools/closet_costumes.py` | Los moldes de disfraz (8 piezas de cabeza en cuadrícula de píxeles, 7 colores) y sus reglas de anclaje; solo datos y dibujo, sin Pillow |
| `tools/make_closet.py` | Hace disfraces nuevos horneados en los GIF de Mochi dentro de una carpeta de closet (`--count`, `--seed`, `--base-name`, `--list`, `--sheet`); usa el generador con su gancho `COSTUME`; necesita Pillow |
| `tools/notify.py` | El comando con el que Hermes deja un aviso en la pantalla (`--title`, `--body`/`-`, `--kind`, `--ttl`, `--wake`, `--id`, `--source`, `--status`, `--withdraw`); manda `POST /notice`. Biblioteca estándar, Python 3.9, no importa nada del repo; limpia y corta el texto como el daemon; mismas reglas de URL y de dirección que `census.py`; 3 s de tope; salida 0/1/2. Ver "Avisos de Hermes" |
| `hermes-plugin/claude-buddy/` | Plugin de Hermes Agent (vive en el Hermes host, en `<hermes-home>/plugins/claude-buddy/`): `register(ctx)` registra el transporte de aprobación `buddy`; `client.py` (stdlib, sin imports de Hermes) hace `POST /approval` + long poll y devuelve `once`/`deny`/None. Settings en `plugins.entries.claude-buddy.settings` (`url`, `token_file`, `timeout`); se activa con `security.approval.transport: buddy` + `transport_fallback: builtin`. Si la pantalla no contesta, levanta excepción y Hermes pregunta por Discord como antes |
| `ui/index.html` | Página táctil, un solo archivo sin dependencias. Escenario fijo 800x480 escalado. Buddy SVG propio con 7 ánimos o el pack GIF (`body.pack`, colores del manifest). `?demo=cycle` o `?demo=<estado>` para verla sin daemon; los de hilos son `idle` (4 hilos de terminal), `tasks1`, `tasks6`, `longsaid`, `idle-pack`, `threads` (títulos, tres grupos, un hijo), `threads-long`, `threads8` (ocho, con scroll) `threads-pack`, `runs` y `runs-pack` (con corridas), `night`, `night-overlay` y `night-ask` (modo noche; `#dim` es la capa); Buddy al tacto: `#petzone` y `#ping` (ver "Toques en Buddy"); solo en demo, `&open=<id o número de fila>` abre el detalle y `&scroll=<px>` corre la lista. Sistema visual en `DESIGN.md` |
| `kiosk.sh` / `claude-buddy.service` | Lanzador de Chromium (elige Wayland/X11, espera al daemon) y servicio de usuario systemd |
| `tests/` | unas 2000 pruebas (en la Mac se saltan ~80 que piden Linux o dbus; en la sesión de Claude desktop hay que desarmar las variables `CLAUDE*` y `test_kiosk` falla solo ahí): `fake_bluez.py` (bluetoothd falso en un D-Bus privado), `test_bluez`, `test_core`, `test_cli` (el `buddy.py` real con pty), `test_ui` (servidor + página en Chromium headless; `view.tasks`), `test_hermes` (listener de Hermes: token, Host, cola mixta, long polling, `POST /session` con título, grupo, padre y `said`, y el daemon real con `--hermes-bind`), `test_cli_hook` (el hook de permisos como proceso contra el listener real), `test_context_hook` (el hook de contexto: el informe, `said` y su limpieza, el respaldo por transcript, la guardia del destino, `thread_info` y su caché contra archivos de desktop de mentira, el temporizador de 2.5 s y los 30 s de calma contra un buddy que gotea bytes, el comando de registro que no bloquea, el hook como proceso bajo el Python 3.9 de macOS y contra el listener real), `test_page_tasks` (la página de los hilos en Chromium headless: filas, grupos, orden estable, la marca de visto, la barra y el desvanecido de una lista que desborda, detalle, Volver y su guarda de 350 ms, cierre a los 25 s, un permiso con el detalle abierto, texto hostil, modo pack, contraste, todas las demos), `test_i18n` (las dos tablas completas y parejas, todas las demos en los dos idiomas sin desborde, el flag y la variable, el cambio de idioma en marcha), `test_closet` (el closet del daemon con carpetas de mentira y reloj y dados inyectados), `test_closet_tool` (los moldes, el anclaje, los extras que no se tapan, el determinismo; necesita Pillow), `test_closet_e2e` (la herramienta, el daemon real y la página), `test_notice` (los avisos del daemon: el tablero con reloj falso, cada validación y recorte, los cubos de pedidos, `GET`/`DELETE`, `/dismiss`, la vista, `wake` de noche y de día, registros sin texto, `run_daemon` en proceso), `test_notify` (el comando: argumentos, stdin que se cierra o no, reglas de URL y de token, salidas y frases, sin fugas; y el comando como proceso contra el `HermesServer` real cableado como `buddy.py`), `test_page_notice` (el grupo Avisos en Chromium headless: filas, tipos, titular, orden, texto hostil, detalle y Listo, guardas, la atención de 4 s, permiso, pantalla oscura, elección de pantalla, pack, demos), `test_night` (el modo noche del daemon: ventana, despertar, el temporizador, `POST /wake`, `Backlight` contra un sysfs de mentira, las banderas), `test_page_night` (la capa `#dim`, el toque que despierta, la guarda, el aviso de uso, un permiso nunca tapado), `test_page_touch` (Buddy al tacto en Chromium headless: el `rev` en las URLs de las imágenes, la máquina de ánimos del toque con el reloj controlado, la zona y el permiso, el eco, la píldora que no miente; usa los ayudantes de `test_page_tasks`), `test_census` (formas reales de `ps` de macOS, privacidad con una llave falsa, rangos, y procesos señuelo contra un `HermesServer` real en loopback), `test_census_agent` (el plist con `plistlib`/`plutil` y la secuencia de instalación con un `launchctl` y un `curl` de mentira), `page_runner.py` (el ayudante que corre la página: busca `chromium` en el Pi, Google Chrome en `/Applications` en la Mac, `/opt/pw-browsers` en CI, o el binario de `CLAUDE_BUDDY_CHROMIUM`), `test_pixel_buddy` (el pack `buddy-pixel` es lo que dibuja `tools/make_pixel_buddy.py`; necesita Pillow), `test_hermes_plugin` (el cliente del plugin de Hermes contra el listener real, y el plugin con un ctx/ApprovalRequest de mentira), `test_kiosk` |

Correr pruebas: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests -q` (necesita
`dbus-daemon`; las de página necesitan un Chromium, se saltan si no hay). El hook se prueba además con el Python de macOS:
`/usr/bin/python3 -m py_compile tools/context_hook.py`.

En la Mac (corrida completa del 18 oct 2026, Python 3.14, unos 114 s): 362 pasan, 79 se saltan y 1 falla. Las de página SÍ
corren (`tests/page_runner.py` encuentra Google Chrome en `/Applications`; con `CLAUDE_BUDDY_CHROMIUM=<binario>` se
elige otro). Se saltan las de BlueZ, las de `test_cli` y dos de `test_hermes`, porque no hay `dbus-daemon`, y el canario
de abajo, que es opt-in. La que falla es ruido de la Mac, no del código:
`test_kiosk::test_it_finds_the_pis_screen_when_started_from_ssh` (`AF_UNIX path too long` en las carpetas temporales de
macOS). Una prueba de página puede salir floja con la máquina cargada (`test_the_page_offers_always_only_with_the_rule_in_sight`
lo hizo en otras corridas): repetila sola antes de culpar al cambio.

Canario de Claude desktop (opt-in): `CLAUDE_BUDDY_CANARY=1 .venv/bin/python -m pytest tests/test_context_hook.py -k real_desktop`
corre contra los archivos REALES de Claude desktop y de `~/.claude/sessions/` y comprueba solo los nombres de las llaves que
lee el hook (no imprime nada privado; carga el config entero en el proceso de la prueba). Correrlo después de actualizar
Claude desktop: si falla, cambió el formato y los hilos se quedan sin título o sin grupo.

## Reglas que no se negocian

- **`POST /decide` aprueba comandos.** Solo loopback, token por arranque metido en la página, `Host`/`Origin`
  verificados, content-type JSON obligatorio. Cualquier cambio ahí va con prueba. Hermes entra con su propio token.
- **Hermes nunca toca `/decide`.** Su listener es otro (`HermesServer`, puerto e IP aparte, `--hermes-bind`) y solo sabe
  encolar (`POST /approval`), contar la decisión (`GET /approval/<id>`) y recibir lo que dicen las sesiones de la Mac
  (`POST /session`, que solo pone datos en la pantalla y no aprueba nada); no sirve la página, ni `/events`, ni `/state`.
  Token propio (`Authorization: Bearer`, 16+ caracteres, nunca en logs ni en la terminal), `Host` igual a la IP a la que
  el cliente se conectó, cualquier `Origin` se rechaza, sin callbacks: el Pi no abre conexiones hacia URLs que lleguen en
  un body. Cualquier cambio ahí va con prueba en `tests/test_hermes.py`.
- **La página nunca convierte en HTML texto recibido** (`textContent` siempre): el hint de un permiso viene de una
  sesión que una web hostil puede dirigir. Lo mismo vale para lo que dice un hilo (título, grupo, `said`, carpeta,
  herramienta): es texto, y los ids y nombres de grupo, que también vienen de afuera, solo se usan como claves de un
  `Map` (`constructor` y `__proto__` son válidos). CSP `default-src 'none'`.
- **El detalle de un hilo no toca el armado.** El código de los hilos nunca lee ni escribe `holdUntil`, `ARM_MS`,
  `decide()` ni `paintAsk()`. Un permiso siempre gana: si llega con el detalle abierto, `render()` lo cierra
  (`closeTask()` cuando la pantalla no es la principal) y los botones se arman a los 600 ms como siempre. El detalle se
  cierra solo a los 25 s sin toque, y Volver ignora los primeros 350 ms (rebote del dedo que abrió la fila).
- **Lo que dicen las sesiones no se loguea, y `said` no viaja a cualquier parte.** `said`, `title`, `group` y `parent` no van a
  ningún log del daemon; el config de Claude desktop se lee solo para grupos y asignaciones (ver "Hechos de los hilos");
  `said` solo sale de la Mac hacia loopback o la tailnet. Cualquier cambio en el hook o en `POST /session` va con
  prueba (`tests/test_context_hook.py`, `tests/test_hermes.py`, `tests/test_core.py`).
- **`tools/context_hook.py` corre EN VIVO en cada sesión de Claude Code de esta Mac**, incluida la tuya: está
  registrado en `~/.claude/settings.json` (PreToolUse, UserPromptSubmit, Stop, SessionEnd, con
  `/usr/bin/python3` 3.9.6), así que apenas se guarda el archivo del repo todas las sesiones corren la versión nueva.
  **El comando registrado termina en `2>/dev/null; exit 0`** (también el del hook de permisos, `cli_hook.py`): si el
  archivo desaparece (carpeta movida, checkout sin `tools/`, backup restaurado) Python sale con código 2 y Claude Code
  lo lee como "bloquear", con lo que cada herramienta de cada sesión quedaría negada y cada pedido escrito se borraría;
  el script siempre sale con 0, pero solo la línea de comando cubre un script que no arranca. Se
  edita en una copia de scratch, se prueba ahí (`/usr/bin/python3 -m py_compile` y las pruebas del hook) y recién
  entonces se mueve a su lugar con `mv` (rename atómico). Nunca se edita en el árbol de trabajo. Solo stdlib y sintaxis
  de Python 3.9 (sin `X | Y` en anotaciones, sin `match`).
- Animaciones solo con `transform`/`opacity` (Pi 4). Nada externo en la página (sin CDN, sin fuentes web).
- Botones de aprobar se activan 600 ms después de aparecer el pedido (anti toque fantasma). No bajar eso.
- Core y UI sin dependencias fuera de stdlib; la única dependencia es `dbus-fast` en el transporte.
- Al reiniciar el daemon cambia `boot` en el view model y la página se recarga sola (token nuevo). Mantenerlo.
- El personaje es propio, no es Clawd ni ningún mascot conocido.
- `POST /notice` y `POST /dismiss` (como `/session`, `/census` y `/wake`) nunca aprueban ni deniegan nada ni caen bajo las reglas de `/decide`; el texto de un aviso no se escribe en ningún registro ni error, y la página lo pone con `textContent`. Cambios en `/notice` o `/dismiss` van con prueba (`tests/test_notice.py`).
- `POST /wake` solo despierta (loopback, token, Host/Origin como `/decide`, su propio límite) y nunca aprueba ni cambia nada más. `hub.close()` (restaura el backlight) va primero en el `finally` de `run_daemon`. Sin modo noche el daemon nunca toca el brillo. `#dim` nunca cubre ni oscurece un permiso, el toque que despierta lo consume `#dim` en `click`, y el código de la noche nunca toca `holdUntil`, `ARM_MS`, `decide()`, `paintAsk()` ni los ánimos del toque.
- El código de toque de la página (`Buddy al tacto`) nunca toca `holdUntil`, `ARM_MS`, `decide()`, `paintAsk()` ni el detalle de un hilo, no usa la red, no guarda nada y no escribe texto; sobre un permiso no responde. Los 600 ms de armado no se bajan por esto.

## Referencias

- Protocolo: https://github.com/anthropics/claude-desktop-buddy (REFERENCE.md y firmware). NUS
  `6e400001-b5a3-f393-e0a9-e50e24dcca9e`, RX `...0002`, TX `...0003`, JSON por líneas, nombre `Claude-XXXX`.
- Hechos de BlueZ verificados en el código fuente (5.66/5.82): `Includes=["local-name"]` corta a 10 letras, por eso
  se usa `LocalName`; `DiscoverableTimeout` rompe Flags en 5.66; quitar el adaptador destruye la GATT DB.
- Kiosco en Pi OS: `~/.config/labwc/autostart` (labwc corre con `-m`, se suma al del sistema), Chromium necesita
  `--ozone-platform=wayland` desde SSH.
