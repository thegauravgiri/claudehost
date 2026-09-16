FROM docker.litellm.ai/berriai/litellm:main-stable

COPY config/litellm_config.yaml /app/config.yaml

CMD ["--config", "/app/config.yaml"]
