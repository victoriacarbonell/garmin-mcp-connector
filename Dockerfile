# Small official Python image. 3.12 is the minimum the Garmin client supports.
FROM python:3.12-slim

# Do not write .pyc files; send logs straight to the container log.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the project and its dependencies. No secrets are involved in the
# build: every secret arrives as an environment variable at run time.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Run as an unprivileged user: if the app were ever compromised, the attacker
# would not be root inside the container.
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

# Inside a container the server must listen on all interfaces so the platform
# can reach it. The app refuses to start like this unless authorization is on.
ENV GARMIN_MCP_HOST=0.0.0.0 \
    GARMIN_MCP_PORT=8000
EXPOSE 8000

CMD ["python", "-m", "garmin_mcp"]
