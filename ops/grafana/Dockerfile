ARG DEBIAN_BASE_IMAGE=public.ecr.aws/docker/library/debian:bookworm-slim
FROM ${DEBIAN_BASE_IMAGE}

ARG GRAFANA_VERSION=13.1.3
ARG GRAFANA_BUILD=31135815010
ADD --checksum=sha256:e0fd22aa63901ebc961ee64195da60eef8624a831683ca10b26c7b068082e92b \
    https://dl.grafana.com/grafana/release/${GRAFANA_VERSION}/grafana_${GRAFANA_VERSION}_${GRAFANA_BUILD}_linux_amd64.tar.gz \
    /tmp/grafana.tar.gz

RUN mkdir -p /usr/share/grafana /var/lib/grafana/plugins /var/log/grafana \
    && tar -xzf /tmp/grafana.tar.gz --strip-components=1 -C /usr/share/grafana \
    && rm /tmp/grafana.tar.gz \
    && chown -R 472:0 /var/lib/grafana /var/log/grafana

COPY provisioning /etc/grafana/provisioning
COPY dashboards /etc/grafana/dashboards

ENV HOME=/usr/share/grafana \
    GF_PATHS_HOME=/usr/share/grafana \
    GF_PATHS_DATA=/var/lib/grafana \
    GF_PATHS_LOGS=/var/log/grafana \
    GF_PATHS_PLUGINS=/var/lib/grafana/plugins \
    GF_PATHS_PROVISIONING=/etc/grafana/provisioning

USER 472
EXPOSE 3000
ENTRYPOINT ["/usr/share/grafana/bin/grafana"]
CMD ["server", "--homepath=/usr/share/grafana"]
