# llm

A unified CLI wrapper for **ollama**, **mlx-lm**, and **vllm-mlx** that
gives every provider a consistent interface. Run and stop any model with
a single command, list everything you have downloaded, and configure a
default so you only ever need to type `llm run`.

## Installation

```bash
pixi install
```

The providers must be installed separately:

| Provider | Install |
|----------|---------|
| ollama | https://ollama.com |
| mlx-lm | `pip install mlx-lm` |
| vllm-mlx | `pip install vllm` (Apple Silicon) |
| HuggingFace CLI | `pip install huggingface-hub` |

## Usage

```
llm COMMAND [options]

Commands:
  ls        List all locally available models from all providers
  ps        Show the currently running model
  run       Start a model (uses configured default when called bare)
  stop      Stop the currently running model (no arguments needed)
  default   Get or set the default provider and model
  download  Download a model from ollama or HuggingFace
```

Run `llm --help` or `llm COMMAND --help` for full option details.

### State tracking

`llm` writes `~/.llm/state.json` while a model is running, storing the
provider, model, host, port, and PID. `llm stop` and `llm ps` read this
file — no arguments required. Only one model may run at a time.

### `llm ls`

Lists every locally downloaded model, labelled by provider.

```
PROVIDER           MODEL
-----------------  ----------------------------------------
ollama             llama3.2:latest
mlx-lm / vllm-mlx  mlx-community/Llama-3.2-3B-Instruct-4bit
```

- **ollama** models: discovered via `ollama ls` (`~/.ollama/models`)
- **mlx-lm / vllm-mlx** models: scanned from
  `~/.cache/huggingface/hub` (either provider can load them)

### `llm ps`

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

### `llm run`

```
llm run [provider model] [--host HOST] [--port PORT] [flags...]
```

Unrecognised flags are forwarded to the provider binary transparently.
Omit `provider` and `model` to use the configured default.

### `llm stop`

Reads the state file and terminates whatever is currently running.
No provider, model, or port argument is needed.

### `llm default`

```
llm default                     # show current default
llm default <provider> <model>  # set a new default
```

### `llm download`

```
llm download ollama <model>      # ollama pull
llm download mlx-lm <model>     # huggingface-cli download
llm download vllm-mlx <model>   # huggingface-cli download
```

## Examples

```bash
# Download models
llm download ollama llama3.2
llm download mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit

# Set a default and run it with just: llm run
llm default mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
llm run

# Run a specific model on a custom port
llm run ollama llama3.2 --host 0.0.0.0 --port 11434

# Run mlx-lm with provider-specific flags
llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit \
    --port 8080 --chat-template chatml --max-tokens 2048

# Run vllm-mlx with extra flags
llm run vllm-mlx mlx-community/Mistral-7B-v0.1-4bit \
    --port 8080 --max-model-len 4096 --dtype float16

# Check what is running
llm ps

# Stop it — no arguments needed
llm stop
```

## Default ports

| Provider | Port |
|----------|------|
| ollama | 11434 |
| mlx-lm | 8080 |
| vllm-mlx | 8080 |
