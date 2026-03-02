FROM ollama/ollama:latest

# Railway private networking uses IPv6 — bind both
ENV OLLAMA_HOST=[::]

# Bake the embedding model into the image at build time
# so it survives container restarts (no persistent volume needed)
RUN ollama serve & \
    sleep 8 && \
    ollama pull qwen3-embedding && \
    pkill ollama || true

EXPOSE 11434
CMD ["serve"]
