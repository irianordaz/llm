#!/usr/bin/env python3
"""Unified CLI wrapper for ollama, mlx-lm, and vllm-mlx."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROVIDERS = ['ollama', 'mlx-lm', 'vllm-mlx']

DEFAULT_HOST = '127.0.0.1'

DEFAULT_PORTS = {
    'ollama': 11434,
    'mlx-lm': 8080,
    'vllm-mlx': 8080,
}

BASE_URL_TEMPLATES = {
    'ollama': 'http://{host}:{port}',
    'mlx-lm': 'http://{host}:{port}/v1',
    'vllm-mlx': 'http://{host}:{port}/v1',
}

HF_CACHE_DIR = Path.home() / '.cache' / 'huggingface' / 'hub'
OLLAMA_MODEL_DIR = Path.home() / '.ollama' / 'models'

PROVIDER_MODEL_DIRS = {
    'ollama': OLLAMA_MODEL_DIR,
    'mlx-lm': HF_CACHE_DIR,
    'vllm-mlx': HF_CACHE_DIR,
}

LLM_DIR = Path.home() / '.llm'
STATE_FILE = LLM_DIR / 'state.json'
CONFIG_FILE = LLM_DIR / 'config.json'

HELP_EPILOG = """
Examples:

  List all local models from every provider:
    llm ls

  Set a default provider and model:
    llm default mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit

  Show the current default:
    llm default

  Run the default model:
    llm run

  Run a specific model:
    llm run ollama llama3.2
    llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
    llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit --port 8081

  Pass provider-specific flags after standard options:
    llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit \\
        --port 8080 --chat-template chatml --max-tokens 2048
    llm run vllm-mlx mlx-community/Mistral-7B-v0.1-4bit \\
        --port 8080 --max-model-len 4096 --dtype float16

  See what is currently running:
    llm ps

  Stop the running model (no arguments needed):
    llm stop

  Download a model:
    llm download ollama llama3.2
    llm download mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
    llm download vllm-mlx mlx-community/Mistral-7B-v0.1-4bit

  Show provider details (executable, port, base URL, model dir):
    llm provider info

Default ports:
  ollama    11434
  mlx-lm    8080
  vllm-mlx  8080

Model locations:
  ollama    ~/.ollama/models
  mlx-lm    ~/.cache/huggingface/hub
  vllm-mlx  ~/.cache/huggingface/hub
"""


# ---------------------------------------------------------------------------
# State and config helpers
# ---------------------------------------------------------------------------


def _ensure_llm_dir() -> None:
    LLM_DIR.mkdir(parents=True, exist_ok=True)


def read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_state(state: dict) -> None:
    _ensure_llm_dir()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_config(config: dict) -> None:
    _ensure_llm_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def get_ollama_models() -> list[str]:
    try:
        result = subprocess.run(
            ['ollama', 'ls'],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        models = []
        for line in lines[1:]:  # first line is the header row
            parts = line.split()
            if parts:
                models.append(parts[0])
        return models
    except FileNotFoundError:
        print('Warning: ollama not found, skipping.', file=sys.stderr)
        return []
    except subprocess.CalledProcessError as err:
        print(f'Warning: ollama ls failed: {err}', file=sys.stderr)
        return []


def get_huggingface_models() -> list[str]:
    if not HF_CACHE_DIR.exists():
        return []
    models = []
    for entry in sorted(HF_CACHE_DIR.iterdir()):
        if not (entry.is_dir() and entry.name.startswith('models--')):
            continue
        name = entry.name.removeprefix('models--')
        models.append('/'.join(name.split('--')))
    return models


# ---------------------------------------------------------------------------
# Subcommand: ls
# ---------------------------------------------------------------------------


def cmd_ls(args: argparse.Namespace, _: list[str]) -> None:
    ollama_models = get_ollama_models()
    hf_models = get_huggingface_models()

    rows: list[tuple[str, str]] = (
        [('ollama', m) for m in ollama_models]
        + [('mlx-lm / vllm-mlx', m) for m in hf_models]
    )

    if not rows:
        print('No local models found.')
        return

    prov_col = max((len(p) for p, _ in rows), default=0)
    prov_col = max(prov_col, len('PROVIDER'))
    model_col = max((len(m) for _, m in rows), default=0)
    model_col = max(model_col, len('MODEL'))

    print(f'{"PROVIDER":<{prov_col}}  {"MODEL":<{model_col}}')
    print(f'{"-" * prov_col}  {"-" * model_col}')
    for provider, model in rows:
        print(f'{provider:<{prov_col}}  {model:<{model_col}}')


# ---------------------------------------------------------------------------
# Subcommand: ps
# ---------------------------------------------------------------------------


def cmd_ps(args: argparse.Namespace, _: list[str]) -> None:
    state = read_state()
    if not state:
        print('No model currently running.')
        return

    provider = state.get('provider', 'unknown')
    model = state.get('model', 'unknown')
    host = state.get('host', DEFAULT_HOST)
    port = state.get('port', DEFAULT_PORTS.get(provider, '?'))
    pid = state.get('pid')
    started_at = state.get('started_at', 'unknown')

    running = is_process_alive(pid) if pid else False
    status = 'running' if running else 'stopped (stale state)'

    base_url = BASE_URL_TEMPLATES.get(
        provider, 'http://{host}:{port}'
    ).format(host=host, port=port)

    w = 10
    print(f'{"Provider":<{w}} {provider}')
    print(f'{"Model":<{w}} {model}')
    print(f'{"Host":<{w}} {host}')
    print(f'{"Port":<{w}} {port}')
    print(f'{"Base URL":<{w}} {base_url}')
    print(f'{"PID":<{w}} {pid}')
    print(f'{"Started":<{w}} {started_at}')
    print(f'{"Status":<{w}} {status}')


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------


def _resolve_provider_model(
    args: argparse.Namespace,
) -> tuple[str, str]:
    provider = args.provider
    model = args.model

    if provider and model:
        if provider not in PROVIDERS:
            print(
                f'Error: unknown provider "{provider}". '
                f'Choose from: {", ".join(PROVIDERS)}',
                file=sys.stderr,
            )
            sys.exit(1)
        return provider, model

    if provider and not model:
        print(
            'Error: a model name is required when a provider is given.',
            file=sys.stderr,
        )
        sys.exit(1)

    config = read_config()
    default_provider = config.get('default_provider')
    default_model = config.get('default_model')

    if not default_provider or not default_model:
        print(
            'Error: no provider/model given and no default is configured.\n'
            'Set one with: llm default <provider> <model>',
            file=sys.stderr,
        )
        sys.exit(1)

    return default_provider, default_model


def _build_run_cmd(
    provider: str,
    model: str,
    host: str,
    port: int,
    passthrough: list[str],
) -> list[str]:
    if provider == 'ollama':
        return ['ollama', 'run', model] + passthrough
    if provider == 'mlx-lm':
        return [
            sys.executable, '-m', 'mlx_lm.server',
            '--model', model,
            '--host', host,
            '--port', str(port),
        ] + passthrough
    if provider == 'vllm-mlx':
        return [
            'vllm', 'serve', model,
            '--host', host,
            '--port', str(port),
        ] + passthrough
    print(f'Unknown provider: {provider}', file=sys.stderr)
    sys.exit(1)


def cmd_run(args: argparse.Namespace, passthrough: list[str]) -> None:
    current = read_state()
    if current:
        pid = current.get('pid')
        if pid and is_process_alive(pid):
            print(
                f'Error: {current["provider"]} is already running '
                f'({current["model"]}).\n'
                'Stop it first with: llm stop',
                file=sys.stderr,
            )
            sys.exit(1)
        clear_state()

    provider, model = _resolve_provider_model(args)
    host = getattr(args, 'host', DEFAULT_HOST)
    port_arg = getattr(args, 'port', None)
    port = port_arg if port_arg is not None else DEFAULT_PORTS[provider]

    env = os.environ.copy()
    if provider == 'ollama':
        env['OLLAMA_HOST'] = f'{host}:{port}'

    cmd = _build_run_cmd(provider, model, host, port, passthrough)
    print(f'Starting {provider}: {model}  ({host}:{port})')

    try:
        proc = subprocess.Popen(cmd, env=env)
    except FileNotFoundError:
        print(
            f'Error: {provider} binary not found. Is it installed?',
            file=sys.stderr,
        )
        sys.exit(1)

    write_state({
        'provider': provider,
        'model': model,
        'host': host,
        'port': port,
        'pid': proc.pid,
        'started_at': datetime.now().isoformat(timespec='seconds'),
    })

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        print(f'\n{provider} stopped.')
    finally:
        clear_state()


# ---------------------------------------------------------------------------
# Subcommand: stop
# ---------------------------------------------------------------------------


def cmd_stop(args: argparse.Namespace, _: list[str]) -> None:
    state = read_state()
    if not state:
        print('No running model found.')
        return

    provider = state.get('provider')
    model = state.get('model')
    pid = state.get('pid')

    if pid and is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            print(f'Sent SIGTERM to PID {pid}.')
        except PermissionError:
            print(
                f'Warning: no permission to signal PID {pid}.',
                file=sys.stderr,
            )
    else:
        print(f'Process {pid} is no longer running.')

    if provider == 'ollama' and model:
        try:
            subprocess.run(['ollama', 'stop', model], check=True)
            print(f'Unloaded ollama model: {model}')
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    clear_state()
    print(f'Stopped {provider}: {model}')


# ---------------------------------------------------------------------------
# Subcommand: default
# ---------------------------------------------------------------------------


def cmd_default(args: argparse.Namespace, _: list[str]) -> None:
    provider = args.provider
    model = args.model

    if not provider and not model:
        config = read_config()
        dp = config.get('default_provider', '(not set)')
        dm = config.get('default_model', '(not set)')
        print(f'Default provider: {dp}')
        print(f'Default model:    {dm}')
        return

    if not provider or not model:
        print(
            'Error: both provider and model are required.\n'
            'Usage: llm default <provider> <model>',
            file=sys.stderr,
        )
        sys.exit(1)

    if provider not in PROVIDERS:
        print(
            f'Error: unknown provider "{provider}". '
            f'Choose from: {", ".join(PROVIDERS)}',
            file=sys.stderr,
        )
        sys.exit(1)

    config = read_config()
    config['default_provider'] = provider
    config['default_model'] = model
    write_config(config)
    print(f'Default set: {provider} / {model}')


# ---------------------------------------------------------------------------
# Subcommand: download
# ---------------------------------------------------------------------------


def cmd_download(args: argparse.Namespace, _: list[str]) -> None:
    provider = args.provider
    model = args.model

    if provider == 'ollama':
        cmd = ['ollama', 'pull', model]
        missing_hint = 'Install ollama from https://ollama.com'
    else:
        cmd = ['huggingface-cli', 'download', model]
        missing_hint = (
            'Install the HuggingFace CLI with: pip install huggingface-hub'
        )

    print(f'Downloading {model} via {provider}...')
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print(f'Error: command not found.\n{missing_hint}', file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as err:
        print(f'Download failed: {err}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: provider info
# ---------------------------------------------------------------------------


def _provider_executable(provider: str) -> tuple[str, bool]:
    if provider == 'ollama':
        path = shutil.which('ollama')
        return (path, True) if path else ('not found', False)
    if provider == 'mlx-lm':
        spec = importlib.util.find_spec('mlx_lm')
        installed = spec is not None
        return (f'{sys.executable} -m mlx_lm.server', installed)
    if provider == 'vllm-mlx':
        path = shutil.which('vllm')
        return (path, True) if path else ('not found', False)
    return ('unknown', False)


def cmd_provider_info(args: argparse.Namespace, _: list[str]) -> None:
    w = 12
    for i, provider in enumerate(PROVIDERS):
        if i > 0:
            print()
        executable, installed = _provider_executable(provider)
        port = DEFAULT_PORTS[provider]
        base_url = BASE_URL_TEMPLATES[provider].format(
            host=DEFAULT_HOST, port=port
        )
        model_dir = PROVIDER_MODEL_DIRS[provider]
        status = 'installed' if installed else 'not installed'

        print(provider)
        print(f'  {"Executable":<{w}}  {executable}')
        print(f'  {"Default port":<{w}}  {port}')
        print(f'  {"Base URL":<{w}}  {base_url}')
        print(f'  {"Model dir":<{w}}  {model_dir}')
        print(f'  {"Status":<{w}}  {status}')


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _add_network_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--host',
        default=DEFAULT_HOST,
        metavar='HOST',
        help=f'Bind host address (default: {DEFAULT_HOST}).',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=None,
        metavar='PORT',
        help=(
            'Port number. Defaults: ollama=11434, '
            'mlx-lm=8080, vllm-mlx=8080.'
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='llm',
        description='Unified wrapper for ollama, mlx-lm, and vllm-mlx.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )

    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND')
    subparsers.required = True

    # -- ls ------------------------------------------------------------------
    ls_parser = subparsers.add_parser(
        'ls',
        help='List all locally available models.',
    )
    ls_parser.set_defaults(func=cmd_ls)

    # -- ps ------------------------------------------------------------------
    ps_parser = subparsers.add_parser(
        'ps',
        help='Show the currently running model.',
    )
    ps_parser.set_defaults(func=cmd_ps)

    # -- run -----------------------------------------------------------------
    run_parser = subparsers.add_parser(
        'run',
        help='Start a model (uses configured default when called bare).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Start a model server or interactive session.\n'
            'Omit provider and model to run the configured default.\n'
            'Unknown flags are forwarded directly to the provider binary.\n'
            'Only one model may run at a time.'
        ),
    )
    run_parser.add_argument(
        'provider',
        nargs='?',
        metavar='PROVIDER',
        help=f'Provider: {", ".join(PROVIDERS)}.',
    )
    run_parser.add_argument(
        'model',
        nargs='?',
        metavar='MODEL',
        help='Model identifier.',
    )
    _add_network_args(run_parser)
    run_parser.set_defaults(func=cmd_run)

    # -- stop ----------------------------------------------------------------
    stop_parser = subparsers.add_parser(
        'stop',
        help='Stop the currently running model.',
        description=(
            'Stops whichever model is currently running.\n'
            'No arguments are required.'
        ),
    )
    stop_parser.set_defaults(func=cmd_stop)

    # -- default -------------------------------------------------------------
    default_parser = subparsers.add_parser(
        'default',
        help='Get or set the default provider and model.',
        description=(
            'With no arguments, shows the current default.\n'
            'With provider and model, saves a new default.'
        ),
    )
    default_parser.add_argument(
        'provider',
        nargs='?',
        metavar='PROVIDER',
        help=f'Provider: {", ".join(PROVIDERS)}.',
    )
    default_parser.add_argument(
        'model',
        nargs='?',
        metavar='MODEL',
        help='Model identifier.',
    )
    default_parser.set_defaults(func=cmd_default)

    # -- download ------------------------------------------------------------
    download_parser = subparsers.add_parser(
        'download',
        help='Download a model from ollama or HuggingFace.',
        description=(
            'Pulls an ollama model or downloads a HuggingFace model\n'
            'for use with mlx-lm or vllm-mlx.'
        ),
    )
    download_parser.add_argument(
        'provider',
        choices=PROVIDERS,
        help='Provider to download from.',
    )
    download_parser.add_argument(
        'model',
        help='Model identifier to download.',
    )
    download_parser.set_defaults(func=cmd_download)

    # -- provider ------------------------------------------------------------
    provider_parser = subparsers.add_parser(
        'provider',
        help='Provider management commands.',
    )
    provider_parser.set_defaults(
        func=lambda args, _: provider_parser.print_help()
    )
    provider_sub = provider_parser.add_subparsers(
        dest='provider_command',
        metavar='SUBCOMMAND',
    )

    info_parser = provider_sub.add_parser(
        'info',
        help='Show info for all providers.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Displays the executable path, default port, base URL,\n'
            'and model directory for every provider.'
        ),
    )
    info_parser.set_defaults(func=cmd_provider_info)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args, passthrough = parser.parse_known_args()

    if passthrough and args.command != 'run':
        print(
            f'Warning: ignoring unknown arguments: {passthrough}',
            file=sys.stderr,
        )

    args.func(args, passthrough)


if __name__ == '__main__':
    main()
