"""Local voice model manifest validation and SenseVoice warm-up helpers.

The module is intentionally side-effect free: importing it never reads a manifest,
loads a model, downloads a file, or imports ``sherpa_onnx``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import time
from typing import Any


SUPPORTED_SCHEMA_VERSION = 1
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|authorization|credential)",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"apikey_[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class ModelManifestError(ValueError):
    """The model manifest is malformed or violates the local-asset policy."""


class ModelValidationError(RuntimeError):
    """A configured model asset is missing or has invalid contents."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    path: Path
    purpose: str
    size_bytes: int | None
    sha256: str | None
    optional: bool = False


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    root: Path
    purpose: str
    runtime: str
    source: str | None
    optional: bool
    files: tuple[ModelFile, ...]


@dataclass(frozen=True, slots=True)
class ModelInventory:
    manifest_path: Path
    schema_version: int
    models: Mapping[str, ModelSpec]

    def require(self, name: str) -> ModelSpec:
        try:
            return self.models[name]
        except KeyError as exc:
            raise ModelManifestError(f"model is not listed in manifest: {name}") from exc


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    model: str
    file: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    ok: bool
    issues: tuple[ValidationIssue, ...]
    checked_files: int
    skipped_optional_files: int

    def raise_for_errors(self) -> None:
        if not self.ok:
            raise ModelValidationError("; ".join(issue.message for issue in self.issues))


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    model: str
    destination: Path
    source: str | None
    allowed: bool
    message: str


@dataclass(frozen=True, slots=True)
class WarmupResult:
    status: str
    loaded: bool
    warmed: bool
    load_seconds: float
    warmup_seconds: float
    total_seconds: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready"


def _manifest_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_ascii_path(path: Path, *, label: str) -> None:
    try:
        os.fspath(path).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ModelManifestError(f"{label} must be ASCII-only: {path}") from exc


def _require_external_root(path: Path, repository_root: Path) -> None:
    resolved = path.expanduser().resolve()
    repository_root = repository_root.expanduser().resolve()
    if resolved == repository_root or _is_relative_to(resolved, repository_root):
        raise ModelManifestError(
            f"model root must be outside repository: {resolved}"
        )


def _validate_file_name(value: Any, *, model: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelManifestError(f"{model}: file name must be a non-empty string")
    name = value.strip()
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(part in {"", ".", ".."} for part in windows.parts)
    ):
        raise ModelManifestError(f"{model}: unsafe model file path: {name}")
    try:
        name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ModelManifestError(
            f"{model}: model file path must be ASCII-only: {name}"
        ) from exc
    return name


def _scan_for_secrets(value: Any, *, location: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                raise ModelManifestError(
                    f"{location} must not contain API keys or secret field {key!r}"
                )
            _scan_for_secrets(nested, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _scan_for_secrets(nested, location=f"{location}[{index}]")
    elif isinstance(value, str) and any(
        pattern.search(value) for pattern in _CREDENTIAL_VALUE_PATTERNS
    ):
        raise ModelManifestError(
            f"{location} must not contain API keys or credential-like values"
        )


def _resolve_model_root(
    name: str,
    raw: Mapping[str, Any],
    *,
    repository_root: Path,
    env: Mapping[str, str],
) -> Path:
    env_name = raw.get("env")
    if not isinstance(env_name, str) or not env_name:
        raise ModelManifestError(f"{name}: env must name a path environment variable")
    if _SENSITIVE_KEY.search(env_name):
        raise ModelManifestError(f"{name}: env must not name an API key or secret")

    configured = env.get(env_name)
    if configured is not None:
        if not configured.strip():
            raise ModelManifestError(f"{name}: {env_name} must not be empty")
        root = Path(os.path.expandvars(os.path.expanduser(configured)))
    else:
        default_path = raw.get("default_path")
        if not isinstance(default_path, str) or not default_path.strip():
            raise ModelManifestError(
                f"{name}: set {env_name}; manifest default_path must be an absolute "
                "repository-external path"
            )
        root = Path(os.path.expandvars(os.path.expanduser(default_path)))

    if not root.is_absolute():
        raise ModelManifestError(f"{name}: model root must be absolute: {root}")
    root = root.resolve()
    _require_ascii_path(root, label=f"{name} model root")
    _require_external_root(root, repository_root)
    return root


def load_model_inventory(
    manifest_path: str | Path,
    *,
    repository_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ModelInventory:
    """Read and validate ``voice-models.json`` without touching model files."""

    manifest = _manifest_path(manifest_path)
    repo = (
        Path(repository_root).expanduser().resolve()
        if repository_root is not None
        else manifest.parent
    )
    environment = os.environ if env is None else env
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModelManifestError(f"cannot read model manifest {manifest}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ModelManifestError(f"invalid JSON in model manifest {manifest}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ModelManifestError("model manifest root must be an object")
    _scan_for_secrets(raw)
    version = raw.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ModelManifestError(
            f"unsupported model manifest schema_version {version!r}; "
            f"expected {SUPPORTED_SCHEMA_VERSION}"
        )
    raw_models = raw.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise ModelManifestError("model manifest models must be a non-empty object")

    models: dict[str, ModelSpec] = {}
    for name, raw_model in raw_models.items():
        if not isinstance(name, str) or not name or not isinstance(raw_model, dict):
            raise ModelManifestError("each model entry must be a named object")
        root = _resolve_model_root(
            name,
            raw_model,
            repository_root=repo,
            env=environment,
        )
        raw_files = raw_model.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ModelManifestError(f"{name}: files must be a non-empty array")

        files: list[ModelFile] = []
        seen: set[str] = set()
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                raise ModelManifestError(f"{name}: each file entry must be an object")
            file_name = _validate_file_name(raw_file.get("name"), model=name)
            canonical_name = file_name.casefold()
            if canonical_name in seen:
                raise ModelManifestError(f"{name}: duplicate model file {file_name}")
            seen.add(canonical_name)

            size = raw_file.get("size_bytes")
            if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
                raise ModelManifestError(
                    f"{name}/{file_name}: size_bytes must be a non-negative integer"
                )
            digest = raw_file.get("sha256")
            if digest is not None and (
                not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            ):
                raise ModelManifestError(
                    f"{name}/{file_name}: sha256 must be 64 hexadecimal characters"
                )
            candidate = (root / Path(file_name)).resolve()
            if not _is_relative_to(candidate, root):
                raise ModelManifestError(
                    f"{name}: model file escapes configured root: {file_name}"
                )
            files.append(
                ModelFile(
                    name=file_name,
                    path=candidate,
                    purpose=str(raw_file.get("purpose", "")),
                    size_bytes=size,
                    sha256=digest.lower() if digest else None,
                    optional=bool(raw_file.get("optional", False)),
                )
            )

        source = raw_model.get("source")
        if source is not None and not isinstance(source, str):
            raise ModelManifestError(f"{name}: source must be a string when provided")
        models[name] = ModelSpec(
            name=name,
            root=root,
            purpose=str(raw_model.get("purpose", "")),
            runtime=str(raw_model.get("runtime", "")),
            source=source,
            optional=bool(raw_model.get("optional", False)),
            files=tuple(files),
        )

    return ModelInventory(
        manifest_path=manifest,
        schema_version=version,
        models=models,
    )


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def validate_model_assets(
    inventory: ModelInventory,
    *,
    model_names: tuple[str, ...] | list[str] | None = None,
) -> ValidationReport:
    """Validate local files only; this function never performs network I/O."""

    names = tuple(model_names) if model_names is not None else tuple(inventory.models)
    issues: list[ValidationIssue] = []
    checked = 0
    skipped = 0
    for name in names:
        model = inventory.require(name)
        for asset in model.files:
            if not _is_relative_to(asset.path.resolve(), model.root.resolve()):
                issues.append(
                    ValidationIssue(
                        name,
                        asset.name,
                        "path_traversal",
                        f"{name}/{asset.name}: asset path escapes model root",
                    )
                )
                continue
            if not asset.path.is_file():
                if model.optional or asset.optional:
                    skipped += 1
                    continue
                issues.append(
                    ValidationIssue(
                        name,
                        asset.name,
                        "missing",
                        f"{name}/{asset.name}: missing file {asset.path}",
                    )
                )
                continue
            checked += 1
            actual_size = asset.path.stat().st_size
            if asset.size_bytes is not None and actual_size != asset.size_bytes:
                issues.append(
                    ValidationIssue(
                        name,
                        asset.name,
                        "size_mismatch",
                        f"{name}/{asset.name}: expected {asset.size_bytes} bytes, "
                        f"found {actual_size}",
                    )
                )
                continue
            if asset.sha256 is not None:
                actual_hash = _sha256_file(asset.path)
                if actual_hash != asset.sha256:
                    issues.append(
                        ValidationIssue(
                            name,
                            asset.name,
                            "sha256_mismatch",
                            f"{name}/{asset.name}: expected sha256 {asset.sha256}, "
                            f"found {actual_hash}",
                        )
                    )
    return ValidationReport(not issues, tuple(issues), checked, skipped)


def plan_model_download(
    inventory: ModelInventory,
    model_name: str,
) -> DownloadPlan:
    """Describe manual acquisition; arbitrary URL downloading is never executed."""

    model = inventory.require(model_name)
    if model.source:
        message = (
            f"automatic downloads are disabled; obtain {model_name} from the reviewed "
            f"source and place verified files in {model.root}"
        )
    else:
        message = (
            f"automatic downloads are disabled and {model_name} has no reviewed source; "
            f"place verified files in {model.root}"
        )
    return DownloadPlan(model_name, model.root, model.source, False, message)


def warmup_sensevoice(
    model_dir: str | Path,
    *,
    recognizer_factory: Callable[[Path], Any] | None = None,
    sample_rate: int = 16_000,
    silence_samples: int = 320,
    clock: Callable[[], float] = time.perf_counter,
) -> WarmupResult:
    """Load SenseVoice and decode a short silent waveform.

    ``recognizer_factory`` makes warm-up testable without importing native model
    libraries. If omitted, the existing lazy ``SenseVoiceRecognizer`` loader is
    used. Failures are returned as status data so callers can report them cleanly.
    """

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if silence_samples <= 0:
        raise ValueError("silence_samples must be positive")

    started = clock()
    loaded_at = started
    loaded = False
    try:
        if recognizer_factory is None:
            from .asr_local import SenseVoiceRecognizer

            wrapper = SenseVoiceRecognizer(model_dir)
            recognizer = wrapper._get_recognizer()
        else:
            recognizer = recognizer_factory(Path(model_dir))
        loaded = True
        loaded_at = clock()

        stream = recognizer.create_stream()
        accept_waveform = getattr(stream, "accept_waveform", None)
        decode_stream = getattr(recognizer, "decode_stream", None)
        if not callable(accept_waveform) or not callable(decode_stream):
            raise TypeError(
                "SenseVoice recognizer must support create_stream, "
                "stream.accept_waveform, and decode_stream"
            )
        try:
            import numpy as np

            silence: Any = np.zeros(silence_samples, dtype=np.float32)
        except ImportError:
            silence = [0.0] * silence_samples
        accept_waveform(sample_rate, silence)
        decode_stream(stream)
        finished = clock()
        return WarmupResult(
            status="ready",
            loaded=True,
            warmed=True,
            load_seconds=max(0.0, loaded_at - started),
            warmup_seconds=max(0.0, finished - loaded_at),
            total_seconds=max(0.0, finished - started),
        )
    except Exception as exc:
        finished = clock()
        return WarmupResult(
            status="error",
            loaded=loaded,
            warmed=False,
            load_seconds=max(0.0, loaded_at - started) if loaded else 0.0,
            warmup_seconds=max(0.0, finished - loaded_at) if loaded else 0.0,
            total_seconds=max(0.0, finished - started),
            error=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "DownloadPlan",
    "ModelFile",
    "ModelInventory",
    "ModelManifestError",
    "ModelSpec",
    "ModelValidationError",
    "SUPPORTED_SCHEMA_VERSION",
    "ValidationIssue",
    "ValidationReport",
    "WarmupResult",
    "load_model_inventory",
    "plan_model_download",
    "validate_model_assets",
    "warmup_sensevoice",
]
