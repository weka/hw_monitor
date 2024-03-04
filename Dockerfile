FROM ubuntu:20.04

RUN apt update
RUN apt -y install ca-certificates
RUN update-ca-certificates --fresh
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_DIR=/etc/ssl/certs/
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ARG BASEDIR="/weka"
ARG ID="472"
ARG USER="weka"

RUN adduser --home $BASEDIR --uid $ID --disabled-password --gecos "Weka User" $USER

WORKDIR $BASEDIR

COPY tarball/hw_monitor/hw_monitor $BASEDIR

EXPOSE 443

WORKDIR $BASEDIR

USER $USER
ENTRYPOINT ["/weka/hw_monitor"]
