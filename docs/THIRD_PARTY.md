# Third-party licenses

The original jarvS material is under the Aymane Amounas Noncommercial
Attribution License 1.0. The third-party dependencies are a different matter:
package managers fetch them at build or install time, and they stay under their
own licenses. The project license does not replace, restrict, or grant any
rights under those dependency licenses. This inventory is not legal
compatibility advice, just a record of what is in here. Reviewed 2026-11-08
against the locked versions.

## Rust (`Cargo.lock`)

| License | Crates |
|---|---|
| MIT and/or Apache-2.0 (incl. Zlib/Unlicense alternatives) | 64 |
| `(MIT OR Apache-2.0) AND Unicode-3.0` | unicode-ident |
| MPL-2.0 | option-ext (transitive, via `directories`) |

MPL-2.0 is file-level copyleft: if the `option-ext` source files are ever
modified and redistributed, those files have to stay MPL-2.0. jarvS does not
modify them. When you redistribute source or binaries, include and preserve the
applicable third-party notices, as each dependency's own license requires.

Regenerate: `cargo metadata --format-version 1 --locked` (fields `license` and
`license_file`).

## Python (`requirements.txt`, `requirements-dev.txt`)

| Package | License |
|---|---|
| langgraph, langgraph-checkpoint-sqlite, PyYAML, ddgs, faster-whisper, fastapi, pytest, ruff | MIT |
| requests, playwright, google-api-python-client, google-auth, google-auth-oauthlib | Apache-2.0 |
| websockets, python-dotenv, uvicorn | BSD-3-Clause |

## Node (WhatsApp bridge, `channels/bridge/package.json`)

| Package | License |
|---|---|
| @whiskeysockets/baileys | MIT. Unofficial WhatsApp Web client; see the README's risk note |
| pino | MIT |
| qrcode-terminal | Apache-2.0 |

## Models and voices (not bundled)

Ollama models, Piper/Kokoro voices, and Whisper weights are downloaded by the
user and carry their own licenses, which can differ per model (some restrict
commercial use). jarvS does not redistribute any of them, so check each model's
license before you use it.

## Brand assets

`assets/logo.svg` and the files derived from it are original jarvS works under
the project license.
