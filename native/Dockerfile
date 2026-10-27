# Build locally on an isolated Ubuntu 24.04 container, never on a live proxy host.
FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv gcc g++ patchelf ccache \
    ca-certificates openssl libyaml-0-2 liblzma5 libbz2-1.0 libffi8 libexpat1 zlib1g \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /source
COPY requirements-build.txt ./
RUN python3 -m venv /build-venv && /build-venv/bin/pip install --no-cache-dir -r requirements-build.txt
COPY launcher.py vpn.py render.py routing.py network.py subinfo.py autoconfig.py hostfirewall.py firewall.py kit_runtime.py installlog.py build_native.py ./
COPY native/ ./native/
RUN /build-venv/bin/python -B build_native.py --work /native-build --output /release --jobs 2
FROM scratch AS export
COPY --from=build /release/ /
