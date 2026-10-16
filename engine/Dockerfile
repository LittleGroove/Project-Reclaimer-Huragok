# The supervisor as a container, for Linux hosts running the official Docker setup.
#   docker build -t reclaimer-supervisor engine
# See docs/docker-compose.example.yaml. In a container it can watch and moderate
# (backend attach); to restart servers, run start.sh on the host instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RECLAIMER_SUPERVISOR_CONFIG=/supervisor/settings.toml

WORKDIR /app
COPY pyproject.toml ./
COPY reclaimer_supervisor ./reclaimer_supervisor
RUN pip install . && useradd --create-home --uid 1000 supervisor

# settings.toml, commands.toml, automod.txt, records/ and logs/ live here.
VOLUME /supervisor
WORKDIR /supervisor
USER supervisor

ENTRYPOINT ["reclaimer-supervisor"]
CMD ["start"]
