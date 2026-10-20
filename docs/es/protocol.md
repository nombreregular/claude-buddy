# Protocolo Hardware Buddy, tal como está implementado

Esto describe lo que el daemon hace en el cable, según `buddy_core.py` y `buddy_bluez.py`. El protocolo es el Hardware Buddy de Anthropic ([REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md) y el firmware del ESP32); las diferencias están al final.

## Transporte

- BLE GATT con el servicio Nordic UART: servicio `6e400001-b5a3-f393-e0a9-e50e24dcca9e`, RX `...0002` (escribe el desktop; `write` y `write-without-response`), TX `...0003` (el dispositivo notifica).
- Con el emparejamiento activo (lo normal), las dos características exigen un enlace autenticado y cifrado. `--insecure` lo quita y es solo para pruebas.
- El emparejamiento usa un agente DisplayOnly: BlueZ elige un passkey, la pantalla lo muestra en 6 dígitos y el desktop te pide escribirlo. Se rechazan la comparación numérica, el "just works" y escribir un passkey en el Pi.
- Anuncio: el UUID de 128 bits del servicio va en el anuncio y el nombre local completo en la respuesta de escaneo, como el ESP32. El nombre es `Claude-XXXX`, los últimos cuatro dígitos hexadecimales de la dirección del adaptador, en mayúsculas. `--adv-mode kernel` le pide al kernel que agregue el nombre del adaptador, y el kernel lo corta a 10 caracteres (`Claude-345`).
- Las notificaciones se parten al MTU negociado menos 3 bytes, con un máximo de 180 bytes y un mínimo de 20, y 4 ms entre pedazos.

## Armado de líneas

Un objeto JSON por línea. Las líneas que entran terminan en LF o CR y pueden llegar partidas en cualquier cantidad de escrituras (un carácter UTF-8 partido entre dos escrituras no es problema). Una línea de más de 16 KB se descarta entera, cola incluida. Se ignoran las líneas que no son UTF-8, que no son JSON o que no son un objeto. El JSON que sale es solo ASCII (escapes `\uXXXX`), así que una notificación partida nunca corta un carácter por la mitad.

## Mensajes del desktop

El tipo de mensaje decide cómo se trata:

| Trae | Significa |
|---|---|
| `cmd` | Un comando; se contesta con un ack |
| `time` | `[epoch_segundos, desfase_de_zona_en_segundos]`: el reloj del desktop, que usan el reloj de la pantalla y el modo noche |
| `evt` | Un evento (por ejemplo de un turno); mantiene vivo el enlace pero no es un latido |
| cualquiera de `total running waiting msg entries tokens tokens_today prompt completed` | Un latido |

Cualquier comando, `time`, `evt` o latido marca el enlace como vivo. Sin tráfico durante 30 segundos el enlace se da por muerto y el estado se borra.

### Campos del latido

`total`, `running`, `waiting` (conteos, recortados a 0 hasta 2^31-1), `tokens_today`, `completed` (true quiere decir "acaba de terminar un turno"), `msg` (hasta 200 caracteres), `entries` (hasta 8 líneas de 200), `tokens` (tokens desde que arrancó el desktop; el daemon acredita las diferencias y se ancla con el primer valor, así que reiniciar el daemon no vuelve a acreditar una sesión entera) y `prompt`: `{"id", "tool", "hint"}`.

Un `prompt` se vuelve una aprobación pendiente con fuente `claude` (herramienta hasta 80 caracteres, pista hasta 500). El mismo `id` en latidos siguientes es el mismo pedido. Un latido sin `prompt`, o 30 segundos sin latidos, lo retira.

### Comandos

| `cmd` | Efecto | Ack |
|---|---|---|
| `status` | Informa nombre, dueño, `sec` (enlace cifrado), uptime y disco libre, y estadísticas (aprobadas, denegadas, mediana de segundos en contestar, segundos de siesta, nivel) | `{"ack":"status","ok":true,"n":0,"data":{...}}` |
| `name` | Pone el nombre del buddy (24 caracteres) | ack |
| `owner` | Pone el nombre del dueño (32 caracteres) | ack |
| `species` | Pone el índice de especie, 0 a 255; 255 significa "usar el personaje GIF" | ack |
| `unpair` | Olvida los emparejamientos hechos con el buddy | ack |
| `char_begin`, `file`, `chunk`, `file_end`, `char_end` | Recibir un pack de personaje, ver abajo | un ack por cada uno, con `n` |

Todo comando recibe un ack, también los desconocidos (`ok:false`, `"error":"unknown command"`). Un comando `permission` que venga del desktop se ignora: esa dirección es la nuestra.

## Mensajes al desktop

- Acks: `{"ack": "<cmd>", "ok": true|false, "n": <int>, "error"?: "...", "data"?: ...}`.
- La decisión sobre un permiso: `{"cmd":"permission","id":"<id del prompt>","decision":"once"|"deny"}`. Una aprobación en menos de 5 segundos además pone contenta a la mascota.
- Por acá solo van las decisiones sobre pedidos que llegaron por BLE. Las aprobaciones de otras fuentes nunca salen del Pi.

## Recibir un pack de personaje

`char_begin` `{name, total}` abre una transferencia hacia una carpeta temporal, `file` `{path, size}` empieza un archivo, `chunk` `{d}` trae datos en base64 (el `n` del ack son los bytes del archivo hasta ahora), `file_end` lo cierra y `char_end` valida e instala.

Límites y revisiones: 1,8 MB en total y 64 archivos como máximo; el disco libre tiene que alcanzar para el pack más 4 KB; el nombre de un archivo es un nombre plano (sin `/` ni `\`, sin punto al principio, sin caracteres de control, hasta 100 caracteres); un archivo no puede pasar el tamaño que declaró; todo archivo que nombra el manifest tiene que existir; el manifest tiene que ser un objeto JSON. El pack se instala de forma atómica en `<state-dir>/characters/<nombre>` (el nombre se limpia a `A-Za-z0-9._-`, 48 caracteres), reemplaza al anterior y queda como personaje activo. Una transferencia sin tráfico por 20 segundos se aborta; si se cae el enlace, también. Ver [features/pets-and-packs.md](features/pets-and-packs.md).

## Diferencias con el firmware de referencia

- Un evento `turn` no es un latido. La referencia trata toda línea sin `cmd`/`time` como una foto del estado, así que un evento de turno borraría un pedido pendiente.
- Todo comando recibe un ack, también los desconocidos.
- Las transferencias de packs validan rutas y tamaños, escriben en una carpeta temporal e instalan de forma atómica.
- Una sesión corriendo alcanza para el ánimo "trabajando" (el firmware del ESP32 usa 3).
- Si un desktop emparejado queda conectado pero no se suscribe a TX ni escribe en 20 segundos, el daemon lo desconecta (hasta 3 veces por racha de silencio) para que reconecte y abra el canal. Nunca toca dispositivos que no son del buddy ni dispositivos Bluetooth clásicos.
