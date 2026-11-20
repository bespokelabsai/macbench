# The verifier's own container: the grader needs only Python's standard library and the live day's state, which Harbor
# copies in from the run (task.toml artifacts). The tests are root-only.
FROM python:3.12.8-bookworm
COPY . /tests
RUN chown -R root:root /tests && chmod 700 /tests && chmod 0500 /tests/test.sh && chmod 0400 /tests/*.py /tests/*.json
