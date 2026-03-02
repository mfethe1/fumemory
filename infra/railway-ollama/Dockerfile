FROM ollama/ollama:latest

ENV OLLAMA_HOST=[::]

# Bake model at build time with longer startup wait
RUN ollama serve & \
    for i in $(seq 1 30); do sleep 2 && ollama list 2>/dev/null && break; done && \
    ollama pull qwen3-embedding && \
    pkill ollama || true

EXPOSE 11434
CMD ["serve"]
