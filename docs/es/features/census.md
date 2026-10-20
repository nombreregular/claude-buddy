# Censo de corridas en segundo plano (opcional)

El hook de contexto solo habla cuando una sesión llama una herramienta. Un `claude -p` callado, o un script que no es una sesión de Claude, no aparecería. `tools/census.py` mira la tabla de procesos de tu compu cada 15 segundos y manda una foto a la [API local](../local-api.md) (`POST /census`, mismo token). La pantalla muestra las filas como el grupo **Corridas**, con el nombre de cada una y cuánto lleva corriendo.

Está pensado para macOS (corre como LaunchAgent) y usa solo la librería estándar y el Python 3.9 que trae macOS.

## Qué cuenta como corrida

Un proceso que cumple **todo** esto:

- el programa se llama `claude` y la palabra que sigue es una opción;
- `-p` o `--print` está entre las primeras 80 palabras, y ahí no hay `--sdk-url`;
- nada arriba vive en Claude.app ni es otra corrida (un `claude -p` anidado es de la de afuera);
- lleva más de `min_age_s` (20 s).

Los envoltorios (`bash -c ... claude -p`, `caffeinate`, `ssh`) nunca son corridas; su hijo sí. Los scripts ("trabajos") solo salen si tu `census.json` los nombra.

## Instalar

```bash
tools/census_agent.sh install      # escribe y carga el LaunchAgent com.claude-buddy.census; -n solo muestra el plan
tools/census_agent.sh status       # ¿cargado? pid, último código de salida, token, config; --ping hace un GET /healthz
tools/census_agent.sh restart      # tras actualizar census.py
tools/census_agent.sh uninstall    # lo descarga y borra el plist (--purge también borra el log)
```

El repo no puede estar en Documents, Desktop, Downloads ni iCloud (macOS los protege de los agentes en segundo plano). Nada corre sin `install`. Pasale `--url` a `install` si el buddy no es `http://buddy:8766`: launchd no hereda el entorno de tu shell.

Tu configuración va en `~/.config/claude-buddy/census.json` (fuera de git; el modelo es `tools/census.example.json`). Sin ese archivo el censo solo ve `claude -p`. Llaves: `jobs` (qué nombrar, comparado con el nombre del script o programa de un proceso, nunca con sus argumentos; `regex`, `name`, `min_age_s`), `ignore`, `strip_prefix`, `interval_s`, `max_rows`, `group`, `url`, `token_file`, `insecure_url_ok`.

## Privacidad

Lo que sale de la compu: un nombre (la carpeta de la corrida, o el que tu config le da a un trabajo), los segundos que lleva corriendo, los segundos desde que su transcript cambió, el id de la sesión (para que una corrida y su hilo del hook salgan como una sola fila) y el nombre de la carpeta. **Nunca** una línea de comandos, argumento, entorno, ruta, prompt ni salida: se leen en memoria para clasificar un proceso y se tiran. El token solo viaja a esta compu, la tailnet o loopback; si el nombre de la URL lleva a otra dirección, el censo lo retiene.

## Contrato

`POST /census` `{v:1, source, seq, ttl, more, rows[<=8]}`; cada fila `{id: "run-..."|"job-...", name, group, state:"working", elapsed, idle?, session?, project?}`. Una foto reemplaza a la anterior de su `source` y caduca sola después de `ttl` (4 veces el intervalo), así que si el censo muere, el grupo se va en un minuto. Tipos equivocados dan `400`; los números fuera de rango se recortan. La pantalla dibuja hasta 8 filas, de las cuales hasta 5 son corridas y al menos 3 son hilos. Una corrida y su hilo del hook salen como una sola fila (por sesión, o por la carpeta solo si la corrida no dice su sesión).

Contra un daemon viejo `/census` da `404`; el censo espera 5 minutos y lo vuelve a intentar, así que el orden de despliegue no importa.

## Límites conocidos

El registro `~/.claude/sessions/<pid>.json` es de Claude Code y no está documentado: si cambia, se pierde el cruce exacto por sesión y queda el cruce por carpeta. Una corrida cuyo prompt contiene la palabra `--sdk-url` puede confundir la ventana de 80 palabras. Un `claude -p` que lanza una conversación de desktop como herramienta no se muestra, a propósito.

Opciones (entorno): `CLAUDE_BUDDY_URL`, `CLAUDE_BUDDY_TOKEN_FILE`, `CLAUDE_BUDDY_CENSUS=off` (sale de una vez), `CLAUDE_BUDDY_CENSUS_CONFIG`, `CLAUDE_CONFIG_DIR`. `python3 tools/census.py once --print` hace una pasada e imprime la foto sin mandar nada.
