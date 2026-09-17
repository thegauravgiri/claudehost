FROM docker.litellm.ai/berriai/litellm:main-stable

COPY config/litellm_config.yaml /app/config.yaml
COPY config/litellm_config.developers.yaml /app/config.developers.yaml
COPY docker/render_pricing.py /app/render_pricing.py
RUN python3 /app/render_pricing.py /app/config.yaml /app/config.developers.yaml \
    && rm /app/render_pricing.py /app/config.developers.yaml

CMD ["--config", "/app/config.yaml"]
