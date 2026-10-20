# Arquitectura

Claude Buddy es un daemon de Python en el Pi, una ventana de Chromium que dibuja su pantalla y unos clientes chicos en la compu donde corre Claude. Todo lo que el daemon necesita viene en la librería estándar de Python salvo `dbus-fast`, que usa el transporte Bluetooth.

## Procesos

| Dónde | Proceso | Qué hace |
|---|---|---|
| Pi | `buddy.py` (un solo proceso asyncio) | Habla BLE con Claude desktop, guarda el estado, sirve la pantalla y la API local |
| Pi | Chromium, lanzado por `kiosk.sh` | Dibuja `ui/index.html` a pantalla completa desde `http://127.0.0.1:8765` |
| Tu compu | Claude desktop | El lado Hardware Buddy del protocolo BLE |
| Tu compu | `tools/context_hook.py` (opcional) | Una corrida corta por evento de Claude Code; le cuenta al Pi qué hace cada sesión |
| Tu compu | `tools/cli_hook.py` (opcional) | Pide permiso en la pantalla para Claude Code en la terminal |
| Tu compu | `tools/census.py` vía un LaunchAgent (opcional) | Mira la tabla de procesos y reporta las corridas en segundo plano |
| Donde sea | `tools/notify.py` (opcional) | Deja un aviso en la pantalla |
| Máquina de Hermes | `hermes-plugin/claude-buddy` (opcional) | Las aprobaciones de Hermes Agent se contestan en la pantalla |

## Módulos dentro del daemon

- `buddy_core.py`: el protocolo, el armado de líneas, los latidos, el `ApprovalBroker` (una sola cola de aprobaciones pendientes, venga de donde venga), el estado persistente, la recepción de packs de personajes, el `TaskBoard` (hilos y corridas vivos) y el `NoticeBoard`.
- `buddy_bluez.py`: servidor GATT, anuncio, agente de passkey y un vigilante para una compu que conecta pero nunca abre el canal. Nunca toca el protocolo.
- `buddy_ui.py`: `UIHub` (el modelo de vista: lo que muestra la pantalla, calculado desde el core, el broker y la radio, más el modo noche y el closet) y `UIServer`, el servidor HTTP en loopback. También tiene `HttpServer`, la base HTTP/1.1 estricta que comparte con la API local.
- `buddy_hermes.py`: el servidor de la API local (`HermesServer`).
- `buddy_backlight.py`, `buddy_closet.py`: la luz del panel para el modo noche y el closet de disfraces.

## Puertos

| Puerto | Escucha en | Para qué | Autenticación |
|---|---|---|---|
| 8765 (`--ui-port`) | solo `127.0.0.1` | La pantalla: página, server-sent events, `POST /decide`, `/wake`, `/dismiss` | Un token que va dentro de la página en cada arranque, más revisión de `Host`/`Origin` |
| 8766 (`--hermes-port`) | `--hermes-bind IP`; apagado si no se da | La API local (`/approval`, `/session`, `/census`, `/notice`) | `Authorization: Bearer` con su propio token |

Los dos puertos nunca comparten rutas: nada en el 8766 puede aprobar nada, y `/approval` no existe en el 8765. Las rutas están en [local-api.md](local-api.md).

## Flujo de datos

```
Claude desktop ──BLE──> buddy_bluez ──> BuddyCore ──> ApprovalBroker ──> UIHub ──SSE──> página en Chromium
  (latidos,               (GATT)         (protocolo)     (una cola)     (modelo de vista)        │
   pedidos)                                                  ▲                                   │ toque
        ▲                                                    │ POST /approval (hermes, cli)      ▼
        └──── {"cmd":"permission","decision":...} <── decide ◄───────────────────── POST /decide (127.0.0.1)

context_hook / census / notify ──HTTP, token──> API local ──> TaskBoard / NoticeBoard ──> UIHub ──> página
```

1. **Permisos de Claude desktop.** Un latido trae un `prompt` (id, herramienta, pista). El core lo mete en el broker como una aprobación con fuente `claude`. La página la muestra; un toque llama a `POST /decide`; el broker le pasa la decisión al responder, que manda `{"cmd":"permission","id":...,"decision":"once"|"deny"}` por BLE.
2. **Permisos de otras fuentes.** `POST /approval` en la API local encola una aprobación con fuente `hermes` o `cli` en el mismo broker; quien pidió se entera de la respuesta por long polling. Las teclas de la terminal (`a`, `d`) y la pantalla contestan la misma cola, la más vieja primero, y cada respuesta vuelve solo a la fuente que pidió.
3. **Información, nunca decisiones.** Los hilos (`POST /session`), las corridas (`POST /census`) y los avisos (`POST /notice`) van a tableros que viven en memoria. El hub los suma a la vista (`view.tasks`, `view.notices`) y la página se redibuja. Reiniciar el daemon vacía los tableros hasta que quienes los alimentan vuelvan a hablar.
4. **La página** recibe el modelo de vista entero como server-sent events (o preguntando a `GET /state` cada 500 ms si la URL de la página lleva `?poll=500`), lo dibuja solo con `textContent` y devuelve únicamente toques.

## Estado en disco

`<state-dir>` (por defecto `~/.local/state/claude-buddy`): `state.json` (nombre, dueño, nivel, aprobadas, denegadas, tokens), `peers.json` (qué dispositivos Bluetooth son del buddy), `characters/` (packs instalados), `closet/` (disfraces, si los usás). El daemon nunca escribe en disco ni en ningún log lo que dicen tus sesiones.

En tu compu los clientes guardan `~/.config/claude-buddy/hermes.token` (el token de la API, permisos 0600) y, opcional, `census.json`; el hook de contexto guarda una caché chica en `~/.cache/claude-buddy/`.

## Correr como servicio de sistema

Lo normal es el servicio de usuario de `claude-buddy.service` (ver el README). Si en tu Pi BlueZ solo acepta al buddy con `sudo`, usá una unidad de sistema:

```ini
# /etc/systemd/system/claude-buddy.service
[Unit]
Description=Claude Buddy (BLE y pantalla táctil)
After=bluetooth.service
Wants=bluetooth.service

[Service]
ExecStart=/home/<usuario>/claude-buddy/.venv/bin/python /home/<usuario>/claude-buddy/buddy.py --no-keys --state-dir /home/<usuario>/.local/state/claude-buddy
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now claude-buddy
journalctl -u claude-buddy -f
```

## Reglas de diseño que dan forma al código

- `POST /decide` aprueba comandos: solo loopback, token por arranque, revisión de `Host`/`Origin`, content-type JSON obligatorio. Cualquier cambio ahí va con prueba.
- La API local puede encolar y contar, pero nunca decide. No abre conexiones hacia una URL que llegue en el cuerpo de un pedido (sin callbacks).
- El texto que viene de afuera (un pedido, el título de un hilo, un aviso) solo se escribe con `textContent`; la página tiene una CSP de `default-src 'none'`.
- Los botones de aprobar se activan 600 ms después de que aparece un pedido. Nada más en la página lee ni escribe ese temporizador.
- Las animaciones usan solo `transform` y `opacity` (Pi 4).
