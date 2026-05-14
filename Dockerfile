FROM python:3.11-slim
WORKDIR /app
COPY relay-server.py .
RUN mkdir -p /data
ENV RELAY_TCP_PORT=51900
ENV RELAY_HTTP_PORT=51901
ENV RELAY_DATA=/data
ENV RELAY_LOG=info
VOLUME /data
EXPOSE 51900 51901
CMD ["python", "relay-server.py"]
