# API local

El daemon corre dos servidores HTTP. En el código el segundo todavía se llama el listener de Hermes (`buddy_hermes.py`, opciones `--hermes-*`); en estos docs es **la API local (históricamente "Hermes listener")**, porque la puede usar cualquiera: Hermes Agent, los hooks de Claude Code, el censo, scripts.

| | Servidor de la pantalla | API local |
|---|---|---|
| Puerto por defecto | 8765 (`--ui-port`) | 8766 (`--hermes-port`) |
| Escucha en | solo `127.0.0.1` | la IP que le des con `--hermes-bind`; **apagada sin eso** |
| La usa | la página en Chromium | hooks, censo, `notify.py`, Hermes |
| Puede aprobar un comando | sí, con un toque (`POST /decide`) | nunca |

Los dos hablan HTTP/1.1 estricto, un pedido por conexión, cuerpos de hasta 4 KB (`413` si pasan), errores en JSON `{"ok": false, "error": "..."}`. Ninguno escribe en un log el texto de un pedido, un hilo o un aviso.

## La API local (puerto 8766)

### Encenderla

```bash
buddy.py --hermes-bind <ip-de-tailscale> --hermes-token-file ~/.config/claude-buddy/hermes.token
```

El token es un archivo que solo lea tu usuario (el daemon avisa si otros lo pueden leer) o la variable `BUDDY_HERMES_TOKEN`, y tiene **al menos 16 caracteres**. Sin un token válido el daemon imprime `Hermes apagado: ...` y corre sin la API. Mejor la IP de Tailscale que `0.0.0.0`, que abre la API a toda tu LAN.

### Qué se revisa en cada pedido

- `Authorization: Bearer <token>`, comparado en tiempo constante, nunca en logs. **5 tokens malos en un minuto desde la misma IP bloquean a esa IP un minuto** (`429`), en todas las rutas. No reintentes después de un `403`.
- `Host` tiene que ser la IP a la que el cliente se conectó, o el nombre propio del Pi o `<nombre>.local`, cada uno con el puerto. Un nombre MagicDNS como `buddy.<tailnet>.ts.net` se rechaza con `403`; usá la IP.
- Cualquier cabecera `Origin` se rechaza: los clientes son scripts, no navegadores.
- `Content-Type: application/json` en los `POST`.
- Como máximo 16 conexiones abiertas y 16 aprobaciones pendientes.
- Presupuestos de pedidos por cliente y por minuto, cada uno por separado para que uno no ahogue a otro: **300** para `/approval` y `/session` juntos, **60** para `/census`, **30** para `/notice`. Pasado el límite: `429`.
- `GET /healthz` contesta `ok` sin token (igual revisa `Host`).

### `POST /approval`

Mete un pedido de permiso en la pantalla, en la misma cola que los de Claude desktop.

```json
{"id": "h_1", "tool": "terminal", "hint": "sudo systemctl restart pihole-FTL", "ttl": 900, "source": "hermes"}
```

- `id`: de 1 a 64 caracteres de `A-Za-z0-9_.-`, lo elige quien llama. `tool` (se corta a 80) y `hint` (a 500) son lo que se ve en la pantalla.
- `ttl`: segundos, de 1 a 3600, por defecto 900. Si nadie contesta en ese tiempo el pedido vence y se va de la pantalla.
- `source`: `hermes` (default) o `cli` (una terminal de Claude Code, ver [features/hermes.md](features/hermes.md)); nunca `claude`, esas respuestas van a la compu por BLE.
- `always` (solo con `source: "cli"`, hasta 120 caracteres imprimibles): la regla de permiso que guardaría un tercer botón, **Siempre**. Solo entonces la pantalla muestra ese botón y `decision` puede volver como `always`.
- Una llave `callback` se rechaza con `400`: el Pi nunca abre conexiones hacia una URL que le pasen.
- Respuesta `202 {"ok":true,"id":"h_1","key":"hermes:h_1","poll":"/approval/h_1","ttl":900}`. `409` si ese `id` ya está pendiente.

### `GET /approval/<id>?wait=N`

`{"id","key","tool","state","decision"}`. `state` es `pending`, `decided` (`decision` es `once`, `deny` o `always`), `withdrawn` o `expired`. `wait` (0 a 30) sostiene la conexión hasta N segundos esperando la decisión: long polling. Repetí hasta que `state` deje de ser `pending`. Un pedido contestado se recuerda 10 minutos; después, `404`.

### `DELETE /approval/<id>`

Quien llamó se cansó de esperar: el pedido sale de la pantalla (`withdrawn`).

```bash
TOKEN=$(cat ~/.config/claude-buddy/hermes.token)
curl -sS -X POST http://buddy:8766/approval \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"id": "h_1", "tool": "terminal", "hint": "ls /tmp"}'
curl -sS "http://buddy:8766/approval/h_1?wait=30" -H "Authorization: Bearer $TOKEN"
```

Nunca uses `curl -v`: imprime el token. Por qué long polling y no un callback: con un callback, alguien con el token robado podría usar el Pi para escanear tu red. Con long polling el Pi no llama a nadie, quien pregunta no necesita un puerto abierto y funciona igual por Tailscale, por NAT o en una LAN.

### `POST /session`: qué hace un hilo

Lo manda `tools/context_hook.py`. Solo `id` es obligatorio.

| Campo | Qué es |
|---|---|
| `id` | El `session_id` del hook, de 1 a 64 caracteres de `A-Za-z0-9_.-` |
| `state` | `working` (default), `idle` (terminó su turno y espera) o `gone` (se va del tablero con todo lo suyo) |
| `project`, `tool`, `what` | Carpeta (40), herramienta (40), resumen de una línea (120). Un `project` vacío conserva el anterior; un `idle` sin `tool` ni `what` conserva los del turno anterior |
| `said` | Hasta 300 caracteres de texto plano; conserva los saltos de párrafo |
| `title`, `group` | Hasta 60 y 40 caracteres, en una sola línea |
| `parent` | El `id` del hilo que lanzó a este |

Para `said`, `title`, `group` y `parent` hay una sola regla: **llave ausente = el tablero se queda con lo que tiene; `""` = borra; texto = reemplaza.** Un valor que no es un string, o un `parent` que no tiene forma de id, responde `400`; un hilo nunca es su propio padre. Responde `200 {"ok":true,"id":...,"state":...}`. El tablero guarda 12 hilos y le pasa 8 a la pantalla. Ver [features/threads-and-context-hook.md](features/threads-and-context-hook.md).

### `POST /census`: lo que corre sin un hook

Lo manda `tools/census.py`: `{"v":1,"source":"mac","seq":N,"ttl":60,"more":0,"rows":[...]}`, hasta 8 filas `{id, name, group, state:"working", elapsed, idle?, session?, project?}` con ids que empiezan con `run-` o `job-`. Una foto reemplaza a la anterior de su `source` y caduca a los `ttl` segundos (recortado a 5..120, default 60). Un tipo equivocado es `400`; un número fuera de rango se recorta, nunca se rechaza; un `seq` más viejo responde `{"ok":true,"stale":true}`. Ver [features/census.md](features/census.md).

### `POST /notice`, `GET /notice/<id>`, `DELETE /notice/<id>`

`{"title","body?","kind?","ttl?","wake?","id?","source?"}` -> `200 {"ok":true,"id":...,"active":n}`. El estado de un aviso es `shown`, `dismissed`, `expired`, `withdrawn`, `evicted` o `unknown`. Todos los límites están en [features/notices.md](features/notices.md).

`/session`, `/census` y `/notice` responden `404` si el daemon no tiene el tablero correspondiente, y ninguna de las tres puede aprobar ni denegar nada.

## El servidor de la pantalla (puerto 8765)

Solo se alcanza desde el propio Pi (o por un túnel SSH). Todo pedido necesita un `Host` de nombre loopback y, si el navegador manda `Origin`, uno que coincida.

| Ruta | Qué hace |
|---|---|
| `GET /` | La página, con el token de este arranque adentro (token nuevo en cada arranque; la página se recarga sola cuando el daemon reinicia) |
| `GET /events` | Server-sent events con el modelo de vista entero, una pista `retry: 1500` y un ping cada 10 s; hasta 8 streams |
| `GET /state` | El mismo modelo de vista, una vez (la página lo consulta con `?poll=500` en su URL) |
| `POST /decide` | `{"key": "claude:<id>", "decision": "once"\|"deny"\|"always"}`. Pide `X-Buddy-Token` y `Content-Type: application/json` (`415` si no). `200`, `409` si ya no está pendiente, `400` si no ofrece `always` |
| `POST /wake` | Un toque en la pantalla oscura: la despierta 30 segundos. Mismas reglas de token; hasta 20 cada 10 s; `{"ok":true,"dim":bool}` |
| `POST /dismiss` | `{"id": "<id del aviso>"}` retira un aviso. Mismas reglas de token; hasta 20 cada 10 s; `{"ok":true,"state":...}` |
| `GET /character/<pack>/<archivo>` | Una imagen del pack activo (o del disfraz puesto), y solo archivos que su manifest nombra |
| `GET /healthz` | `ok` |

La página se sirve con `Content-Security-Policy: default-src 'none'` (script y estilo en línea, imágenes de sí misma).
