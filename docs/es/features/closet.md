# Closet de disfraces (opcional, apagado por defecto)

La mascota puede ponerse un disfraz: gorro de fiesta, corona, gorro de lana, sombrero de mago, gorro de chef, moño, flor u orejas de gato, en siete colores (56 looks). Los disfraces van **horneados en los GIF** (un velo en la página se desfasaría del cuerpo que rebota), se hacen en tu compu y viven en una carpeta de closet **fuera de git**. El daemon sigue siendo solo librería estándar y nunca importa Pillow.

## Hacer disfraces

En tu compu (necesita Pillow):

```bash
python3 tools/make_closet.py ~/closet-buddy --count 8 --sheet closet.png
```

Cada disfraz queda en `~/closet-buddy/<molde>-<color>/`: un pack completo (`manifest.json` más los 9 GIF, unos 60 KB) cuyo manifest trae un bloque `costume` (`name`, `template`, `color`, `base`). Otras opciones: `--seed S`, `--base-name buddy` (el pack que el disfraz viste), `--list` (qué tiene el closet). Cada corrida agrega otros distintos; al llegar a 56 lo dice y sale con 0. `--sheet` dibuja todos los disfraces en los 7 ánimos para mirarlos. Sin Pillow te dice cómo instalarlo y sale con 1.

Para agregar un molde: un `Template(id, name_es, rows, sink)` más en `TEMPLATES` de `tools/closet_costumes.py`.

## Pasarlos al Pi y prenderlo

```bash
rsync -a ~/closet-buddy/ <usuario>@buddy.local:.local/state/claude-buddy/closet/
```

Agregá `--closet auto --closet-every 3` al `ExecStart` del servicio y reinicialo (`auto` es `<state-dir>/closet`; o pasá cualquier carpeta).

| Opción | Qué hace |
|---|---|
| `--closet off\|auto\|RUTA` | Default `off`: no se lee nada, no corre ningún temporizador y el personaje es exactamente el pack activo |
| `--closet-every HORAS` | 0,25 a 168, default 3; 0 elige uno al arrancar y no cambia más |

## Cómo se porta

- El daemon elige al azar entre los disfraces **y "sin disfraz"**, nunca el mismo dos veces seguidas, al arrancar y cada H horas con un solo temporizador. Cada elección relee la carpeta, así que un disfraz copiado después entra en el sorteo.
- Es solo un cambio de vista: nunca cambia el pack activo. Un disfraz se usa únicamente mientras el pack activo se llame como su `base` (`buddy` en la configuración de referencia), así que mantené el closet homogéneo. Un closet que desaparece o se corrompe en marcha vuelve al pack de abajo.
- El closet es la carpeta de alguien y nada de lo que hay adentro se da por bueno: una carpeta con manifest de hasta 64 KB, un bloque `costume` (nombre de hasta 40 caracteres y `base`), estados que apuntan a archivos regulares de hasta 256 KB, un nombre de carpeta `[a-z0-9-]` de hasta 63 caracteres, sin enlaces simbólicos en ningún lado, y como mucho 64 disfraces. Lo demás se descarta y se cuenta una vez por tipo en el registro.
- El registro (sin `-v`) dice "Closet: N disfraces, cambia cada H h", "Closet: descarté N carpetas (tipos)" y cuándo Buddy se pone o se saca un disfraz. Nunca el contenido de un archivo.

Los disfraces son una pieza con contorno de tinta de 1 px, centrada en la cabeza de cada cuadro (siguiendo el rebote y la inclinación del mareo) y que nunca tapa los extras (zetas, corazones, chispas, gotas). El pack Mochi de `characters/buddy-pixel` sale byte por byte igual con o sin el gancho de disfraces del generador.
