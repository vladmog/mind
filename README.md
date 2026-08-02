# mind — a personal LLM wiki

A wiki for arbitrary data and information in one life. Structured as a graph of markdown files. Maintained by LLM agents. Browseable by humans via a small web UI.

## Layout

- `raw/` — immutable inputs (text, audio, image, pdf, video). Drop things here. *(data repo)*
- `wiki/` — the LLM-maintained markdown graph. Source of truth. *(data repo)*
- `cache/` — derived indexes; disposable. Rebuild with `./bin/mind rebuild-cache`.
- `bin/mind` — the CLI (init, ingest, query, lint, search, index, graph, serve).
- `ui/` — the read-only web UI served by `./bin/mind serve`.
- `seed/wiki/` — generic starter pages `./bin/mind init` copies into a fresh `wiki/`.
- `config.yaml` — tunable knobs (preprocessing toggles, agent command, defaults).
- `config.wiki.yaml` — wiki-scoped overrides (your taxonomy). *(data repo)*
- `config.local.yaml` — per-machine overrides (paths). Gitignored; see `config.local.example.yaml`.

## For agents

- `AGENTS.md` — the canonical contract for any agent operating on the wiki.
- `CLAUDE.md` — Claude-Code-specific layer pointing at `AGENTS.md`.
- `llm-wiki.md` — the underlying philosophy.
- `notes/` — local working notes (older drafts, specs). Gitignored; not part of the app.

## Quick start

```bash
# 1) First-time setup
git clone https://github.com/<you>/mind && cd mind
python3 -m venv .venv             # Windows: py -3 -m venv .venv
pip install -r requirements.txt   # pyyaml, markdown-it-py, starlette, uvicorn, jinja2, pypdf
./bin/mind init                   # seed wiki/, create your private data repo, build cache
                                  # (Windows: bin\mind.cmd init)

# 2) Drop a source in and ingest it
cp ~/Downloads/some-article.md raw/
./bin/mind ingest raw/some-article.md

# 3) Ask the wiki something
./bin/mind query "what have I learned about X?"

# 4) Browse
./bin/mind serve
# open http://localhost:8787
```

`mind init` is idempotent: it only creates what's missing and never overwrites pages you already have.

## Audio transcription (whisper.cpp)

Audio ingest relies on [whisper.cpp](https://github.com/ggerganov/whisper.cpp). The binary is platform-specific (Mach-O on Mac, PE on Windows), so `whisper.cpp/` is **gitignored — set it up per machine**. The ggml model files are architecture-independent and can be copied between machines.

### macOS

Two options. The Homebrew path is simplest; the local-clone path keeps everything inside the project.

```bash
# option A — Homebrew (binary on PATH)
brew install whisper-cpp          # provides `whisper-cli`

# either way, you still need a ggml model. Put it where bin/mind looks by default:
mkdir -p whisper.cpp/models
curl -L -o whisper.cpp/models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin   # ~142 MB; config.yaml default
```

```bash
# option B — build from source
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
make -j                                              # requires cmake (brew install cmake)
./models/download-ggml-model.sh base.en
```

### Windows

```bat
:: option A — prebuilt release (simplest): download the x64 zip from
::   https://github.com/ggerganov/whisper.cpp/releases
:: and unzip so whisper-cli.exe sits at whisper.cpp\whisper-cli.exe

:: option B — build from source (needs cmake + VS Build Tools)
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
cmake -B build && cmake --build build --config Release

:: model, either way:
models\download-ggml-model.cmd base.en
```

Bigger models transcribe better (`small.en`, `medium.en`, or the multilingual `large-v3-turbo`). Download the one you want into `whisper.cpp/models/` and set `preprocessing.audio.model` in `config.local.yaml` (see `config.local.example.yaml`).

### How `bin/mind` finds it

- **Binary**: `preprocessing.audio.binary` (config) → `whisper-cli` / `whisper-cpp` / `main` on PATH (`.exe` implied on Windows) → `./whisper.cpp/build/bin/whisper-cli[.exe]` → `./whisper.cpp/build/bin/Release/whisper-cli.exe` → `./whisper.cpp/whisper-cli[.exe]` → `./whisper.cpp/main[.exe]`.
- **Model**: `preprocessing.audio.model_path` (config) → `./whisper.cpp/models/ggml-<model>.bin` (where `<model>` is `preprocessing.audio.model`).

If either is missing at ingest time, audio preprocessing writes a stub transcript with a pointer to this section instead of failing the ingest.

## Two repos, one directory

Your wiki is personal data; the tooling is not. They live in separate git repos sharing this working directory:

- **Code repo** (`.git`) — bin/, ui/, docs, config. Safe to publish; push it to GitHub freely. The shipped `.gitignore` hides `wiki/`, `raw/`, and `config.wiki.yaml` from it, so every clone is safe by default: `git add -A` can never stage your data, and a code PR can never contain it.
- **Data repo** (`.git-data`) — tracks `wiki/`, `raw/`, and `config.wiki.yaml` (its `info/exclude` hides everything else). `mind commit` (and every auto-commit) targets this repo, staging with `git add -f` to deliberately bypass the shared `.gitignore`. Created by `mind init` (or lazily on first commit). It has no remote by default — keep it local, or push it to a *private* remote for off-machine backup. Never give it a public remote.

One cosmetic side effect of the `.gitignore` split: `git --git-dir=.git-data status` reports brand-new pages as ignored rather than untracked. That's expected — staging is `mind commit`'s job, and already-tracked files show modifications normally.

Day-to-day: plain `git` commands operate on the code repo. To inspect your data history use `git --git-dir=.git-data log`, or add an alias: `git config --global alias.data '--git-dir=.git-data'` then `git data log`.

**Contributing:** because the data paths are gitignored in the code repo, you can hack on the code and open PRs from the same directory you journal in — your data physically cannot ride along.

## Notes

- `wiki/` is the canonical data. Everything else is tooling. Swap any tool (CLI, UI, preprocessing backend) without migrating data.
- Every mutating operation auto-commits — to the data repo.
- The wiki grows organically. Start empty; add sources as they arrive.
