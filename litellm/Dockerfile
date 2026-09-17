FROM docker.litellm.ai/berriai/litellm:main-stable

COPY config.yaml /app/config.yaml
COPY auto_workspace_callback.py /app/auto_workspace_callback.py
COPY render_pricing.py /app/render_pricing.py
RUN python3 /app/render_pricing.py /app/config.yaml && rm /app/render_pricing.py

CMD ["--config", "/app/config.yaml"]
