from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


MAX_AI_STEPS = 6
MAX_FILE_SEARCH_RESULTS = 20
MAX_READ_LINES = 220
MAX_FILE_BYTES = 64_000
DEFAULT_AI_USER_AGENT = 'codex-rs/1.0.7'
SKIP_DIR_NAMES = {
    '.git',
    '.hg',
    '.svn',
    '.venv',
    'node_modules',
    '__pycache__',
    '.mypy_cache',
    '.pytest_cache',
    'dist',
    'build',
}


@dataclass(frozen=True)
class CodexModelConfig:
    model: str
    base_url: str
    responses_api_url: str
    api_key: str
    extra_headers: dict[str, str]


@dataclass(frozen=True)
class AiRequestContext:
    route_name: str
    tmux_session: str
    instruction: str
    author_name: str
    workspace_root: Path | None
    latest_output: str
    running: bool


def build_responses_api_url(base_url: str) -> str:
    normalized = base_url.rstrip('/')
    if normalized.endswith('/v1'):
        return f'{normalized}/responses'
    return f'{normalized}/v1/responses'


def load_codex_model_config(
    *,
    config_path: Path | None = None,
    auth_path: Path | None = None,
) -> CodexModelConfig:
    config_file = config_path or (Path.home() / '.codex/config.toml')
    auth_file = auth_path or (Path.home() / '.codex/auth.json')
    if not config_file.exists():
        raise ValueError(f'Codex config file not found: {config_file}')
    if not auth_file.exists():
        raise ValueError(f'Codex auth file not found: {auth_file}')

    config = tomllib.loads(config_file.read_text())
    auth = json.loads(auth_file.read_text())

    model = str(config.get('model', '')).strip()
    if not model:
        raise ValueError('Codex config missing model')

    provider_name = str(config.get('model_provider', '')).strip()
    providers = config.get('model_providers', {})
    provider_config = providers.get(provider_name, {}) if isinstance(providers, dict) and provider_name else {}
    base_url = str(provider_config.get('base_url', '')).strip()
    if not base_url:
        raise ValueError('Codex config missing provider base_url')

    wire_api = str(provider_config.get('wire_api', '')).strip()
    if wire_api and wire_api != 'responses':
        raise ValueError(f'Unsupported Codex provider wire_api: {wire_api}')

    raw_headers = provider_config.get('headers', {}) if isinstance(provider_config, dict) else {}
    extra_headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if normalized_key and normalized_value:
                extra_headers[normalized_key] = normalized_value

    api_key = str(auth.get('OPENAI_API_KEY', '')).strip()
    if not api_key:
        raise ValueError('Codex auth missing OPENAI_API_KEY')

    return CodexModelConfig(
        model=model,
        base_url=base_url,
        responses_api_url=build_responses_api_url(base_url),
        api_key=api_key,
        extra_headers=extra_headers,
    )


class AiCommandRunner:
    def __init__(
        self,
        *,
        model_config: CodexModelConfig | None = None,
        config_loader: Callable[[], CodexModelConfig] | None = None,
        post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self._model_config = model_config
        self._config_loader = config_loader or load_codex_model_config
        self._post_json = post_json or _post_json

    async def run(self, context: AiRequestContext) -> str:
        return await asyncio.to_thread(self._run_sync, context)

    def _run_sync(self, context: AiRequestContext) -> str:
        if context.workspace_root is None:
            return '当前无法确定 tmux 工作目录，暂时不能处理 `ai` 文件请求。'
        if not context.workspace_root.exists():
            return f'当前工作目录不存在：`{context.workspace_root}`'

        model_config = self._model_config or self._config_loader()
        workspace_tools = WorkspaceTools(context.workspace_root)
        response = self._request_response(
            model_config,
            {
                'model': model_config.model,
                'input': _build_responses_input(_build_ai_prompt(context)),
                'tools': _tool_definitions(),
            },
        )

        for _ in range(MAX_AI_STEPS):
            function_calls = _extract_function_calls(response)
            if not function_calls:
                text = _extract_text_response(response).strip()
                return text or 'AI 没有返回可发送的结果。'

            tool_outputs = []
            for function_call in function_calls:
                tool_outputs.append(
                    {
                        'type': 'function_call_output',
                        'call_id': function_call['call_id'],
                        'output': workspace_tools.execute(
                            name=function_call['name'],
                            arguments_json=function_call['arguments'],
                        ),
                    }
                )

            response = self._request_response(
                model_config,
                {
                    'model': model_config.model,
                    'previous_response_id': response['id'],
                    'input': tool_outputs,
                    'tools': _tool_definitions(),
                },
            )

        return 'AI 处理步数过多，已停止。请把问题描述得更具体一些。'

    def _request_response(self, model_config: CodexModelConfig, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            'Authorization': f'Bearer {model_config.api_key}',
            'Content-Type': 'application/json',
            'User-Agent': DEFAULT_AI_USER_AGENT,
        }
        headers.update(model_config.extra_headers)
        return self._post_json(model_config.responses_api_url, payload, headers)


class WorkspaceTools:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def execute(self, *, name: str, arguments_json: str) -> str:
        try:
            arguments = json.loads(arguments_json or '{}')
        except json.JSONDecodeError as exc:
            return f'工具参数解析失败：{type(exc).__name__}: {exc}'

        if name == 'search_files':
            return self.search_files(
                query=str(arguments.get('query', '')).strip(),
                limit=int(arguments.get('limit', MAX_FILE_SEARCH_RESULTS)),
            )
        if name == 'read_file':
            return self.read_file(
                path=str(arguments.get('path', '')).strip(),
                start_line=int(arguments.get('start_line', 1)),
                max_lines=int(arguments.get('max_lines', MAX_READ_LINES)),
            )
        return f'不支持的工具：{name}'

    def search_files(self, *, query: str, limit: int) -> str:
        normalized_limit = max(1, min(limit, MAX_FILE_SEARCH_RESULTS))
        lowered_terms = [term for term in query.lower().split() if term]
        matches: list[str] = []

        for dirpath, dirnames, filenames in os.walk(self.workspace_root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
            base = Path(dirpath)
            for filename in filenames:
                candidate = base / filename
                relative = candidate.relative_to(self.workspace_root).as_posix()
                haystack = relative.lower()
                if lowered_terms and not all(term in haystack for term in lowered_terms):
                    continue
                matches.append(relative)
                if len(matches) >= normalized_limit:
                    return json.dumps(
                        {
                            'workspace_root': str(self.workspace_root),
                            'query': query,
                            'matches': matches,
                            'truncated': True,
                        },
                        ensure_ascii=False,
                    )

        return json.dumps(
            {
                'workspace_root': str(self.workspace_root),
                'query': query,
                'matches': sorted(matches),
                'truncated': False,
            },
            ensure_ascii=False,
        )

    def read_file(self, *, path: str, start_line: int, max_lines: int) -> str:
        if not path:
            return '缺少 path 参数。'
        try:
            target = _resolve_child_path(self.workspace_root, path)
        except ValueError as exc:
            return str(exc)

        if not target.exists():
            return f'文件不存在：{path}'
        if not target.is_file():
            return f'目标不是文件：{path}'

        raw = target.read_bytes()
        if b'\x00' in raw:
            return f'文件看起来是二进制，无法直接作为文本发送：{path}'

        snippet = raw[:MAX_FILE_BYTES]
        try:
            text = snippet.decode('utf-8')
        except UnicodeDecodeError:
            try:
                text = snippet.decode('utf-8', errors='replace')
            except Exception as exc:
                return f'文件无法按文本读取：{type(exc).__name__}: {exc}'

        lines = text.splitlines()
        normalized_start = max(1, start_line)
        normalized_max_lines = max(1, min(max_lines, MAX_READ_LINES))
        excerpt = lines[normalized_start - 1 : normalized_start - 1 + normalized_max_lines]
        payload = {
            'path': target.relative_to(self.workspace_root).as_posix(),
            'start_line': normalized_start,
            'max_lines': normalized_max_lines,
            'truncated_by_bytes': len(raw) > MAX_FILE_BYTES,
            'truncated_by_lines': len(lines) > (normalized_start - 1 + normalized_max_lines),
            'content': '\n'.join(excerpt),
        }
        return json.dumps(payload, ensure_ascii=False)


def _build_ai_prompt(context: AiRequestContext) -> str:
    workspace_root = str(context.workspace_root) if context.workspace_root is not None else '(unknown)'
    latest_output = context.latest_output.strip() or '(empty)'
    running_text = 'yes' if context.running else 'no'
    return (
        '你是 Discord bridge 的本地 AI 助手。请优先使用工具确认文件位置并读取真实文件内容，不要编造路径。\n'
        '你只能在给定工作目录下查找和读取文件，不能访问工作目录之外的内容。\n'
        '如果用户要求“把文件发我”，请返回适合直接发到 Discord 的正文内容。\n'
        '如果文件过长，可以说明已截断，并提示用户继续索要下一段。\n'
        '请使用中文回答。\n\n'
        f'路由名: {context.route_name}\n'
        f'tmux session: {context.tmux_session}\n'
        f'用户: {context.author_name}\n'
        f'当前工作目录: {workspace_root}\n'
        f'Codex 当前是否忙碌: {running_text}\n'
        f'最近 tmux 输出:\n{latest_output}\n\n'
        f'用户请求: {context.instruction}'
    )


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            'type': 'function',
            'name': 'search_files',
            'description': 'Search for likely matching files inside the current workspace by file name or relative path substring.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_FILE_SEARCH_RESULTS},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
        },
        {
            'type': 'function',
            'name': 'read_file',
            'description': 'Read a UTF-8 text file inside the current workspace and return a bounded excerpt.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'start_line': {'type': 'integer', 'minimum': 1},
                    'max_lines': {'type': 'integer', 'minimum': 1, 'maximum': MAX_READ_LINES},
                },
                'required': ['path'],
                'additionalProperties': False,
            },
        },
    ]


def _extract_function_calls(response: dict[str, Any]) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for item in response.get('output', []):
        if item.get('type') != 'function_call':
            continue
        calls.append(
            {
                'call_id': str(item.get('call_id', '')),
                'name': str(item.get('name', '')),
                'arguments': str(item.get('arguments', '{}')),
            }
        )
    return calls


def _extract_text_response(response: dict[str, Any]) -> str:
    output_text = response.get('output_text')
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in response.get('output', []):
        if item.get('type') != 'message':
            continue
        for content in item.get('content', []):
            if content.get('type') not in {'output_text', 'text'}:
                continue
            text = content.get('text', '')
            if isinstance(text, dict):
                text = text.get('value', '')
            if text:
                chunks.append(str(text))
    return '\n'.join(chunks)


def _resolve_child_path(workspace_root: Path, raw_path: str) -> Path:
    candidate = (workspace_root / raw_path).resolve()
    if candidate == workspace_root or workspace_root in candidate.parents:
        return candidate
    raise ValueError(f'路径超出当前工作目录：{raw_path}')


def _build_responses_input(prompt: str) -> list[dict[str, Any]]:
    return [
        {
            'role': 'user',
            'content': [
                {
                    'type': 'input_text',
                    'text': prompt,
                }
            ],
        }
    ]


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    body = json.dumps(payload).encode('utf-8')
    request_obj = request.Request(url, data=body, headers=headers, method='POST')
    try:
        with request.urlopen(request_obj, timeout=90) as response:
            return json.loads(response.read().decode('utf-8'))
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'AI 请求失败：HTTP {exc.code}: {detail}') from exc
