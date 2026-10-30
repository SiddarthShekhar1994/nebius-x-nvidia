# Keeper's live demo as a Hugging Face Docker Space. Upload this file and space/README.md to the
# Space; the build takes Keeper's code from GitHub, so a "Factory rebuild" picks up new commits.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 user

ARG KEEPER_REPO=https://github.com/SiddarthShekhar1994/nebius-x-nvidia.git
ARG KEEPER_REF=main
RUN git clone --depth 1 --branch "$KEEPER_REF" "$KEEPER_REPO" /app \
    && pip install --no-cache-dir -r /app/requirements.txt

USER user
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=7860
EXPOSE 7860
# TOKENFACTORY_API_KEY comes from the Space's secrets at run time; never put it in this file.
CMD ["python", "-m", "space.server"]
