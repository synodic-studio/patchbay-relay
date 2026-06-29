---
type: process
title: Custom pi extension with argv-array security
kind: decision
tags: [pi, TypeScript, TypeBox, security, shell injection prevention]
confidence: 1.0
---
Replaced broad `--tools read,grep,find,ls,write` with a TypeScript pi extension defining 12 narrow tools, using `pi.exec(cmd, argv[])` and TypeBox schemas to prevent shell injection. `write_file` enforces the `docs/patchbay/` directory at the tool level.
