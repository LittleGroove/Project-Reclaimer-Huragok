# Huragok as a container, for Linux hosts running the official Docker setup.
#   docker build -t huragok engine
# See docs/docker-compose.example.yaml. In a container it can watch and moderate
# (backend attach); to restart servers, run start.sh on the host instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HURAGOK_CONFIG=/huragok/settings.toml

WORKDIR /app
COPY pyproject.toml ./
COPY huragok ./huragok
RUN pip install . && useradd --create-home --uid 1000 huragok

# settings.toml, commands.toml, automod.txt, records/ and logs/ live here.
VOLUME /huragok
WORKDIR /huragok
USER huragok

ENTRYPOINT ["huragok"]
# No window in a container: --no-menu, so a settings problem is retried rather than the end of it.
CMD ["start", "--no-menu"]
