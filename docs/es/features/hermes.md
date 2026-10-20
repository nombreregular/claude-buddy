# Hermes Agent y Claude Code en la terminal (opcional)

Los dos usan la misma puerta: la [API local](../local-api.md) (`POST /approval`), que mete un pedido en la **misma cola** que contesta la pantalla. En la pantalla el pedido lleva el nombre de quien lo hace (`hermes · terminal`, `cli · Bash`) y Claude y los demás se atienden en orden de llegada. Solo un toque (o las teclas `a`/`d` en el Pi) decide.

Preparación común: arrancá el daemon con `--hermes-bind <ip-de-tailscale> --hermes-token-file ...` y copiá el token a la máquina que pide (ver el README, "La API local").

## Plugin de Hermes Agent

Hermes Agent ya le pregunta a un humano antes de correr un comando peligroso (por Discord, la TUI, ...). `hermes-plugin/claude-buddy/` es un plugin de Hermes que registra un transporte de aprobación llamado `buddy`: el mismo pedido va a `POST /approval` del Pi, sale en la pantalla como `hermes · terminal` con el comando y la explicación de Hermes, y el toque vuelve como la decisión (`once` o `deny`). El plugin solo presenta el pedido: qué cuenta como peligroso, las opciones, el tiempo máximo y la última palabra siguen siendo de Hermes (`hermes_cli/approval_transport.py` en su código). Si la pantalla no puede contestar (buddy apagado, sin token, nadie toca en `timeout` segundos) el plugin falla a propósito y, con `transport_fallback: builtin`, Hermes pregunta como antes. **Nunca aprueba ni deniega solo.** Solo librería estándar.

Instalalo en la máquina donde corre Hermes, con el usuario que corre Hermes (`<usuario-hermes>`):

```bash
# copiá la carpeta del plugin a la carpeta de plugins de Hermes de ese usuario
rsync -a --delete hermes-plugin/claude-buddy/ <equipo-hermes>:<home-de-hermes>/plugins/claude-buddy/
```

El token de la API tiene que quedar en el equipo de Hermes a nombre de `<usuario-hermes>`, con permisos 0600, sin pasar por una terminal compartida. Por ejemplo desde tu compu, encadenado para que nunca se escriba en disco local:

```bash
ssh <usuario>@buddy.local 'cat ~/.config/claude-buddy/hermes.token' \
  | ssh <equipo-hermes> 'sudo install -m 600 -o <usuario-hermes> -g <usuario-hermes> /dev/stdin <home-de-hermes>/claude-buddy.token'
```

Después, como `<usuario-hermes>`:

```bash
hermes plugins doctor claude-buddy        # import y registro OK
hermes plugins enable claude-buddy
hermes config set plugins.entries.claude-buddy.settings.url http://<ip-de-tailscale>:8766
hermes config set plugins.entries.claude-buddy.settings.timeout 120
hermes config set security.approval.transport buddy
hermes config set security.approval.transport_fallback builtin
```

| Setting (`plugins.entries.claude-buddy.settings.*`) | Default | Qué es |
|---|---|---|
| `url` | `http://buddy.local:8766` | La API local del Pi (mejor la IP de Tailscale) |
| `token_file` | `~/.hermes/claude-buddy.token` | El token de la API, permisos 0600 y del usuario de Hermes; si no cumple, el plugin no lo usa |
| `timeout` | `120` | Segundos que tiene la pantalla antes de que Hermes pregunte por otro lado (5 a 3600) |

Cómo se ve: Hermes quiere correr algo peligroso, aparece en la pantalla, tocás **Aprobar una vez** o **Denegar** y Hermes sigue. Si nadie toca dentro del tiempo, el pedido se va de la pantalla y Hermes pregunta por su respaldo. `hermes approvals test "<comando>"` dice, sin correrlo, si un comando pediría aprobación. Para probar la cañería sin tocar la config de Hermes usá el `curl` de [local-api.md](../local-api.md#delete-approvalid). Ojo: `hermes chat -q --oneshot` es desatendido y deniega solo sin consultar transportes: probá con la TUI interactiva o un canal de chat.

## Avisos de Hermes (el skill `buddy-pantalla`)

`hermes-plugin/skills/buddy-pantalla/SKILL.md` es un skill de Hermes que enseña cuándo y cómo dejar un aviso corto en la pantalla: cuando algo largo terminó o falló, nunca para charla, ids estables, `--wake` solo para lo que no puede esperar a la mañana y qué hacer ante un `403` o un `429`. Espera un comando llamado `buddy-notify` en el equipo de Hermes. Un envoltorio junto a una copia de `tools/notify.py`:

```sh
#!/bin/sh
export CLAUDE_BUDDY_URL=http://<ip-de-tailscale>:8766
export CLAUDE_BUDDY_TOKEN_FILE="$HOME/.hermes/claude-buddy.token"
exec python3 -I "$(dirname "$0")/buddy-notify.py" "$@"
```

Copiá `tools/notify.py` como `buddy-notify.py` al lado (de nuevo después de cualquier cambio del original) y copiá la carpeta del skill a la carpeta de skills de Hermes. La política de aprobaciones de Hermes decide si puede correr `buddy-notify` sin preguntar cada vez. Detalles del comando: [notices.md](notices.md).

## Claude Code en la terminal (hook CLI)

`tools/cli_hook.py` es un hook `PermissionRequest` (solo librería estándar, corre con el Python 3.9 de macOS). Manda el pedido a la misma API con `"source": "cli"`: sale como `cli · Bash`, "Claude Code pide permiso", con el comando y la carpeta. Claude Code solo llama al hook cuando **iba a mostrar su propio diálogo**, así que tus reglas `allow`, `acceptEdits` y `auto` siguen igual que antes.

**Solo un toque en la pantalla aprueba o deniega.** Cualquier otra cosa (Pi apagado, la API cerrada, sin token, un token que otros pueden leer, nadie toca en 60 s, una respuesta rara, Ctrl-C) termina en el **prompt normal de la terminal**, y si el pedido ya estaba en la pantalla, se va solo. `AskUserQuestion` y `ExitPlanMode` se quedan en la terminal. En las sesiones de **Claude desktop** el hook no hace nada (`CLAUDE_CODE_ENTRYPOINT=claude-desktop`): leen el mismo `settings.json`, pero sus pedidos ya llegan por Bluetooth. Una terminal abierta desde una sesión de desktop hereda esa variable.

**La terminal no espera al buddy.** Claude Code muestra su diálogo mientras el hook espera; contestá donde te quede a mano y gana lo primero. Si contestás en la terminal, el hook ve aparecer el resultado de la herramienta en el transcript de la sesión y quita el pedido de la pantalla.

**Siempre.** Cuando Claude Code propone una regla para no volver a preguntar, la pantalla agrega un tercer botón, **Siempre**, y muestra la regla entera arriba, por ejemplo "Siempre guarda Bash(npm run *) en este proyecto". Ojo: la regla puede ser más amplia que el comando. Tocarlo aprueba y le devuelve a Claude Code esa misma sugerencia sin cambiarla, y Claude Code la guarda como si la hubieras elegido en la terminal (`.claude/settings.local.json` del proyecto). Solo se ofrecen reglas `allow` simples; carpetas nuevas o cambios de modo se quedan en la terminal.

Registralo en `~/.claude/settings.json` (si ya tenés `"hooks"`, agregá solo la llave `PermissionRequest`):

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/cli_hook.py\" 2>/dev/null; exit 0",
            "timeout": 90
          }
        ]
      }
    ]
  }
}
```

El `timeout` de Claude Code tiene que ser mayor que la espera del hook (60 s por defecto). El `2>/dev/null; exit 0` hace que un hook que no arranca (el archivo se movió) deje la pregunta en la terminal en vez de negar el permiso. Para probarlo sin tocar tus settings: guardá ese JSON aparte y corré `claude --settings ese-archivo.json`.

| Variable | Flag | Default |
|---|---|---|
| `CLAUDE_BUDDY_URL` | `--url` | `http://buddy:8766` |
| `CLAUDE_BUDDY_TIMEOUT` | `--timeout` | `60` segundos esperando el toque (1 a 600) |
| `CLAUDE_BUDDY_TOKEN_FILE` | `--token-file` | `~/.config/claude-buddy/hermes.token` |
| `CLAUDE_BUDDY_HOOK_LOG` | `--log` | ninguno; si lo das, una línea por pedido (nunca el token) |

El flag gana sobre la variable. La URL por defecto es el nombre del Pi en Tailscale; si la API escucha en tu LAN usá `CLAUDE_BUDDY_URL=http://buddy.local:8766`. Si el log dice `Errno 65` (`No route to host`) con el Pi en tu LAN, dale permiso de **Red local** a tu terminal (ver la sección de problemas del README).
