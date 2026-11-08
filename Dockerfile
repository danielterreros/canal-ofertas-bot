# Imagen de producción multiarch para linux/amd64, linux/arm64 y linux/arm/v7.
FROM python:3.14.10-slim-trixie@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS base
COPY docker/debian.sources /etc/apt/sources.list.d/debian.sources
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM base AS builder
ARG BUILD_JOBS=2
ENV MAX_CONCURRENCY=${BUILD_JOBS}
RUN apt-get -o Acquire::Retries=5 -o APT::Update::Error-Mode=any update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends gcc g++ libc6-dev libjpeg62-turbo-dev zlib1g-dev libfreetype-dev libwebp-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements-build.lock requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-build.lock \
    && python -m pip install --no-cache-dir --require-hashes --no-build-isolation --no-deps \
       --no-binary=Pillow --prefix=/opt/runtime -r requirements.lock \
       -C jpeg=enable -C zlib=enable -C freetype=enable -C webp=enable \
       -C tiff=disable -C jpeg2000=disable -C lcms=disable -C raqm=disable \
       -C imagequant=disable -C xcb=disable -C avif=disable

FROM base AS runtime
RUN apt-get -o Acquire::Retries=5 -o APT::Update::Error-Mode=any update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates tzdata fonts-dejavu-core \
       libjpeg62-turbo zlib1g libfreetype6 libwebp7 libwebpmux3 libwebpdemux2 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin bot \
    && mkdir -p /app /data \
    && chown 10001:10001 /data \
    && chmod 700 /data \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip /usr/local/include/python3.14 \
       /usr/local/lib/python3.14/config-* /usr/local/lib/pkgconfig /usr/local/bin/python*-config \
    && find /usr/share/fonts/truetype/dejavu -type f ! -name DejaVuSans.ttf ! -name DejaVuSans-Bold.ttf -delete
COPY --from=builder /opt/runtime/ /usr/local/
WORKDIR /app
COPY canal_ofertas_bot/ ./canal_ofertas_bot/
USER 10001:10001
CMD ["python", "-m", "canal_ofertas_bot"]


FROM runtime AS production
LABEL org.opencontainers.image.title="Canal Ofertas Bot" \
      org.opencontainers.image.description="Preparación privada y aprobación explícita; un único contenedor con SQLite" \
      org.opencontainers.image.version="1.0.0"
