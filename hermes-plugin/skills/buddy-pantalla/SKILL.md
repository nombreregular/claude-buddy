---
name: buddy-pantalla
description: "Usar cuando haya que dejarle al dueño un aviso corto en la pantalla del Buddy (el Pi con pantalla táctil): algo terminó, falló o necesita su atención y no conviene esperar a que mire Discord. También para saber si lo vio o para retirarlo."
version: 1.0.0
---

# Avisos en la pantalla del Buddy

El Buddy es la pantalla táctil que el dueño tiene en su cuarto (un Raspberry Pi). Con `buddy-notify` dejás un mensaje corto ahí: sale arriba de la lista, con una campana, se cierra con un toque ("Listo") y caduca solo. No reemplaza a Discord: es para lo que vale un vistazo.

## Cuándo avisar

- Terminó algo largo que el dueño estaba esperando (una tanda, un despliegue, una copia), o falló algo que no puede esperar.
- Pasó algo de la casa o de la red que él quiere saber ya.
- El dueño te pidió "avisame en la pantalla cuando X".

Cuándo NO: charla, respuestas a lo que acaba de preguntarte (eso va por el canal donde habla), repetir algo que ya avisaste, ruido de rutina. Un aviso que se repite se ignora.

## Cómo

```bash
buddy-notify --title 'La tanda terminó' --body '37 de 40 pasaron; 3 esperan revisión' --kind ok --ttl 600 --id tanda-21
buddy-notify --status tanda-21      # {"id":"tanda-21","state":"shown"}
buddy-notify --withdraw tanda-21    # lo saca de la pantalla
```

- `--title` (obligatorio, hasta 60 caracteres) dice lo esencial; `--body` es opcional (hasta 4 líneas y 240 caracteres).
- `--kind`: `info` (por defecto), `ok` (salió bien, verde) o `warn` (algo falló o pide atención, ámbar).
- `--ttl`: segundos que vive (10 a 3600, por defecto 300). Poné lo que dure de verdad la utilidad del aviso.
- `--id`: un nombre estable ("tanda-21", "backup-noche") reemplaza el aviso anterior con ese nombre en vez de apilar otro. Usalo siempre que el aviso se pueda actualizar; elegí vos los ids si vas a preguntar `--status`.
- `--source`: de quién viene (por defecto `hermes`).
- Como mucho hay 3 avisos a la vez; el cuarto saca al más viejo.

## Estado

`--status ID` dice `shown` (sigue en pantalla), `dismissed` (el dueño lo cerró con Listo, o sea que lo vio), `expired` (venció), `withdrawn` (lo retiraste), `evicted` (lo desplazó otro) o `unknown` (el Buddy no lo conoce, por ejemplo tras reiniciarse: los avisos viven solo en memoria).

## Despertar la pantalla (`--wake`)

De noche (23:00 a 07:00) la pantalla está tenue. Un aviso sin `--wake` espera y el dueño lo ve la próxima vez que la toque. Con `--wake` la enciende 30 segundos.

- Usalo solo para algo que de verdad no puede esperar a la mañana (algo crítico de la casa que falló, o algo que el dueño te pidió que lo despertara).
- Nunca lo mandes en bucle ni reenvíes el mismo aviso con `--wake`: cada envío vuelve a encenderla.
- De día `--wake` no cambia nada.
- Si dudás, no lo pongas.

## Qué escribir

- Es una pantalla en el cuarto del dueño: nada de secretos, contraseñas, tokens ni datos de terceros.
- Frases cortas en español (voseo). El título dice qué pasó; el cuerpo, lo mínimo para decidir si hace falta abrir algo.
- No metas HTML ni formato: es texto plano.

## Si falla

`buddy-notify` imprime en stdout el JSON del Buddy y sale con 0. Con salida 1 escribe una frase fija en stderr:

- Un 403 significa que el token no sirvió: **no reintentes** (cinco fallos en un minuto bloquean a Hermes en todo el Buddy, incluidos los permisos). Avisale al dueño por Discord.
- Un 429 significa demasiados avisos (el límite es 30 por minuto): esperá un minuto.
- Una falla de red o un tiempo agotado: probablemente el Pi está apagado o sin Tailscale; no insistas más de una vez.
- Salida 2: mal uso (por ejemplo un título con varias palabras sin comillas). Corregí el comando.

El comando nunca aprueba ni deniega nada y no toca otra cosa que la lista de avisos.
