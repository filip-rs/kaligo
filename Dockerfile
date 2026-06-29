# Build Python package and dependencies
FROM python:3.12-alpine AS python-build
RUN apk add --no-cache \
        git \
        libffi-dev \
        musl-dev \
        gcc \
        g++ \
        make \
        zlib-dev \
        tiff-dev \
        freetype-dev \
        libpng-dev \
        libjpeg-turbo-dev \
        lcms2-dev \
        libwebp-dev \
        openssl-dev

RUN mkdir -p /opt/venv
WORKDIR /opt/venv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /src
WORKDIR /src

# Install bot package and dependencies
COPY . .
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Package everything
FROM python:3.12-alpine AS final
# Update system first
RUN apk update

# Install optional native tools (for full functionality)
# Note: neofetch was dropped from Alpine repos (upstream discontinued in 2024).
# The .sysinfo command degrades gracefully if it's absent.
RUN apk add --no-cache \
        curl \
        git \
        nss
# Install native dependencies
RUN apk add --no-cache \
        libffi \
        musl \
        gcc \
        g++ \
        make \
        tiff \
        freetype \
        libpng \
        libjpeg-turbo \
        lcms2 \
        libwebp \
        openssl \
        zlib \
        busybox \
        sqlite \
        libxml2 \
        libssh2 \
        ca-certificates \
        ffmpeg \
        libvpx \
        x264-libs \
        x265 \
        libvorbis \
        opus \
        libass \
        xvidcore \
        lame

# Create an unprivileged user to run the bot
RUN addgroup -S kaligo && adduser -S -G kaligo -h /kaligo kaligo

# Setup runtime files
RUN mkdir -p /kaligo
WORKDIR /kaligo
COPY . .

# Copy Python venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=python-build /opt/venv /opt/venv

# Create the downloads dir (the only path the bot writes to at runtime; it's
# backed by a named volume in docker-compose). Creating + chowning it here means
# the volume inherits kaligo ownership so the non-root user can write to it.
RUN mkdir -p /kaligo/kaligo/downloads

# Writable log dir (outside the package, backed by a named volume). Created +
# chowned here so the volume inherits kaligo ownership.
RUN mkdir -p /kaligo/logs

# Give the runtime user ownership of the app dir
RUN chown -R kaligo:kaligo /kaligo

# Drop root before running
USER kaligo

# Set runtime settings
CMD ["python3", "-m", "kaligo"]
