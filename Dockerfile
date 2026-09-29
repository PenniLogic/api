FROM eclipse-temurin:21-jre-alpine@sha256:4cbffea0432e0209a002c816a9fad6557d83147e56d5df6a73cdeec3c03ea522

RUN addgroup -S -g 10001 api && adduser -S -D -H -u 10001 -G api api
WORKDIR /app
COPY --chown=10001:10001 build/install/pennilogic-api/lib/ /app/lib/
ENV HOST=0.0.0.0 PORT=8080
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=15s --retries=3 \
    CMD wget -q -O - "http://127.0.0.1:${PORT}/health/ready" | grep -qx '{"status":"ready"}' || exit 1
ENTRYPOINT ["java", "-cp", "/app/lib/*", "com.pennilogic.bootstrap.ApplicationKt"]
