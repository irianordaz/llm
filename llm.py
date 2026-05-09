#!/usr/bin/env python3
"""Unified CLI wrapper for ollama, mlx-lm, and vllm-mlx."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

RUNNERS = ['ollama', 'mlx-lm', 'vllm-mlx']

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

DEFAULT_CTX = 65000

MODEL_PARAMS = [
    'temperature',
    'top_p',
    'top_k',
    'min_p',
    'repeat_penalty',
    'presence_penalty',
]

MLX_LM_PARAM_FLAGS: dict[str, str | None] = {
    'temperature': '--temp',
    'top_p': '--top-p',
    'top_k': '--top-k',
    'min_p': '--min-p',
    'repeat_penalty': '--repetition-penalty',
    'presence_penalty': None,
}

VLLM_MLX_PARAM_FLAGS: dict[str, str | None] = {
    'temperature': '--temperature',
    'top_p': '--top-p',
    'top_k': '--top-k',
    'min_p': '--min-p',
    'repeat_penalty': '--repetition-penalty',
    'presence_penalty': '--presence-penalty',
}

# Library-level defaults used by each runner when a parameter is not
# specified. Surfaced in the dashboard's Model Settings dialog so users see
# what will actually be applied.
MLX_LM_DEFAULT_PARAMS: dict[str, float] = {
    'temperature': 0.0,
    'top_p': 1.0,
    'top_k': 0,
    'min_p': 0.0,
    'repeat_penalty': 1.0,
}

VLLM_MLX_DEFAULT_PARAMS: dict[str, float] = {
    'temperature': 1.0,
    'top_p': 1.0,
    'top_k': -1,
    'min_p': 0.0,
    'repeat_penalty': 1.0,
    'presence_penalty': 0.0,
}

RUNNER_MODEL_DIRS = {
    'ollama': OLLAMA_MODEL_DIR,
    'mlx-lm': HF_CACHE_DIR,
    'vllm-mlx': HF_CACHE_DIR,
}

DEFAULT_RUNNER_PATHS: dict[str, str] = {
    'ollama': '/usr/local/bin/ollama',
    'mlx-lm': '/opt/homebrew/bin/mlx_lm',
}

LLM_DIR = Path.home() / '.llm'
STATE_FILE = LLM_DIR / 'state.json'
CONFIG_FILE = LLM_DIR / 'config.json'

HELP_EPILOG = """
Workflow:

  1. Check what is installed and what models you have:
       llm runner info
       llm ls

  2. Download a model if needed:
       llm download ollama llama3.2
       llm download mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit

  3. Set a default so bare 'llm run' always works:
       llm default mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
       llm default                    # confirm

  4. Run and monitor:
       llm run                        # uses configured default
       llm ps                         # show runner, port, base URL
       llm stop                       # no arguments needed

Examples:

  Run a specific runner and model:
    llm run ollama llama3.2
    llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit
    llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit \\
        --host 0.0.0.0 --port 8081

  Forward runner-specific flags after the standard options:
    llm run mlx-lm mlx-community/Llama-3.2-3B-Instruct-4bit \\
        --port 8080 --chat-template chatml --max-tokens 2048
    llm run vllm-mlx mlx-community/Mistral-7B-v0.1-4bit \\
        --port 8080 --max-model-len 4096 --dtype float16

Default ports:
  ollama    11434      base URL: http://127.0.0.1:11434
  mlx-lm    8080       base URL: http://127.0.0.1:8080/v1
  vllm-mlx  8080       base URL: http://127.0.0.1:8080/v1

Model locations:
  ollama    ~/.ollama/models
  mlx-lm    ~/.cache/huggingface/hub
  vllm-mlx  ~/.cache/huggingface/hub

Runtime files:
  ~/.llm/state.json   active session  (runner, model, pid, port)
  ~/.llm/config.json  saved default   (runner, model)
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


def _get_runner_path(runner: str) -> str | None:
    """Return the user-configured path, then the built-in default, then None."""
    config = read_config()
    configured = config.get('runners', {}).get(runner, {}).get('path')
    if configured:
        return configured
    return DEFAULT_RUNNER_PATHS.get(runner)


def _is_pixi_env(path: str) -> bool:
    """Return True if path is a directory that contains a pixi.toml."""
    p = Path(path)
    return p.is_dir() and (p / 'pixi.toml').exists()


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pids_on_port(port: int) -> list[int]:
    """Return PIDs of processes listening on *port* (macOS/Linux via lsof)."""
    try:
        result = subprocess.run(
            ['lsof', '-ti', f':{port}', '-sTCP:LISTEN'],
            capture_output=True,
            text=True,
        )
        return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _kill_process(pid: int) -> None:
    """Send SIGTERM to the process group of *pid*, falling back to the PID itself."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def get_ollama_models() -> list[str]:
    path = _get_runner_path('ollama')
    binary = path if path and Path(path).is_file() else 'ollama'
    try:
        result = subprocess.run(
            [binary, 'ls'],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
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
# Model deletion
# ---------------------------------------------------------------------------


def delete_model(runner: str, model: str) -> tuple[bool, str]:
    """Delete a local model. Returns (success, message)."""
    if runner == 'ollama':
        path = _get_runner_path('ollama')
        binary = path if path and Path(path).is_file() else 'ollama'
        try:
            result = subprocess.run(
                [binary, 'rm', model],
                check=True,
                capture_output=True,
                text=True,
            )
            return True, f'Deleted ollama model: {model}'
        except FileNotFoundError:
            return False, 'ollama not found.'
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or err.stdout or str(err)).strip()
            return False, f'ollama rm failed: {detail}'

    if runner in ('mlx-lm', 'vllm-mlx'):
        # "org/model-name" → "models--org--model-name"
        dir_name = 'models--' + model.replace('/', '--')
        model_dir = HF_CACHE_DIR / dir_name
        if not model_dir.exists():
            return False, f'Model directory not found: {model_dir}'
        try:
            shutil.rmtree(model_dir)
            return True, f'Deleted {model}'
        except OSError as err:
            return False, f'Failed to delete {model_dir}: {err}'

    return False, f'Unknown runner: {runner}'


# ---------------------------------------------------------------------------
# Subcommand: rm
# ---------------------------------------------------------------------------


def cmd_rm(args: argparse.Namespace, _: list[str]) -> None:
    runner = args.runner
    model = args.model
    success, msg = delete_model(runner, model)
    if success:
        print(msg)
    else:
        print(f'Error: {msg}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: ls
# ---------------------------------------------------------------------------


def cmd_ls(args: argparse.Namespace, _: list[str]) -> None:
    ollama_models = get_ollama_models()
    hf_models = get_huggingface_models()

    rows: list[tuple[str, str]] = [('ollama', m) for m in ollama_models] + [
        ('mlx-lm / vllm-mlx', m) for m in hf_models
    ]

    if not rows:
        print('No local models found.')
        return

    run_col = max((len(p) for p, _ in rows), default=0)
    run_col = max(run_col, len('RUNNER'))
    model_col = max((len(m) for _, m in rows), default=0)
    model_col = max(model_col, len('MODEL'))

    print(f'{"RUNNER":<{run_col}}  {"MODEL":<{model_col}}')
    print(f'{"-" * run_col}  {"-" * model_col}')
    for runner, model in rows:
        print(f'{runner:<{run_col}}  {model:<{model_col}}')


# ---------------------------------------------------------------------------
# Subcommand: ps
# ---------------------------------------------------------------------------


def cmd_ps(args: argparse.Namespace, _: list[str]) -> None:
    state = read_state()
    if not state:
        print('No model currently running.')
        return

    runner = state.get('runner', 'unknown')
    model = state.get('model', 'unknown')
    host = state.get('host', DEFAULT_HOST)
    port = state.get('port', DEFAULT_PORTS.get(runner, '?'))
    pid = state.get('pid')
    started_at = state.get('started_at', 'unknown')

    running = is_process_alive(pid) if pid else False
    status = 'running' if running else 'stopped (stale state)'

    base_url = BASE_URL_TEMPLATES.get(runner, 'http://{host}:{port}').format(
        host=host, port=port
    )

    w = 10
    print(f'{"Runner":<{w}} {runner}')
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


def _resolve_runner_model(
    args: argparse.Namespace,
) -> tuple[str, str]:
    runner = args.runner
    model = args.model

    if runner and model:
        if runner not in RUNNERS:
            print(
                f'Error: unknown runner "{runner}". '
                f'Choose from: {", ".join(RUNNERS)}',
                file=sys.stderr,
            )
            sys.exit(1)
        return runner, model

    if runner and not model:
        print(
            'Error: a model name is required when a runner is given.',
            file=sys.stderr,
        )
        sys.exit(1)

    config = read_config()
    default_runner = config.get('default_runner')
    default_model = config.get('default_model')

    if not default_runner or not default_model:
        print(
            'Error: no runner/model given and no default is configured.\n'
            'Set one with: llm default <runner> <model>',
            file=sys.stderr,
        )
        sys.exit(1)

    return default_runner, default_model


def get_ollama_model_params(model: str) -> dict[str, float]:
    """Read default PARAMETER values from an ollama Modelfile."""
    path = _get_runner_path('ollama')
    binary = path if path and Path(path).is_file() else 'ollama'
    try:
        result = subprocess.run(
            [binary, 'show', '--modelfile', model],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        params: dict[str, float] = {}
        for line in result.stdout.splitlines():
            m = re.match(r'^PARAMETER\s+(\S+)\s+(.+)$', line.strip())
            if m:
                name, value = m.group(1), m.group(2).strip()
                if name in MODEL_PARAMS:
                    try:
                        params[name] = float(value)
                    except ValueError:
                        pass
        return params
    except Exception:
        return {}


def get_runner_default_params(
    runner: str, model: str
) -> dict[str, float]:
    """Default parameter values applied by ``runner`` when a value is not
    explicitly set. For ollama, these are read from the model's Modelfile;
    for mlx-lm and vllm-mlx they come from the upstream library defaults."""
    if runner == 'ollama':
        return get_ollama_model_params(model)
    if runner == 'mlx-lm':
        return dict(MLX_LM_DEFAULT_PARAMS)
    if runner == 'vllm-mlx':
        return dict(VLLM_MLX_DEFAULT_PARAMS)
    return {}


def _get_ollama_custom_model(base_model: str, params: dict) -> str:
    """Idempotently create an ollama model with custom parameters. Returns the model name."""
    if not params:
        return base_model
    param_lines = '\n'.join(
        f'PARAMETER {k} {v}' for k, v in sorted(params.items())
    )
    modelfile = f'FROM {base_model}\n{param_lines}\n'
    h = hashlib.sha1(modelfile.encode()).hexdigest()[:8]
    safe = base_model.replace('/', '-').replace(':', '-')
    custom_name = f'llm-{safe}-{h}'
    try:
        subprocess.run(
            ['ollama', 'create', custom_name, '-f', '-'],
            input=modelfile,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return base_model
    return custom_name


def search_huggingface_models(
    query: str, filter_tags: str = '', limit: int = 30
) -> list[dict]:
    """Search HuggingFace Hub for models. filter_tags is comma-separated.
    Returns list of {id, downloads, likes, size_bytes}."""
    params_list: list[tuple[str, str]] = [
        ('search', query),
        ('limit', str(limit)),
        ('sort', 'downloads'),
        ('direction', '-1'),
        ('full', 'true'),
        ('blobs', 'true'),
    ]
    for tag in [t.strip() for t in filter_tags.split(',') if t.strip()]:
        params_list.append(('filter', tag))
    url = 'https://huggingface.co/api/models?' + urllib.parse.urlencode(
        params_list
    )
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'llm-wrapper/1.0'}
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        results = []
        for m in data:
            size_bytes = 0
            for sib in m.get('siblings', []):
                # direct size field (available with blobs=true)
                direct = sib.get('size') or 0
                # LFS pointer size (available with full=true)
                lfs_size = (sib.get('lfs') or {}).get('size') or 0
                size_bytes += direct or lfs_size
            # top-level fallback if siblings have no size info
            if not size_bytes:
                size_bytes = m.get('usedStorage') or 0
            results.append(
                {
                    'id': m.get('modelId', m.get('id', '')),
                    'downloads': m.get('downloads', 0),
                    'likes': m.get('likes', 0),
                    'size_bytes': size_bytes,
                }
            )
        return results
    except Exception:
        return []


def _hf_download_cmd(model: str) -> list[str]:
    """Return the command to download a HuggingFace model, preferring the newer 'hf' binary."""
    # GUI/frozen apps inherit a minimal macOS PATH that omits Homebrew and user dirs,
    # so shutil.which may fail even when the binary exists. Probe known locations.
    _FALLBACK_DIRS = [
        '/opt/homebrew/bin',
        '/usr/local/bin',
        str(Path.home() / '.local' / 'bin'),
        str(Path.home() / '.pixi' / 'bin'),
    ]

    def _find(name: str) -> str | None:
        found = shutil.which(name)
        if found:
            return found
        for d in _FALLBACK_DIRS:
            candidate = Path(d) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    binary = _find('hf') or _find('huggingface-cli') or 'hf'
    return [binary, 'download', model]


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n <= 0:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024  # type: ignore[assignment]
    return f'{n:.1f} PB'


def _pixi_executable() -> str:
    found = shutil.which('pixi')
    if found:
        return found
    default = Path.home() / '.pixi' / 'bin' / 'pixi'
    if default.is_file():
        return str(default)
    return 'pixi'


def _python_executable() -> str:
    if getattr(sys, 'frozen', False):
        python3 = shutil.which('python3')
        if not python3:
            print(
                'Error: python3 not found on PATH.\n'
                'Install Python 3 to use mlx-lm or vllm-mlx.',
                file=sys.stderr,
            )
            sys.exit(1)
        return python3
    return sys.executable


def _build_run_cmd(
    runner: str,
    model: str,
    host: str,
    port: int,
    passthrough: list[str],
    ctx: int | None = None,
    params: dict | None = None,
) -> tuple[list[str], str | None]:
    """Return (command, cwd). cwd is set only when a pixi env directory is used."""
    if runner == 'ollama':
        path = _get_runner_path('ollama')
        binary = path if path and Path(path).is_file() else 'ollama'
        # ctx via OLLAMA_NUM_CTX env var; params via custom model name (_get_ollama_custom_model).
        return [binary, 'run', model, '--keepalive', '-1'] + passthrough, None

    def _param_flags(flag_map: dict) -> list[str]:
        flags: list[str] = []
        if params:
            for k, v in params.items():
                flag = flag_map.get(k)
                if flag is not None:
                    flags.extend([flag, str(v)])
        return flags

    if runner == 'mlx-lm':
        path = _get_runner_path('mlx-lm')
        ctx_flags = ['--max-tokens', str(ctx)] if ctx is not None else []
        if path and Path(path).is_file():
            cmd = [
                path,
                'server',
                '--model',
                model,
                '--host',
                host,
                '--port',
                str(port),
            ]
        else:
            cmd = [
                _python_executable(),
                '-m',
                'mlx_lm.server',
                '--model',
                model,
                '--host',
                host,
                '--port',
                str(port),
            ]
        return cmd + ctx_flags + _param_flags(
            MLX_LM_PARAM_FLAGS
        ) + passthrough, None

    if runner == 'vllm-mlx':
        path = _get_runner_path('vllm-mlx')
        ctx_flags = ['--max-model-len', str(ctx)] if ctx is not None else []
        if path and _is_pixi_env(path):
            cmd = [
                _pixi_executable(),
                'run',
                'vllm-mlx',
                'serve',
                model,
                '--host',
                host,
                '--port',
                str(port),
            ]
            return cmd + ctx_flags + _param_flags(
                VLLM_MLX_PARAM_FLAGS
            ) + passthrough, path
        cmd = ['vllm', 'serve', model, '--host', host, '--port', str(port)]
        return cmd + ctx_flags + _param_flags(
            VLLM_MLX_PARAM_FLAGS
        ) + passthrough, None

    print(f'Unknown runner: {runner}', file=sys.stderr)
    sys.exit(1)


def cmd_run(args: argparse.Namespace, passthrough: list[str]) -> None:
    current = read_state()
    if current:
        pid = current.get('pid')
        if pid and is_process_alive(pid):
            print(
                f'Error: {current["runner"]} is already running '
                f'({current["model"]}).\n'
                'Stop it first with: llm stop',
                file=sys.stderr,
            )
            sys.exit(1)
        clear_state()

    runner, model = _resolve_runner_model(args)
    host = getattr(args, 'host', DEFAULT_HOST)
    port_arg = getattr(args, 'port', None)
    port = port_arg if port_arg is not None else DEFAULT_PORTS[runner]
    ctx = getattr(args, 'ctx', DEFAULT_CTX)

    params: dict = {}
    for pname in MODEL_PARAMS:
        val = getattr(args, pname, None)
        if val is not None:
            params[pname] = val

    env = os.environ.copy()
    if runner == 'ollama':
        env['OLLAMA_HOST'] = f'{host}:{port}'
        if ctx is not None:
            env['OLLAMA_NUM_CTX'] = str(ctx)
        if params:
            model = _get_ollama_custom_model(model, params)

    cmd, cwd = _build_run_cmd(
        runner,
        model,
        host,
        port,
        passthrough,
        ctx,
        params if runner != 'ollama' else None,
    )
    print(f'Starting {runner}: {model}  ({host}:{port})')

    try:
        proc = subprocess.Popen(cmd, env=env, cwd=cwd)
    except FileNotFoundError:
        print(
            f'Error: {runner} binary not found. Is it installed?',
            file=sys.stderr,
        )
        sys.exit(1)

    write_state(
        {
            'runner': runner,
            'model': model,
            'host': host,
            'port': port,
            'pid': proc.pid,
            'started_at': datetime.now().isoformat(timespec='seconds'),
        }
    )

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        print(f'\n{runner} stopped.')
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

    runner = state.get('runner')
    model = state.get('model')
    pid = state.get('pid')
    port = state.get('port')

    if pid and is_process_alive(pid):
        _kill_process(pid)
        print(f'Sent SIGTERM to PID {pid}.')
    elif port:
        # Model was detected externally (no PID stored) — find it by port.
        port_pids = _pids_on_port(port)
        if port_pids:
            for p in port_pids:
                _kill_process(p)
            print(f'Sent SIGTERM to processes on port {port}: {port_pids}')
        else:
            print(f'No process found on port {port}.')
    else:
        print('No PID or port available to signal.')

    if runner == 'ollama' and model:
        o_path = _get_runner_path('ollama')
        binary = o_path if o_path and Path(o_path).is_file() else 'ollama'
        try:
            subprocess.run(
                [binary, 'stop', model], check=True, capture_output=True
            )
            print(f'Unloaded ollama model: {model}')
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    clear_state()
    print(f'Stopped {runner}: {model}')


# ---------------------------------------------------------------------------
# Subcommand: default
# ---------------------------------------------------------------------------


def cmd_default(args: argparse.Namespace, _: list[str]) -> None:
    runner = args.runner
    model = args.model

    if not runner and not model:
        config = read_config()
        dp = config.get('default_runner', '(not set)')
        dm = config.get('default_model', '(not set)')
        print(f'Default runner: {dp}')
        print(f'Default model:    {dm}')
        return

    if not runner or not model:
        print(
            'Error: both runner and model are required.\n'
            'Usage: llm default <runner> <model>',
            file=sys.stderr,
        )
        sys.exit(1)

    if runner not in RUNNERS:
        print(
            f'Error: unknown runner "{runner}". '
            f'Choose from: {", ".join(RUNNERS)}',
            file=sys.stderr,
        )
        sys.exit(1)

    config = read_config()
    config['default_runner'] = runner
    config['default_model'] = model
    write_config(config)
    print(f'Default set: {runner} / {model}')


# ---------------------------------------------------------------------------
# Subcommand: download
# ---------------------------------------------------------------------------


def cmd_download(args: argparse.Namespace, _: list[str]) -> None:
    runner = args.runner
    model = args.model

    if runner == 'ollama':
        path = _get_runner_path('ollama')
        binary = path if path and Path(path).is_file() else 'ollama'
        cmd = [binary, 'pull', model]
        missing_hint = 'Install ollama from https://ollama.com'
    else:
        cmd = _hf_download_cmd(model)
        missing_hint = (
            'Install the HuggingFace CLI with: brew install huggingface-cli'
        )

    print(f'Downloading {model} via {runner}...')
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print(f'Error: command not found.\n{missing_hint}', file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as err:
        print(f'Download failed: {err}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: runner info
# ---------------------------------------------------------------------------


def _runner_executable(runner: str) -> tuple[str, bool]:
    if runner == 'ollama':
        path = _get_runner_path('ollama')
        if path:
            exists = Path(path).is_file() and os.access(path, os.X_OK)
            return (path, exists)
        fallback = shutil.which('ollama')
        return (fallback, True) if fallback else ('not found', False)

    if runner == 'mlx-lm':
        path = _get_runner_path('mlx-lm')
        if path:
            exists = Path(path).is_file() and os.access(path, os.X_OK)
            return (path, exists)
        spec = importlib.util.find_spec('mlx_lm')
        return (f'{sys.executable} -m mlx_lm.server', spec is not None)

    if runner == 'vllm-mlx':
        path = _get_runner_path('vllm-mlx')
        if path:
            if _is_pixi_env(path):
                return (f'pixi run vllm-mlx  (cwd: {path})', True)
            exists = Path(path).is_file() and os.access(path, os.X_OK)
            return (path, exists)
        vllm = shutil.which('vllm')
        return (vllm, True) if vllm else ('not found', False)

    return ('unknown', False)


def get_model_settings(runner: str, model: str) -> dict:
    """Return saved host/port for a model, or empty dict if none saved."""
    config = read_config()
    return config.get('model_settings', {}).get(runner, {}).get(model, {})


def save_model_settings(
    runner: str,
    model: str,
    host: str,
    port: int,
    ctx: int | None = None,
    params: dict | None = None,
) -> None:
    """Persist host/port/ctx/params for a model so future runs reuse them."""
    config = read_config()
    settings: dict = {
        'host': host,
        'port': port,
        'ctx': ctx if ctx is not None else DEFAULT_CTX,
    }
    if params:
        settings['params'] = params
    (
        config.setdefault('model_settings', {}).setdefault(runner, {})[model]
    ) = settings
    write_config(config)


def discover_running_models() -> list[dict]:
    """Probe runner endpoints to detect running models started outside llm."""
    results: list[dict] = []
    queried_ports: set[int] = set()

    # Check ollama — /api/ps returns only models currently loaded in memory.
    # /api/tags would list all downloaded models regardless of running state.
    try:
        port = DEFAULT_PORTS['ollama']
        resp = urllib.request.urlopen(
            f'http://{DEFAULT_HOST}:{port}/api/ps',
            timeout=1,
        )
        data = json.loads(resp.read())
        queried_ports.add(port)
        for model in data.get('models', []):
            results.append(
                {
                    'runner': 'ollama',
                    'model': model['name'],
                    'host': DEFAULT_HOST,
                    'port': port,
                }
            )
    except Exception:
        pass

    # Check mlx-lm / vllm-mlx — they may share a port; query each unique port once.
    for runner in ('mlx-lm', 'vllm-mlx'):
        port = DEFAULT_PORTS[runner]
        if port in queried_ports:
            continue
        queried_ports.add(port)
        try:
            resp = urllib.request.urlopen(
                f'http://{DEFAULT_HOST}:{port}/v1/models',
                timeout=1,
            )
            data = json.loads(resp.read())
            for model in data.get('data', []):
                results.append(
                    {
                        'runner': runner,
                        'model': model['id'],
                        'host': DEFAULT_HOST,
                        'port': port,
                    }
                )
        except Exception:
            pass

    return results


def cmd_runner_info(args: argparse.Namespace, _: list[str]) -> None:
    w = 12
    for i, runner in enumerate(RUNNERS):
        if i > 0:
            print()
        executable, installed = _runner_executable(runner)
        port = DEFAULT_PORTS[runner]
        base_url = BASE_URL_TEMPLATES[runner].format(
            host=DEFAULT_HOST, port=port
        )
        model_dir = RUNNER_MODEL_DIRS[runner]
        status = 'installed' if installed else 'not installed'

        print(runner)
        print(f'  {"Executable":<{w}}  {executable}')
        print(f'  {"Default port":<{w}}  {port}')
        print(f'  {"Base URL":<{w}}  {base_url}')
        print(f'  {"Model dir":<{w}}  {model_dir}')
        print(f'  {"Status":<{w}}  {status}')


# ---------------------------------------------------------------------------
# Subcommand: gui
# ---------------------------------------------------------------------------


def cmd_gui(args: argparse.Namespace, _: list[str]) -> None:
    try:
        import llm_dashboard
    except ImportError:
        print(
            'Error: GUI requires wxPython.\n'
            'Install with: pip install wxPython   (or: pixi install)',
            file=sys.stderr,
        )
        sys.exit(1)
    llm_dashboard.main()


# ---------------------------------------------------------------------------
# Subcommand: runner set
# ---------------------------------------------------------------------------


def cmd_runner_set(args: argparse.Namespace, _: list[str]) -> None:
    runner = args.runner
    path = args.path
    config = read_config()
    config.setdefault('runners', {}).setdefault(runner, {})['path'] = path
    write_config(config)
    print(f'Set {runner} path: {path}')


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
            'Port number. Defaults: ollama=11434, mlx-lm=8080, vllm-mlx=8080.'
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='llm',
        description=(
            'Unified wrapper for ollama, mlx-lm, and vllm-mlx.\n'
            'Run any model with a consistent interface across all\n'
            'three runners. Only one model runs at a time;\n'
            'session state is persisted in ~/.llm/state.json.'
        ),
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
            'Omit runner and model to run the configured default.\n'
            'Unknown flags are forwarded directly to the runner binary.\n'
            'Only one model may run at a time.'
        ),
    )
    run_parser.add_argument(
        'runner',
        nargs='?',
        metavar='RUNNER',
        help=f'Runner: {", ".join(RUNNERS)}.',
    )
    run_parser.add_argument(
        'model',
        nargs='?',
        metavar='MODEL',
        help='Model identifier.',
    )
    _add_network_args(run_parser)
    run_parser.add_argument(
        '--ctx',
        type=int,
        default=DEFAULT_CTX,
        metavar='N',
        help=(
            f'Context window length (default: {DEFAULT_CTX}). '
            'Maps to --num-ctx (ollama), --max-tokens (mlx-lm), '
            '--max-model-len (vllm-mlx).'
        ),
    )
    run_parser.add_argument(
        '--temperature',
        type=float,
        default=None,
        metavar='N',
        help='Sampling temperature.',
    )
    run_parser.add_argument(
        '--top-p',
        dest='top_p',
        type=float,
        default=None,
        metavar='N',
        help='Top-p (nucleus) sampling.',
    )
    run_parser.add_argument(
        '--top-k',
        dest='top_k',
        type=int,
        default=None,
        metavar='N',
        help='Top-k sampling.',
    )
    run_parser.add_argument(
        '--min-p',
        dest='min_p',
        type=float,
        default=None,
        metavar='N',
        help='Min-p sampling.',
    )
    run_parser.add_argument(
        '--repeat-penalty',
        dest='repeat_penalty',
        type=float,
        default=None,
        metavar='N',
        help='Repetition penalty.',
    )
    run_parser.add_argument(
         '--presence-penalty',
        dest='presence_penalty',
        type=float,
        default=None,
        metavar='N',
        help='Presence penalty.',
    )
    run_parser.add_argument(
         '--detach',
        action='store_true',
        default=False,
        help='Run the model as a persistent background process that survives CLI exit.',
    )
    run_parser.add_argument(
         '--log',
        default=None,
        metavar='PATH',
        help=(
            'Log file path for detached runs (default: ~/.llm/logs/run.log).'
        ),
    )
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
        help='Get or set the default runner and model.',
        description=(
            'With no arguments, shows the current default.\n'
            'With runner and model, saves a new default.'
        ),
    )
    default_parser.add_argument(
        'runner',
        nargs='?',
        metavar='RUNNER',
        help=f'Runner: {", ".join(RUNNERS)}.',
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
        'runner',
        choices=RUNNERS,
        help='Runner to download from.',
    )
    download_parser.add_argument(
        'model',
        help='Model identifier to download.',
    )
    download_parser.set_defaults(func=cmd_download)

    # -- rm ------------------------------------------------------------------
    rm_parser = subparsers.add_parser(
        'rm',
        help='Delete a local model.',
        description=(
            'Removes a local model from disk.\n'
            'For ollama, calls "ollama rm <model>".\n'
            'For mlx-lm / vllm-mlx, deletes the HuggingFace cache directory\n'
            '(~/.cache/huggingface/hub/models--<org>--<name>).\n'
            'Because mlx-lm and vllm-mlx share the same cache, deleting\n'
            'via either runner removes it for both.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rm_parser.add_argument(
        'runner',
        choices=RUNNERS,
        help='Runner the model belongs to.',
    )
    rm_parser.add_argument(
        'model',
        help='Model identifier to delete.',
    )
    rm_parser.set_defaults(func=cmd_rm)

    # -- runner ------------------------------------------------------------
    runner_parser = subparsers.add_parser(
        'runner',
        help='Runner management commands.',
    )
    runner_parser.set_defaults(
        func=lambda args, _: runner_parser.print_help()
    )
    runner_sub = runner_parser.add_subparsers(
        dest='runner_command',
        metavar='SUBCOMMAND',
    )

    info_parser = runner_sub.add_parser(
        'info',
        help='Show info for all runners.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Displays the executable path, default port, base URL,\n'
            'and model directory for every runner.'
        ),
    )
    info_parser.set_defaults(func=cmd_runner_info)

    set_parser = runner_sub.add_parser(
        'set',
        help='Set the executable path for a runner.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Configure the path to a runner executable or directory.\n'
            'For a pixi-managed runner, set the path to the directory\n'
            'containing pixi.toml — llm will invoke it with pixi run.\n\n'
            'Examples:\n'
            '  llm runner set mlx-lm /opt/homebrew/bin/mlx_lm\n'
            '  llm runner set vllm-mlx /Users/you/local/vllm-mlx'
        ),
    )
    set_parser.add_argument(
        'runner',
        choices=RUNNERS,
        help='Runner to configure.',
    )
    set_parser.add_argument(
        'path',
        metavar='PATH',
        help=(
            'Path to the runner executable, or to a directory '
            'containing pixi.toml for pixi-managed runners.'
        ),
    )
    set_parser.set_defaults(func=cmd_runner_set)

    # -- gui -----------------------------------------------------------------
    gui_parser = subparsers.add_parser(
        'gui',
        help='Launch the desktop GUI (requires wxPython).',
        description=(
            'Opens a light-themed wxPython window with all CLI features:\n'
            'live status of the running model, model list, run/stop/download,\n'
            'default selection, and runner configuration.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gui_parser.set_defaults(func=cmd_gui)

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
