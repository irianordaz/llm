# llm

A unified CLI wrapper for **ollama**, **mlx-lm**, and **vllm-mlx**.
One command to run, stop, download, and inspect models across all
three providers. Set a default once and `llm run` always works without
arguments. Only one model runs at a time; state is persisted in
`~/.llm/`.

## Installation

### From source

Requires **Python 3.9 or newer**. No other dependencies — `llm.py`
uses only the standard library.

```bash
git clone <repo>
cd llm
python3 llm.py --help
```

[Pixi](https://pixi.sh) is also supported if you want a managed
environment (required for `pixi run build`):

```bash
pixi install
python3 llm.py --help
```

### Standalone app 

Build a self-contained `.app` and `.dmg` — no Python or Pixi needed
to run the result:

```bash
pixi run build
```

This produces `dist/llm.app` and `dist/llm.dmg`.

**Install from the DMG:**

```bash
open dist/llm.dmg        # drag llm.app to /Applications
```

**Add to your shell** (append to `~/.zshrc` or `~/.bashrc`):

```bash
export PATH="/Applications/llm.app/Contents/MacOS:$PATH"
```

Then reload your shell and confirm:

```bash
source ~/.zshrc
llm --help
```

### Provider dependencies

Install whichever providers you need separately:

| Provider | Install |
|---|---|
| ollama | <https://ollama.com> |
| mlx-lm | `pip install mlx-lm` |
| vllm-mlx | `pip install vllm` (Apple Silicon) |
| HuggingFace CLI | `pip install huggingface-hub` |

## Commands

| Command | Description |
|---|---|
| `llm ls` | List all locally downloaded models |
| `llm ps` | Show the currently running model |
| `llm run [provider model]` | Start a model (uses default if no args) |
| `llm stop` | Stop the running model — no args needed |
| `llm default [provider model]` | Show or set the default |
| `llm download <provider> <model>` | Download a model |
| `llm provider info` | Show provider details |
| `llm provider set <provider> <path>` | Set the executable path for a provider |

Run `llm --help` or `llm <command> --help` for full option details.

## Usage

### `llm ls`

Lists every locally available model, labelled by provider.

```
PROVIDER            MODEL
------------------  ----------------------------------------
ollama              llama3.2:latest
ollama              mistral:latest
mlx-lm / vllm-mlx  mlx-community/Llama-3.2-3B-Instruct-4bit
```

- **ollama** models: discovered via `ollama ls` (`~/.ollama/models`)
- **mlx-lm / vllm-mlx** models: scanned from
  `~/.cache/huggingface/hub` — either provider can load them

### `llm provider info`

Shows the executable path, default port, base URL, model directory,
and install status for every provider.

```
ollama
  Executable    /usr/local/bin/ollama
  Default port  11434
  Base URL      http://127.0.0.1:11434
  Model dir     /Users/you/.ollama/models
  Status        installed

mlx-lm
  Executable    /opt/homebrew/bin/mlx_lm
  Default port  8080
  Base URL      http://127.0.0.1:8080/v1
  Model dir     /Users/you/.cache/huggingface/hub
  Status        not installed

vllm-mlx
  Executable    not found
  Default port  8080
  Base URL      http://127.0.0.1:8080/v1
  Model dir     /Users/you/.cache/huggingface/hub
  Status        not installed
```

### `llm provider set`

Configure the path to a provider executable or pixi environment directory.
Settings are saved to `~/.llm/config.json`.

If the path is a **directory containing `pixi.toml`**, `llm` invokes the
provider with `pixi run` from that directory instead of using the system
Python.

```bash
# Set the mlx-lm executable (default is /opt/homebrew/bin/mlx_lm)
llm provider set mlx-lm /opt/homebrew/bin/mlx_lm

# Point vllm-mlx at a pixi environment directory
llm provider set vllm-mlx /path/to/vllm-mlx
```

After setting a path, `llm provider info` reflects the new executable and
`llm run` uses it immediately.

### `llm download`

```bash
llm download ollama llama3.2
llm download mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
llm download vllm-mlx mlx-community/Mistral-7B-v0.1-4bit
```

Uses `ollama pull` for ollama and `huggingface-cli download` for the
HuggingFace providers.

### `llm default`

```bash
llm default                                    # show current default
llm default ollama llama3.2                    # set a new default
```

Saved to `~/.llm/config.json`. Once set, bare `llm run` uses it.

### `llm run`

```bash
llm run                          # run the configured default
llm run ollama llama3.2
llm run mlx-lm <model> [flags]
llm run mlx-lm <model> --host 0.0.0.0 --port 8081
```

Any flag not recognised by `llm` is forwarded directly to the
provider binary:

```bash
llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit \
    --port 8080 --chat-template chatml --max-tokens 2048

llm run vllm-mlx mlx-community/Mistral-7B-v0.1-4bit \
    --port 8080 --max-model-len 4096 --dtype float16
```

**Provider behaviour:**
- **ollama** — starts an interactive session (`ollama run`)
- **mlx-lm** — starts an OpenAI-compatible API server
- **vllm-mlx** — starts an OpenAI-compatible API server

### `llm ps`

Shows the provider, model, host, port, base URL, PID, start time,
and liveness of whatever is currently running.

```
Provider   mlx-lm
Model      mlx-community/Llama-3.2-3B-Instruct-4bit
Host       127.0.0.1
Port       8080
Base URL   http://127.0.0.1:8080/v1
PID        12345
Started    2026-04-25T12:00:00
Status     running
```

### `llm stop`

Stops whichever model is currently running — reads the state file,
sends `SIGTERM` to the saved PID, and for ollama also calls
`ollama stop <model>` to free GPU memory.

## Examples

```bash
# Inspect providers and local models
llm provider info
llm ls

# Download models
llm download ollama llama3.2
llm download mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit

# Set a default and run it with just: llm run
llm default mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
llm run

# Run ollama on a specific host and port
llm run ollama llama3.2 --host 0.0.0.0 --port 11434

# Run mlx-lm with provider-specific flags
llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit \
    --port 8080 --chat-template chatml --max-tokens 2048

# Run vllm-mlx with provider-specific flags
llm run vllm-mlx mlx-community/Mistral-7B-v0.1-4bit \
    --port 8080 --max-model-len 4096 --dtype float16

# Inspect and stop
llm ps
llm stop
```

## Reference

### Default ports and base URLs

| Provider | Port | Base URL |
|---|---|---|
| ollama | 11434 | `http://127.0.0.1:11434` |
| mlx-lm | 8080 | `http://127.0.0.1:8080/v1` |
| vllm-mlx | 8080 | `http://127.0.0.1:8080/v1` |

### Model storage

| Provider | Location |
|---|---|
| ollama | `~/.ollama/models` |
| mlx-lm | `~/.cache/huggingface/hub` |
| vllm-mlx | `~/.cache/huggingface/hub` |

### Runtime files

| File | Purpose |
|---|---|
| `~/.llm/state.json` | Active session — provider, model, PID, port |
| `~/.llm/config.json` | Saved default provider, model, and provider paths |
