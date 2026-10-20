# Hilos y hook de contexto (opcional)

Con varias sesiones de Claude abiertas, "3 trabajan" no dice cuáles. El protocolo Hardware Buddy solo trae totales, así que un hook chico de Claude Code en tu compu le cuenta al buddy qué hace cada sesión. La pantalla entonces lista **qué conversaciones están vivas**, con su título real, agrupadas como la barra lateral de Claude desktop.

Necesita la [API local](../local-api.md) encendida y el token puesto. Instalá el hook con el bloque del README ([Hook de contexto](../../../README.es.md#hook-de-contexto-qué-conversaciones-están-vivas)). Está pensado para macOS: lee los archivos de Claude desktop bajo `~/Library/Application Support/Claude`.

## Qué muestra la pantalla

- **Solo hilos vivos.** Los que trabajan y los que terminaron su turno y esperan tu respuesta. No hay historial. Un hilo que trabaja y no dice nada en 10 minutos se va; uno que terminó se queda 30 minutos. El tablero guarda 12, la pantalla muestra 8: primero los que trabajan, después los más nuevos.
- **Una fila** es el título de la conversación. Una sesión de terminal que Claude desktop no conoce no tiene título ni grupo: sale con el nombre de su carpeta bajo el grupo "Terminal". Una sesión de desktop que no está en ningún grupo sale bajo "Sesiones". Un hilo que otro lanzó cuelga, sangrado, debajo de su padre mientras el padre siga vivo.
- **Los colores.** Las filas que trabajan toman el color del ánimo; una que terminó es gris suave y lleva una marca de visto antes de la edad. Una fila cambia de aspecto cuando cambia su estado, nunca de lugar.
- **Si hay más de los que caben.** La lista muestra unas tres filas a la vez; una barra fina a la derecha dice que hay más (siempre) y la última fila visible se desvanece hacia el borde. Deslizá con el dedo.
- **Tocá una fila** y leés lo que dijo (hasta 300 caracteres de su última respuesta; si está trabajando, la del turno anterior, con el rótulo "Antes dijo:"). **Volver** regresa. Si no tocás nada se cierra a los 25 segundos, y un permiso siempre gana: el detalle se cierra y los botones se arman a los 0,6 s como siempre.

## Cómo funciona

El hook (`tools/context_hook.py`) corre en `PreToolUse` y `UserPromptSubmit` (trabaja), `Stop` (terminó y espera tu respuesta) y `SessionEnd` (se fue). `Stop` no corre si interrumpís a Claude: un hilo interrumpido sigue como "trabaja" hasta que habla otra vez o pasan 10 minutos.

Los títulos y los grupos salen de los archivos de Claude desktop, enlazados solo por el id que trae el hook (`cliSessionId`); nada se adivina. El grupo de un hijo sin grupo propio es el del ancestro más cercano, como en la barra lateral.

## Nunca frena una sesión

Sale con 0 y en silencio pase lo que pase. Se rinde a los 1,5 s para conectar y a los 2 s esperando respuesta, y un temporizador lo termina a los 2,5 s pase lo que pase con la red (el límite de Claude Code es 5 s). Tras un intento fallido, el buddy se deja en paz 30 segundos: las llamadas a herramientas y los pedidos nuevos ni lo intentan, así que un Pi apagado le cuesta a la sesión una espera lenta cada 30 segundos y no una por herramienta. `Stop` y `SessionEnd` siempre lo intentan, porque un "terminó" perdido dejaría al hilo diciendo "trabaja".

Dejá el `2>/dev/null; exit 0` en cada comando registrado: sin eso, un script que falta sale con código 2, que Claude Code lee como "bloquear".

## Qué viaja, qué se lee, qué se escribe

Al Pi viaja, por HTTP plano y con el token: la carpeta, la herramienta, un resumen de una línea, el **título** de la conversación, el **nombre de su grupo**, el id del hilo padre y, cuando termina un turno, **`said`**: hasta 300 caracteres de su última respuesta (los 200 del principio y los 97 del final), con el markdown aplanado, los bloques de código reemplazados por `(código)` y lo que parece un secreto reemplazado por `[oculto]`. Ese filtro es de mejor esfuerzo, no una frontera, y a veces tapa de más (un id largo con forma de UUID). Por eso conviene que la API escuche en la IP de Tailscale y no en `0.0.0.0`.

- `said` solo sale de tu compu hacia ella misma o hacia la tailnet (100.64.0.0/10 y fd7a:115c:a1e0::/48); hacia cualquier otra IP el hook lo quita. Ojo: 100.64.0.0/10 también lo usan los proveedores de internet (CGNAT) y otras VPN, así que eso prueba "no es la red de la casa", no "es Tailscale".
- En tu compu el hook lee de Claude desktop solo `claude-code-sessions/<org>/<org>/local_<uuid>.json` (título, id y de quién viene la sesión) y, **del archivo de configuración de Claude desktop, únicamente los grupos y a qué grupo pertenece cada sesión**. El resto de ese archivo (los ajustes de los conectores, por ejemplo) no se mira, no se guarda, no se loguea y no se manda. También lee `~/.claude/sessions/<pid>.json` para saber a qué sesión de desktop pertenece cada hook.
- Lo único que escribe está en `~/.cache/claude-buddy/` (carpeta 0700, archivos 0600, tuyos; si no son privados se ignoran): `threads.json` (id del hook, id de desktop, título y padre; se vuelve a mirar a los 30 s) y `down_until` (la pausa de 30 segundos).
- El daemon guarda los hilos solo en memoria, no escribe nada de eso en ningún log y no se lo ofrece a nadie: la pantalla escucha en `127.0.0.1`.

## Interruptores (variables de entorno del hook)

| Variable | Qué hace |
|---|---|
| `CLAUDE_BUDDY_URL` | A dónde manda. Default `http://buddy:8766` |
| `CLAUDE_BUDDY_TOKEN_FILE` | El token. Default `~/.config/claude-buddy/hermes.token`; tiene que ser 0600 y tuyo |
| `CLAUDE_BUDDY_CONTEXT=off` | Apaga el hook entero |
| `CLAUDE_BUDDY_THREADS=off` | No manda título, grupo ni padre, y no lee nada de Claude desktop; los hilos se nombran por su carpeta |
| `CLAUDE_BUDDY_SAID=off` | Nunca manda `said` ni abre el transcript. En el entorno del **daemon** (se lee al arrancar) hace que lo descarte aunque un cliente lo mande |
| `CLAUDE_BUDDY_SAID_EXCLUDE=a,b` | Nombres de carpeta cuyas sesiones nunca mandan `said`. El hook es global, así que las sesiones programadas y headless también lo mandan: esta lista es para ellas |

Solo para pruebas o para mover carpetas: `CLAUDE_BUDDY_DESKTOP_DIR`, `CLAUDE_BUDDY_CACHE_DIR`, `CLAUDE_CONFIG_DIR`.

Después de actualizar Claude desktop, corré el canario: `CLAUDE_BUDDY_CANARY=1 .venv/bin/python -m pytest tests/test_context_hook.py -k real_desktop`. Comprueba solo nombres de llaves y no imprime nada privado.

El contrato del cable (`POST /session`) está en [local-api.md](../local-api.md#post-session-qué-hace-un-hilo).
