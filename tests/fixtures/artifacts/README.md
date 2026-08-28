# Artifact fixtures

Tiny stand-ins for the BESAgent packages, holding nothing but an
`opt/BESClient/bin/qna` shell script at mode 755 — the one path the extractor
has to get right.

`tiny-qna-gzip.rpm` and `tiny-qna-zstd.rpm` are real rpms built with
`rpmbuild` inside a `rockylinux:9` container, from the same spec, differing
only in `%_binary_payload` (`w9.gzdio` and `w19.zstdio`). Both compressors are
in the wild: EL8-era packages are gzip, EL9-era ones are zstd, so the extractor
is tested against each.

The `.deb` fixtures are generated in-test with stdlib `tarfile` plus a
hand-written `ar` wrapper — no external tooling needed for that format.
