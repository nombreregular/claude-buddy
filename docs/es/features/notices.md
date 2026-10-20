# Avisos (opcional)

Un script o un asistente deja un mensaje corto en la pantalla: "la tanda terminó", "reunión en 10 minutos". Sale como el grupo **Avisos** arriba de la lista, se cierra con un toque en **Listo**, caduca solo y, si se pide, despierta de noche la pantalla tenue. Los avisos viven en memoria: reiniciar el daemon los borra.

Necesita la [API local](../local-api.md).

## Desde la línea de comandos

`tools/notify.py` es un solo archivo (librería estándar, Python 3.9) que no importa nada del repo, así que corre también en el Pi o en la máquina donde vive un asistente:

```bash
python3 tools/notify.py --title 'La tanda terminó' --body '37 de 40 pasaron' --kind ok --ttl 600 --id tanda-21 --wake
echo 'detalle largo' | python3 tools/notify.py --title 'Aviso' --body -     # el stdin tiene que cerrarse
python3 tools/notify.py --status tanda-21      # shown, dismissed, expired, withdrawn, evicted o unknown
python3 tools/notify.py --withdraw tanda-21
```

| Opción | Qué es |
|---|---|
| `--title` | Obligatorio, de 1 a 60 caracteres ya limpio |
| `--body` | Opcional; hasta 4 líneas y 240 caracteres; `-` lee el stdin |
| `--kind` | `info` (default, el color de acento), `ok` (verde), `warn` (ámbar); otro valor es `400` |
| `--ttl` | Segundos, recortado a 10..3600, default 300 |
| `--wake` | De noche, enciende la pantalla 30 segundos |
| `--id` | `[A-Za-z0-9_.:-]` hasta 64. El mismo id **reemplaza** al aviso que lo tiene y reinicia su tiempo; sin id el daemon inventa `n-<contador>` |
| `--source` | Quién lo manda, hasta 24 caracteres, se muestra como "de <source>"; default `hermes` |

Entorno: `CLAUDE_BUDDY_URL` (default `http://buddy:8766`; el daemon solo acepta en `Host` la IP a la que está atado o el nombre del propio Pi) y `CLAUDE_BUDDY_TOKEN_FILE` (default `~/.config/claude-buddy/hermes.token`; archivo regular, tuyo, 0600, 16 caracteres o más).

Códigos de salida: 0 el buddy contestó (su JSON queda en stdout); 1 no se pudo entregar (una frase fija en stderr con el nombre de la excepción o el estado HTTP, nunca el token ni el texto); 2 mal uso. Toda la llamada tarda 3 segundos como máximo y nunca se reintenta: **no reintentes tras un 403**, cinco tokens malos en un minuto bloquean a ese cliente en todas las rutas, hooks incluidos. Con `curl` el token queda en `ps`, por eso se prefiere el script. `--insecure-url-ok` levanta la regla de la URL y la revisión de la dirección.

## Del lado HTTP

`POST /notice` `{"title","body?","kind?","ttl?","wake?","id?","source?"}` -> `200 {"ok":true,"id":...,"active":n}`. Las claves desconocidas se ignoran; un tipo o forma equivocados es `400`. Como mucho **3 activos**: un cuarto saca al más viejo (`evicted`). El presupuesto es de 30 pedidos por minuto por cliente, aparte del de los hooks. `GET /notice/<id>` devuelve el estado (los terminados se recuerdan 1 hora, hasta 50); `DELETE /notice/<id>` lo retira (inofensivo repetido o con un id desconocido).

`wake: true` despierta la pantalla en **cada** POST, aunque el aviso no haya cambiado, a propósito (así se puede escalar uno que esperaba). Quien lo reenvía más seguido que cada 30 segundos mantiene el panel prendido; solo lo acotan el token y el presupuesto de 30 por minuto.

## En la pantalla

Una fila de 44 px con una campana, el título y la edad, teñida según el tipo (una marca de visto o de exclamación también señala `ok` y `warn`, así el color no es la única señal). Tocala para ver el detalle: título, "Aviso · de <source> · hace <edad>" y el cuerpo, con **Listo** y **Volver**. Un aviso nuevo o cambiado pone a la mascota en atención 4 segundos, nunca en la primera vista tras cargar, con la pantalla oscura, bajo un permiso ni bajo el código de emparejar. Un permiso pendiente siempre conserva la pantalla.

De noche, sin `--wake` el aviso espera y está ahí la próxima vez que la pantalla se despierte. De día `--wake` no cambia nada visible.

## Privacidad

El texto de un aviso (título, cuerpo, fuente, ni siquiera el id) no se escribe en ningún registro ni en un error; el daemon solo registra "aviso: <tipo>, N activos". La página lo pone con `textContent`. `/notice` y `/dismiss` nunca aprueban ni deniegan nada.
