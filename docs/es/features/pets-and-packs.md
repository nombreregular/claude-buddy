# La mascota (Mochi) y los packs de personajes

El personaje de Buddy es **Mochi**: una gota blandita en pixel art con cachetes y sin cosas pegadas. Es un personaje propio del proyecto (no es Clawd ni ninguna mascota conocida). Tiene siete ánimos y toda la pantalla toma el color de su ánimo.

## Ánimos

| Ánimo | Color | Cuándo |
|---|---|---|
| `sleep` | lavanda `#A78BFA` | Nadie conectado, o Bluetooth no listo |
| `idle` | arena `#FF9E7A` | Conectado y tranquilo |
| `busy` | menta `#4FD1C5` | Al menos una sesión corriendo |
| `attention` | amarillo `#FFCF5C` | Hay un permiso pendiente (de cualquier fuente), o una sesión te espera |
| `celebrate` | lima `#8BE76A` | Acaba de terminar un turno, o subió de nivel (3 s) |
| `dizzy` | gris azul `#A9B8CF` | Cinco toques rápidos a la mascota |
| `heart` | rosa `#FF8FA3` | Aprobaste en menos de 5 segundos (2 s), o un toque a la mascota |

Prioridad: primero un estado de un solo disparo (celebrar al subir de nivel, corazón por una aprobación rápida); luego un pedido pendiente; luego sin conexión; luego sesiones esperando; luego un turno terminado; luego sesiones corriendo; y por último tranquilo. La mascota sube de nivel cada **50.000 tokens de salida** (el desktop informa los tokens desde que arrancó; el daemon cuenta los aumentos, así que reiniciarlo no vuelve a acreditar una sesión entera). El nombre, el dueño, el nivel y las aprobadas y denegadas se guardan en `state.json`.

## Tocar a la mascota

Un área transparente sobre la mascota (192x196 px) responde a los toques, sin ir al servidor y sin guardar nada:

- Dormida, el primer toque la despierta (`idle` 2,5 s; los toques de esa ventana valen como corazones).
- Despierta (cualquier ánimo menos `attention`), un toque pone `heart` 2,5 s y otro toque reinicia el plazo.
- Cinco toques que reaccionan en 2 s la marean (`dizzy` 3 s) y los toques se ignoran hasta que termina. Después vuelve el ánimo real del servidor.
- Cada toque aceptado la aprieta 160 ms. La píldora de ánimo, el titular y los contadores siempre dicen el ánimo real, no el que puso el toque.
- Con un permiso en pantalla el área es inerte: un dedo grande que roza debajo de los pies de la mascota no puede caer sobre Denegar.

## Packs

La página dibuja el **pack de personaje activo** y, si no hay ninguno, usa su propio Buddy en SVG. Un pack es una carpeta plana: `manifest.json` más archivos GIF (o PNG/WebP/JPEG). Los packs oficiales de Hardware Buddy son GIF de 96 px; la página los muestra a 192 px (2x, píxeles parejos).

```json
{
  "name": "buddy",
  "render": "pixel",
  "colors": {"body": "#FF9E7A", "bg": "#0E0D14", "text": "#F3EEF7", "textDim": "#9A93A4", "ink": "#1B1626"},
  "accents": {"sleep": "#A78BFA", "idle": "#FF9E7A", "busy": "#4FD1C5", "attention": "#FFCF5C", "celebrate": "#8BE76A", "dizzy": "#A9B8CF", "heart": "#FF8FA3"},
  "states": {"sleep": "sleep.gif", "idle": ["idle_0.gif", "idle_1.gif", "idle_2.gif"], "busy": "busy.gif",
             "attention": "attention.gif", "celebrate": "celebrate.gif", "dizzy": "dizzy.gif", "heart": "heart.gif"}
}
```

`states` asigna a cada ánimo un archivo o una lista; si falta un ánimo, la página decide el respaldo. `colors` (`bg`, `text`, `textDim`, `body`) son los únicos valores que entran desde un pack y el servidor se queda solo con `#rrggbb`. `render` y `accents` son extensiones de este proyecto al manifest (`render: pixel` para pixel art nítido, un color de acento por ánimo).

- **Las imágenes** se sirven como `/character/<pack>/<archivo>`, y solo los archivos que el manifest nombra. Cada URL lleva `?v=<rev>`, un hash del manifest y del nombre, tamaño y fecha de modificación de cada archivo que lista: si reemplazás un GIF con el mismo nombre, la pantalla refresca sus imágenes sola, sin un cuadro vacío y sin relanzar el kiosco.
- **Mandar un pack desde Claude desktop:** arrastrá la carpeta a la ventana Hardware Buddy. Viaja por BLE (límites en [protocol.md](../protocol.md#recibir-un-pack-de-personaje): 1,8 MB, 64 archivos, nombres planos), se valida, se instala de forma atómica en `<state-dir>/characters/<nombre>` y queda activo.
- **Instalarlo a mano:** copiá la carpeta a `<state-dir>/characters/<nombre>` y poné `"character": "<nombre>"` en `state.json` con el daemon detenido.

## El generador de Mochi

`tools/make_pixel_buddy.py` dibuja a Mochi de forma procedural en una grilla de 32x32, escalada 3x a GIF transparentes de 96 px, con los siete ánimos. Cada cuadro se revisa tras decodificarlo (tamaño, transparencia, un solo cuerpo, extras lejos de la cabeza) y la corrida falla si algún cuadro rompe una regla. Necesita Pillow (el daemon nunca lo importa).

```bash
python3 tools/make_pixel_buddy.py characters/buddy-pixel                    # el pack
python3 tools/make_pixel_buddy.py characters/buddy-pixel --sheet sheet.png  # y una hoja de contacto
```

`tests/test_pixel_buddy.py` exige que la carpeta del repo sea idéntica a lo que dibuja el generador, así que si cambiás el dibujo, corrélo de nuevo y copiá el pack al Pi. El mismo generador, con su gancho `COSTUME`, hace el [closet](closet.md).
