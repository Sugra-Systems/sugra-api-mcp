# Container image for sugra-api-mcp.
#
# The default entrypoint runs the MCP server over stdio (no arguments),
# matching `sugra-api-mcp` on a local install. Override the command for the
# Streamable HTTP transport:
#
#   docker run -p 8001:8001 sugra-api-mcp \
#     --transport streamable-http --host 0.0.0.0 --port 8001
#
# No environment variable is required to start the container; configuration
# such as SUGRA_API_KEY is supplied at run time (docker run -e / compose).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl backs the container healthcheck in docker-compose.yml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build the package from the repository source with the [http] extra so one
# image serves both the stdio and Streamable HTTP transports.
COPY pyproject.toml README.md LICENSE ./
COPY sugra_api_mcp ./sugra_api_mcp
RUN pip install --no-cache-dir ".[http]"

# Run as a non-root user.
RUN useradd --create-home --uid 1000 sugra
USER sugra

ENTRYPOINT ["sugra-api-mcp"]
CMD []
