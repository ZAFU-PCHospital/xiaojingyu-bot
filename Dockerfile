FROM m.daocloud.io/docker.io/soulter/astrbot@sha256:d3007632822a128439561a3de02fc663a3ca8e9e5cd309de1c19108317ba26f5
COPY requirements-extra.txt /tmp/xiaojingyu-requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/xiaojingyu-requirements.txt
