# Permisos de las publicaciones programadas

La variable `SCHEDULE_PERMISSIONS` decide qué publicaciones pendientes puede **ver y anular** cada persona autorizada.

## 1. Autoriza a los usuarios

Para que una persona pueda utilizar el bot, debes incluir su ID de Telegram en la variable `TELEGRAM_OWNER_ID`.

Para obtener los IDs:

1. Cada persona debe abrir [@userinfobot](https://t.me/userinfobot) en Telegram y enviar `/start`o pulsar **Iniciar**.
2. El bot mostrará su ID numérico.
3. Cada persona debe facilitarte ese número para que lo añadas a la configuración.

Si una persona ya está autorizada, también puede consultar su ID enviando `/id` al chat privado de tu bot.

En el apartado `environment` del Compose, configura `TELEGRAM_OWNER_ID` con los IDs autorizados, **separados por comas y sin espacios**:

```yaml
- TELEGRAM_OWNER_ID=123456789,987654321
```

Los números del ejemplo son ficticios. Sustitúyelos por los IDs reales, incluido el tuyo.

Si tu Compose obtiene esta variable desde un archivo `.env`, modifica su valor en ese archivo:

```env
TELEGRAM_OWNER_ID=123456789,987654321
```

## 2. Configura los permisos de las programadas

| Valor | Funcionamiento |
| --- | --- |
| `own` | Cada persona ve y anula solo las publicaciones que ha programado ella. Es el valor predeterminado. |
| `all` | Todas las personas autorizadas pueden ver y anular las publicaciones programadas por cualquiera de ellas. |

En el apartado `environment` del Compose, añade o activa esta línea:

```yaml
- SCHEDULE_PERMISSIONS=own
```

Sustituye `own` por `all` si quieres que todas las personas autorizadas puedan gestionar las publicaciones pendientes de los demás.

## 3. Aplica los cambios

Guarda la configuración y recrea el contenedor.

Las personas autorizadas podrán abrir el chat privado con tu bot y enviar `/start`.

`SCHEDULE_PERMISSIONS` no autoriza a personas nuevas: los usuarios permitidos siguen siendo los ID indicados en `TELEGRAM_OWNER_ID`. Tampoco cambia el grupo o canal de destino.
