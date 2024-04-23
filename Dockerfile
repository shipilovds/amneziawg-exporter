##############################################################################
#=======================| Wal-g exporter Builder Image |=====================#
##############################################################################
FROM python:alpine AS builder
ARG DEBIAN_FRONTEND=noninteractive
RUN apk update && apk add binutils
RUN pip3 install python-decouple prometheus-client pyinstaller
COPY . /exporter
WORKDIR /exporter
RUN pyinstaller --name awg-exporter --onefile exporter.py

FROM amneziavpn/amneziawg-go as exporter
COPY --from=builder /exporter/dist/awg-exporter /usr/bin/awg-exporter
CMD ["/usr/bin/awg-exporter"]
