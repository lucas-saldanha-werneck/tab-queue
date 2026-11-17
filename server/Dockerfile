FROM python:3.12-alpine
WORKDIR /app
COPY app.py page.html ./
RUN adduser -D -u 10001 tq && mkdir /data && chown tq /data
USER tq
ENV TQ_DATA=/data TQ_HOST=0.0.0.0 TQ_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
CMD ["python", "-u", "app.py"]
