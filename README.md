# Canal Ofertas Bot

> Publica ofertas de Amazon en tu grupo o canal de Telegram sin apenas esfuerzo. Envías el enlace de un producto, revisas la imagen y el texto, y decides si quieres **publicar** o **programar** la oferta.

## 🚀 Primeros pasos

### 1. Crea tu bot en Telegram

1. Abre [@BotFather](https://t.me/BotFather) en Telegram y envía `/newbot`. Sigue las instrucciones (un nombre y un username acabado en `bot`).
2. BotFather te devolverá el token del bot. Guárdalo: irá en la variable `TELEGRAM_BOT_TOKEN`.
3. Para conocer tu propio chat ID (lo necesitas para `TELEGRAM_OWNER_ID`), usa el bot  [@userinfobot](https://t.me/userinfobot).
4. Para conocer el ID del grupo o canal de Telegram donde quieres publicar las ofertas (lo necesitas para `TELEGRAM_CHANNEL_ID`), usa el bot  [@userinfobot](https://t.me/userinfobot).
5. Añade tu bot al grupo o canal de destino. En un canal, hazlo administrador y dale permiso para publicar y eliminar mensajes. En un grupo, asegúrate de que puede enviar fotos.

### 2. Arranca el contenedor

Copia este `docker-compose.yml`, rellena las 3 variables imprescindibles y levántalo:

```yaml
services:
  canal-ofertas-bot:
    image: danielterreros/canal-ofertas-bot:latest
    container_name: canal-ofertas-bot
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    stop_grace_period: 60s
    tmpfs:
      - /tmp:size=33554432,mode=1777
    environment:
      - TELEGRAM_BOT_TOKEN= # Token del bot de Telegram.
      - TELEGRAM_OWNER_ID= # ID de usuario de Telegram.
      - TELEGRAM_CHANNEL_ID= # ID del grupo o canal de Telegram.
      - TZ=Europe/Madrid # Tu zona horaria.
     # - IMAGE_BRAND_TEXT=TU MARCA # Texto de marca.
     # - IMAGE_BRAND_COLOR=#172433 # Color del texto de marca.
     # - IMAGE_CURRENT_PRICE_COLOR=#008940 # Color del precio actual.
     # - IMAGE_PREVIOUS_PRICE_COLOR=#737b83 # Color del precio anterior.
     # - IMAGE_DISCOUNT_BADGE_COLOR=#e34538 # Color de la etiqueta de descuento.
     # - IMAGE_DEAL_TYPE_BADGE_COLOR=#188451 # Color de la etiqueta del tipo de oferta.
     # - SHOW_PROMOTION_IN_IMAGE=true # Muestra el tipo de oferta en la imagen.
     # - SHOW_PROMOTION_IN_CAPTION=true # Muestra el tipo de oferta en el mensaje.
     # - PUBLICATION_FOOTER= # Firma opcional: 🗣️ Visto en @canal.
     # - SCHEDULE_PERMISSIONS=own # own: cada usuario gestiona sus programadas; all: todos gestionan todas.
    volumes:
      - bot-data:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  bot-data: {}
```

```sh
docker compose up -d
```

## ❓ Cómo se usa

1. Abre el chat privado con tu bot y envía `/start`.
2. Envíale un enlace de Amazon.es.
3. Revisa los datos. Si falta algo, pulsa **Editar**. También puedes quitar la foto de la imagen o cambiarla manteniendo la plantilla, o sustituir completamente la imagen sin conservar la plantilla. 
4. Una vez enviada la edición puedes comprobar la imagen y el texto.
5. Pulsa **Publicar** o **Programar**.

El menú `/start` también permite consultar borradores, publicaciones programadas, tus permisos, el estado del bot y la ayuda.

Si Amazon no devuelve algunos datos, el bot te pedirá que los completes manualmente.

### Comandos

| Comando | Para qué sirve |
| --- | --- |
| `/start` | Abre el menú principal. |
| `/help` | Muestra todos los comandos y ejemplos. |
| `/id` | Muestra tu ID personal. |
| `/datos` | Muestra los datos del borrador. |
| `/editar` | Corrige o completa los datos. |
| `/previa` | Prepara la imagen y el texto para revisarlos. |
| `/programar DD/MM/AAAA HH:MM` | Programa la vista previa para esa fecha y hora. |
| `/programadas` | Muestra las publicaciones pendientes. |
| `/anular ID` | Anula una publicación pendiente si tienes permiso. |
| `/status` | Muestra el estado del bot. |
| `/cancelar` | Descarta el borrador actual, sin anular programaciones. |

## 📝 Variables de entorno

Estas son las variables que aparecen en el Compose. Puedes cambiar estos valores en `docker-compose.yml`. Para activar una opción comentada, elimina `#` al principio de su línea:

| Variable                              | Para qué sirve                                                                                                                                                                                    |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN=`                 | **Imprescindible.** Token del bot de Telegram proporcionado por @BotFather.                                                                                                                       |
| `TELEGRAM_OWNER_ID=`                  | **Imprescindible.** ID del usuario de Telegram autorizado para usar el bot. Para autorizar a varios usuarios, separa sus IDs con comas, sin espacios.                                             |
| `TELEGRAM_CHANNEL_ID=`                | **Imprescindible.** ID numérico negativo del grupo o canal de Telegram donde se publicarán las ofertas.                                                                                           |
| `TZ=Europe/Madrid`                    | Zona horaria del calendario y las fechas.                                                                                                                                                         |
| `IMAGE_BRAND_TEXT=TU MARCA`           | Muestra el texto de marca en la imagen generada. Si lo dejas en blanco o sin esta variable, no aparece texto de marca.                                                                            |
| `IMAGE_BRAND_COLOR=#172433`           | Color del texto de marca en formato HEX. Consultar en: [htmlcolorcodes.com](https://htmlcolorcodes.com/es/)                                                                                       |
| `IMAGE_CURRENT_PRICE_COLOR=#008940`   | Color del precio actual en formato HEX. Consultar en: [htmlcolorcodes.com](https://htmlcolorcodes.com/es/)                                                                                        |
| `IMAGE_PREVIOUS_PRICE_COLOR=#737b83`  | Color del precio anterior en formato HEX. Consultar en: [htmlcolorcodes.com](https://htmlcolorcodes.com/es/)                                                                                      |
| `IMAGE_DISCOUNT_BADGE_COLOR=#e34538`  | Color de la etiqueta de descuento en formato HEX. Consultar en: [htmlcolorcodes.com](https://htmlcolorcodes.com/es/)                                                                              |
| `IMAGE_DEAL_TYPE_BADGE_COLOR=#188451` | Color de la etiqueta del tipo de oferta en formato HEX. Consultar en: [htmlcolorcodes.com](https://htmlcolorcodes.com/es/)                                                                        |
| `SHOW_PROMOTION_IN_IMAGE=true`        | Muestra el tipo de oferta en la imagen.                                                                                                                                                           |
| `SHOW_PROMOTION_IN_CAPTION=true`      | Muestra el tipo de oferta en el mensaje.                                                                                                                                                          |
| PUBLICATION_FOOTER=                   | Permite añadir una firma o un enlace del canal donde se publica la oferta. Por defecto: sin firma.                                                                                                |
| `SCHEDULE_PERMISSIONS=own`            | `own`: cada persona ve y anula solo sus programaciones. <br>`all`: las personas autorizadas pueden ver y anular las de todos. En `TELEGRAM_OWNER_ID=` debe haber al menos dos IDs de usuario.<br> |

Estas son variables opcionales que no aparecen en el Compose. Puedes añadir estos valores en `docker-compose.yml`:

| Variable                        | Valor predeterminado               | Para qué sirve                                                                                           |
| ------------------------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `PREVIEW_TTL_SECONDS`           | `86400`                            | Caducidad del borrador desde su creación, en segundos. Por defecto: 24 h.                                |
| `ENABLE_PUBLISH`                | `true`                             | Permite publicar y programar. `false` desactiva ambas acciones.                                          |
| `AMAZON_FETCH_HTML`             | `true`                             | Intenta obtener los datos desde Amazon. `false` deja la preparación manual.                              |
| `SCHEDULE_MAX_DELAY_SECONDS`    | `900`                              | Retraso máximo en segundos para enviar una programación cuya hora ya pasó. Por defecto: 15 minutos.      |
| `SCHEDULER_INTERVAL_SECONDS`    | `5`                                | Cada cuántos segundos revisa las publicaciones programadas.                                              |
| `SCHEDULE_HISTORY_DAYS`         | `0`                                | Días que conserva registros de publicaciones terminadas. Admite hasta 30 días.                           |
| `REPEAT_WARNING_DAYS`           | `7`                                | Días durante los que avisa de un producto repetido. Por defecto: 7 días.                                 |
| `HTTP_TIMEOUT_SECONDS`          | `20`                               | Tiempo máximo en segundos de espera de una petición web.                                                 |
| `TELEGRAM_POLL_TIMEOUT_SECONDS` | `25`                               | Tiempo en segundos que una consulta espera nuevos mensajes de Telegram. No retrasa la respuesta del bot. |
| `TELEGRAM_RETRY_LIMIT`          | `3`                                | Reintentos ante ciertos fallos temporales al enviar publicaciones programadas.                           |
| `MAX_DOWNLOAD_BYTES`            | `5000000`                          | Tamaño máximo de descarga de HTML o fotos, **en bytes**.                                                 |
| `MAX_REDIRECTS`                 | `6`                                | Número máximo de redirecciones de un enlace.                                                             |
| `MAX_IMAGE_PIXELS`              | `20000000`                         | Tamaño máximo de una imagen, **en píxeles**.                                                             |
| `LOG_LEVEL`                     | `INFO`                             | Detalle de los registros: `DEBUG`, `INFO`, `WARNING` o `ERROR`.                                          |
| `DATA_DIR`                      | `/data`                            | Carpeta **interna del contenedor** para la base de datos. Normalmente no se cambia.                      |
| `FONT_DIR`                      | `/usr/share/fonts/truetype/dejavu` | Carpeta **interna del contenedor** con las fuentes. Normalmente no se cambia.                            |

### Permisos de las publicaciones programadas

`SCHEDULE_PERMISSIONS` decide qué publicaciones pendientes puede **ver y anular** cada persona autorizada.

|Valor|Funcionamiento|
|---|---|
|`own`|Cada persona ve y anula solo las publicaciones que ha programado ella. Es el valor predeterminado.|
|`all`|Todas las personas autorizadas pueden ver y anular las publicaciones programadas por cualquiera de ellas.|

Para cambiarlo, edita esta línea de `docker-compose.yml`:

```
- SCHEDULE_PERMISSIONS=own
```

Sustituye `own` por `all` y recrea el contenedor.

Esta opción no autoriza a personas nuevas: los usuarios permitidos siguen siendo los ID indicados en `TELEGRAM_OWNER_ID`. Tampoco cambia el grupo o canal de destino.

## 🗂️ Datos

Por defecto, Docker guarda los borradores y las publicaciones programadas en el volumen `bot-data`. Los datos permanecen al recrear el contenedor. No necesitas crear una carpeta `data/`.

Si prefieres guardar los datos en una carpeta de tu máquina, sustituye `- bot-data:/data` por `- /ruta/elegida/data:/data` y elimina la declaración final del volumen `bot-data`. En Linux, esa carpeta debe permitir escribir al usuario interno `10001:10001`. Si ya tienes datos en el volumen, cópialos antes de cambiar el montaje.

Los borradores caducan. Las publicaciones solo pueden programarse para los próximos 30 días y las que sigan pendientes 30 días después de programarlas caducan. Docker guarda sus registros fuera de `/data`; el Compose limita cuánto espacio ocupan.

No ejecutes dos instancias con el mismo token de Telegram a la vez.

## Licencia

Este proyecto se distribuye bajo la [Licencia Pública General de GNU, versión 3 (GPLv3)](LICENSE). Consulta el archivo para conocer las condiciones de uso, modificación y redistribución.