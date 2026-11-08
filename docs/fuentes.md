# Utilizar una fuente personalizada

El bot utiliza **DejaVu Sans** por defecto, pero puedes utilizar otra tipografía para la marca, los precios y las etiquetas de las imágenes generadas.

## 1. Prepara los archivos

Crea manualmente una carpeta `fuentes` en el equipo donde ejecutas Docker, usando tu usuario habitual y antes de desplegar el contenedor. Después deberás indicar la ruta completa de esa carpeta en el apartado `volumes` del Compose.

Copia dentro los archivos `.ttf` de la tipografía elegida y renómbralos:

- Fuente normal: `font-regular.ttf`.
- Fuente negrita: `font-bold.ttf`.

Debes incluir los dos archivos: uno de la fuente normal (Regular) y otro de la fuente en negrita (Bold).

La carpeta y ambos archivos deben poder ser leídos por el usuario del contenedor.

## 2. Configura el Compose

Añade esta línea al apartado `environment` del servicio:

```yaml
- FONT_DIR=/fonts
```

Añade el montaje de la carpeta al apartado `volumes` del servicio, conservando el volumen de datos:

```yaml
volumes:
  - bot-data:/data
  - /ruta/elegida/fuentes:/fonts:ro
```

Sustituye `/ruta/elegida/fuentes` por la ruta real de tu carpeta. Dentro del contenedor, esa carpeta estará disponible como `/fonts`.

## 3. Aplica el cambio

Guarda el Compose y recrea el contenedor.

Crea un borrador nuevo para comprobar la tipografía. Las imágenes ya guardadas conservarán la fuente anterior.

## Volver a la fuente predeterminada

Elimina la variable `FONT_DIR` y el montaje de la carpeta de fuentes del Compose. Después recrea el contenedor y genera una previa nueva.
