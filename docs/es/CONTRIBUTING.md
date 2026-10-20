# Contribuir

Gracias por ayudar. Una guía corta para tener el código andando y saber qué le pedimos a un cambio. `CLAUDE.md` (en español) es la referencia larga para quien contribuye y para agentes de código: hechos verificados en hardware, contratos y reglas que no se negocian.

## Correr las pruebas

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

Unas 2.000 pruebas. No necesitan hardware: las de Bluetooth hablan con un `bluetoothd` falso en un D-Bus privado.

- **En el Pi** (Linux con `dbus-daemon`, que Pi OS trae) corre toda la suite. No la corras de noche: las pruebas que levantan el daemon real atenuarían el backlight real un rato (se restaura al salir).
- **En un Mac** se saltan las pruebas de BlueZ y las que piden Linux o D-Bus (`test_cli`, dos de `test_hermes`), unas 80 en total. `test_kiosk` tiene una prueba que falla ahí solo porque las rutas temporales de macOS son demasiado largas para un socket Unix.
- **Las pruebas de página** (`test_ui`, `test_page_*`, `test_closet_e2e`) manejan la página en un **Chromium** headless y se saltan si no hay uno. `tests/page_runner.py` busca `chromium` en el `PATH`, Google Chrome en `/Applications` o el binario de `CLAUDE_BUDDY_CHROMIUM`. Una prueba de página puede salir floja con la máquina cargada: repetila sola antes de culpar a tu cambio.
- `test_pixel_buddy`, `test_closet_tool` y la de punta a punta del closet necesitan Pillow.
- Los hooks tienen que seguir siendo válidos en el Python 3.9 que trae macOS: `/usr/bin/python3 -m py_compile tools/context_hook.py` (sin anotaciones `X | Y`, sin `match`). Varias pruebas los corren con ese intérprete si existe.
- Canario opcional tras actualizar Claude desktop: `CLAUDE_BUDDY_CANARY=1 .venv/bin/python -m pytest tests/test_context_hook.py -k real_desktop` comprueba que los archivos de Claude desktop sigan teniendo las llaves que lee el hook de contexto (solo nombres de llaves, no imprime nada privado).

Una nota que aplica solo si corrés las pruebas desde dentro de una sesión de Claude desktop: ese entorno define variables `CLAUDE*` (por ejemplo `CLAUDE_CODE_ENTRYPOINT`) que hacen fallar a `test_kiosk`. Desarmalas para la corrida: `for v in $(env | grep -E '^CLAUDE' | cut -d= -f1); do unset $v; done`.

## Pi y Mac

El daemon es para el Pi (BlueZ, el backlight, el kiosco de Chromium). En un Mac igual podés trabajar la página, la API local, los hooks, el censo y los generadores. Para ver la página sin daemon abrí `ui/index.html?demo=cycle` en un navegador; para verla con daemon, desplegá al Pi con `rsync` y `systemctl --user restart claude-buddy`, o abrí un túnel con `ssh -L 8765:localhost:8765`.

## Qué le pedimos a un cambio

- **Pruebas con el cambio**, sobre todo si toca `POST /decide`, la API local, los hooks o el manejo de toques de la página.
- **Mantener las reglas de seguridad** ([architecture.md](architecture.md#reglas-de-diseño-que-dan-forma-al-código)): `/decide` es loopback con un token por arranque; la API local nunca aprueba; el texto en la página siempre con `textContent`; los 600 ms de armado de los botones de aprobar se quedan; nada de lo que dice una sesión va a un log.
- **El core y la página sin dependencias** (librería estándar; la única dependencia de ejecución es `dbus-fast`, en el transporte). Sin CDN ni fuentes web.
- Animaciones solo con `transform` y `opacity`.
- Los scripts de hook corren en vivo en cada sesión de Claude Code de quien los usa: editá una copia, probala y movela a su lugar con `mv`, y mantené el `2>/dev/null; exit 0` en todo registro documentado.
- Código y comentarios en inglés; los textos para el usuario pasan por las tablas de i18n ([features/i18n.md](features/i18n.md)). Sin em dashes en los textos.
- Los cambios de diseño siguen `DESIGN.md` (el cuarto a oscuras y la burbuja; píldoras redondas; sin líneas finas ni dashboards).

## Documentación

Los docs en inglés viven en `docs/` y las copias en español en `docs/es/`, con los mismos nombres de archivo. Cuando cambies uno, cambiá el otro.
