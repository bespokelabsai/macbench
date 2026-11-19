# Build environment for the dataset (Rfam 15.1): BLAST, Infernal, numpy, pandas, pyarrow.
# Offline build: tool binaries and wheels are fetched on the host by fetch_vendor.sh (checked against vendor.sha256).
# The base is pinned to the image the published dataset was built with.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY vendor/wheels /tmp/wheels
RUN pip install --no-cache-dir --no-index --find-links /tmp/wheels numpy pandas pyarrow && rm -rf /tmp/wheels
COPY vendor/blast.tgz vendor/infernal.tgz vendor/libgomp1.deb /tmp/
RUN dpkg -i /tmp/libgomp1.deb && rm /tmp/libgomp1.deb
RUN mkdir -p /opt/blast /opt/infernal \
 && tar xzf /tmp/blast.tgz -C /opt/blast --strip-components=1 \
 && tar xzf /tmp/infernal.tgz -C /opt/infernal --strip-components=1 \
 && rm /tmp/*.tgz && rm -rf /opt/infernal/tutorial /opt/infernal/testsuite
ENV PATH=/opt/blast/bin:/opt/infernal/binaries:$PATH
WORKDIR /work
