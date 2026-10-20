# Modo noche

De noche la pantalla se atenúa **bajando el backlight real** del display del Pi, porque un velo negro sobre un LCD deja la luz de fondo prendida. Un toque la despierta 30 segundos; un permiso, un código de emparejar o un aviso mandado con `wake` la despiertan solos. Viene prendido por defecto.

## Opciones

| Opción | Default | Qué hace |
|---|---|---|
| `--night HH:MM-HH:MM` u `off` | `23:00-07:00` | La franja. Inicio inclusivo, fin exclusivo, puede cruzar la medianoche, inicio igual a fin significa siempre de noche. Un valor inválido sale con código 2 |
| `--night-level N` | 12 | Luz de la pantalla de noche, de 4 hasta el máximo del panel (nunca se apaga del todo). De día va al máximo |
| `--backlight auto\|off\|RUTA` | `auto` | `auto` toma el primer panel escribible bajo `/sys/class/backlight/`; `off` hace que la página se oscurezca sola con un velo (78% de negro); o la carpeta de uno (con `brightness` y `max_brightness`) |

La hora es la local de tu compu (se la cuenta al buddy por BLE, la misma del reloj de la pantalla) y, si nunca lo hizo, la del propio Pi. Con `--night off`, `--no-ui` o el puerto de la pantalla ocupado, el modo noche y el backlight se apagan por completo y el daemon nunca toca el brillo.

## Qué despierta la pantalla (30 segundos, `AWAKE_S`)

- Un toque en la pantalla oscura (`POST /wake`). Ese toque no hace nada más: no abre una fila, no aprieta un botón y no deja eco.
- Un permiso pendiente (30 segundos más después de que el último se resuelve o vence) y un código de emparejar en pantalla. Los botones de aprobar se siguen armando a los 0,6 s.
- Estando despierta de noche, cualquier toque sobre la pantalla la mantiene despierta (la página avisa, a lo sumo una vez cada 5 segundos), así podés seguir leyendo un hilo.
- Un aviso con `wake: true` ([avisos](notices.md)).

Manda el daemon: la vista trae `night`, `dim` y `backlight`, y el daemon republica solo cuando se acaba el despertar o empieza o termina la franja. Durante 350 ms después de despertar (alargados a 600 ms desde que un dedo apoya dentro de esa guarda) la capa oscura sigue atajando toques. Un permiso nunca queda tapado ni oscurecido.

## El panel y la salida

El backlight se escribe solo cuando cambia el nivel. Un error de escritura desactiva la atenuación y la página pasa al velo; si la escritura que falló era la de vuelta a día, la reintenta una vez por segundo hasta 60 veces para no dejar el panel tenue. Un panel cuya "noche" no queda por debajo de su "día" (un on/off por GPIO) no se usa. El daemon restaura el brillo de día lo primero al detenerse (SIGTERM de systemd, SIGINT, la tecla `q`, SIGHUP, un arranque de Bluetooth que falla). SIGKILL y un corte de luz no restauran: el panel queda tenue hasta que el servicio reinicia y el primer sync escribe el nivel correcto.

El servicio necesita permiso de escritura sobre el backlight: en Pi OS el usuario tiene que estar en el grupo `video`. Tras reiniciar, buscá la línea "Modo noche ..." en `journalctl --user-unit claude-buddy`; si dice que no encontró luz de pantalla que mover, al servicio le falta el grupo y solo hay velo.

No corras la suite de pruebas en un Pi de noche: las pruebas que levantan el daemon real no pasan `--backlight off` y atenuarían el panel real un rato.

Vista previa sin daemon: `ui/index.html?demo=night`, `?demo=night-overlay` (el velo) y `?demo=night-ask` (un permiso de noche).
