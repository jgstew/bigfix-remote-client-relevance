# Artifact fixtures

Tiny stand-ins for the BESAgent packages, holding nothing but an
`opt/BESClient/bin/qna` shell script at mode 755 — the one path the extractor
has to get right.

`tiny-qna-gzip.rpm`, `tiny-qna-zstd.rpm`, and `tiny-qna-lzma.rpm` are real rpms
built with `rpmbuild` inside a `rockylinux:9` container, from the same spec,
differing only in `%_binary_payload` (`w9.gzdio`, `w19.zstdio`, and
`w7.lzdio`). All three compressors are in the wild: EL8-era packages are gzip,
EL9-era ones are zstd, and SLE12-era ones use the classic standalone `lzma`
tag — easy to conflate with `xz` (the container format EL8+'s occasional xz
payloads use), but a distinct `archive_compression` tag that this project's
`rpmfile` dependency (2.2.1) doesn't recognize on its own. The extractor is
tested against all three.

The `.deb` fixtures are generated in-test with stdlib `tarfile` plus a
hand-written `ar` wrapper — no external tooling needed for that format.
