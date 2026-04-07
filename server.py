from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, UnidentifiedImageError


BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / "projects"
DEFAULT_ASPECT_RATIO = "1:1"
DEFAULT_RESOLUTION = "1K"
DEFAULT_PAGE_COUNT = 5
ALLOWED_ASPECT_RATIOS = {"1:1", "4:5", "9:16", "16:9", "21:9", "3:4"}
ALLOWED_RESOLUTIONS = {"1K", "2K", "4K"}
BATCH_ACTIVE_STATES = {"queued", "running", "cancelling"}
BATCH_TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
SLIDE_GENERATION_STATES = {
    "idle",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
}

load_dotenv(BASE_DIR / ".env")
PROJECTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024
PROJECT_IO_LOCK = threading.RLock()
JOB_REGISTRY_LOCK = threading.Lock()
ACTIVE_BATCH_JOBS: dict[str, dict[str, Any]] = {}


class ProjectError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_batch_generation(page_count: int) -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "idle",
        "total_slides": coerce_page_count(page_count),
        "processed_slides": 0,
        "completed_slides": 0,
        "failed_slides": 0,
        "current_slide_index": None,
        "started_at": None,
        "finished_at": None,
        "last_error": None,
        "message": "",
    }


def slugify_project_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw_name.strip().lower()).strip("-")
    if not name:
        raise ProjectError("Project name is required.")
    return name


def coerce_page_count(value: Any) -> int:
    try:
        page_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectError("Page count must be an integer.") from exc

    if page_count < 1:
        raise ProjectError("Page count must be at least 1.")
    if page_count > 50:
        raise ProjectError("Page count must be 50 or fewer.")
    return page_count


def validate_aspect_ratio(value: Any) -> str:
    aspect_ratio = str(value or DEFAULT_ASPECT_RATIO)
    if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        raise ProjectError(f"Unsupported aspect ratio: {aspect_ratio}")
    return aspect_ratio


def validate_resolution(value: Any) -> str:
    resolution = str(value or DEFAULT_RESOLUTION)
    if resolution not in ALLOWED_RESOLUTIONS:
        raise ProjectError(f"Unsupported resolution: {resolution}")
    return resolution


def default_slide(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "prompt": "",
        "fixed_text_prompt_override": None,
        "images": [],
        "generated": False,
        "filename": f"s{index}.png",
        "generation_status": "idle",
        "generation_error": None,
    }


def project_dir(project_name: str) -> Path:
    name = slugify_project_name(project_name)
    path = (PROJECTS_DIR / name).resolve()
    if path.parent != PROJECTS_DIR.resolve():
        raise ProjectError("Invalid project path.")
    return path


def project_json_path(project_name: str) -> Path:
    return project_dir(project_name) / "project.json"


def create_project_payload(
    name: str,
    page_count: int = DEFAULT_PAGE_COUNT,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    resolution: str = DEFAULT_RESOLUTION,
    fixed_text_prompt: str = "",
) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "name": slugify_project_name(name),
        "created_at": timestamp,
        "updated_at": timestamp,
        "aspect_ratio": validate_aspect_ratio(aspect_ratio),
        "resolution": validate_resolution(resolution),
        "fixed_text_prompt": str(fixed_text_prompt or ""),
        "fixed_images": [],
        "slides": [default_slide(i) for i in range(1, coerce_page_count(page_count) + 1)],
        "batch_generation": default_batch_generation(page_count),
    }


def save_project(project: dict[str, Any]) -> None:
    with PROJECT_IO_LOCK:
        name = slugify_project_name(project["name"])
        folder = project_dir(name)
        folder.mkdir(exist_ok=True)
        project["updated_at"] = now_iso()
        project_json_path(name).write_text(json.dumps(project, indent=2), encoding="utf-8")


def project_folder_has_assets(folder: Path) -> bool:
    for child in folder.iterdir():
        if child.name == "project.json" or child.name.startswith("."):
            continue
        return True
    return False


def project_has_meaningful_content(project: dict[str, Any]) -> bool:
    if str(project.get("fixed_text_prompt", "")).strip():
        return True
    if project.get("fixed_images"):
        return True

    for slide in project.get("slides", []):
        if str(slide.get("prompt", "")).strip():
            return True
        if slide.get("fixed_text_prompt_override") not in {None, ""}:
            return True
        if slide.get("images"):
            return True
        if slide.get("generated"):
            return True

    return False


def project_delete_warning(has_assets: bool) -> str:
    if has_assets:
        return "this project has assets are you sure"
    return "This project has content. Are you sure you want to delete it?"


def invalid_project_stub(project_name: str, project_error: str, has_assets: bool, updated_at: str | None = None) -> dict[str, Any]:
    timestamp = updated_at or now_iso()
    is_empty = not has_assets
    return {
        "name": slugify_project_name(project_name),
        "created_at": timestamp,
        "updated_at": timestamp,
        "aspect_ratio": DEFAULT_ASPECT_RATIO,
        "resolution": DEFAULT_RESOLUTION,
        "fixed_text_prompt": "",
        "fixed_images": [],
        "slides": [],
        "page_count": 0,
        "batch_generation": default_batch_generation(1),
        "is_valid": False,
        "project_error": project_error,
        "is_empty": is_empty,
        "has_assets": has_assets,
        "delete_requires_confirmation": not is_empty,
        "delete_warning": project_delete_warning(has_assets) if not is_empty else "",
    }


def get_runtime_job(project_name: str) -> dict[str, Any] | None:
    with JOB_REGISTRY_LOCK:
        return ACTIVE_BATCH_JOBS.get(slugify_project_name(project_name))


def has_active_batch_job(project_name: str, job_id: str | None = None) -> bool:
    runtime_job = get_runtime_job(project_name)
    if not runtime_job:
        return False
    if job_id is not None and runtime_job["job_id"] != job_id:
        return False
    return runtime_job["status"] in BATCH_ACTIVE_STATES


def normalize_slide_generation_state(raw_state: Any) -> str:
    state = str(raw_state or "idle")
    if state not in SLIDE_GENERATION_STATES:
        return "idle"
    return state


def normalize_batch_generation(project: dict[str, Any]) -> dict[str, Any]:
    slides = project.get("slides", [])
    batch_generation_value = project.get("batch_generation")
    raw_batch: dict[str, Any] = batch_generation_value if isinstance(batch_generation_value, dict) else {}
    batch = default_batch_generation(max(1, len(slides) or DEFAULT_PAGE_COUNT))
    batch.update(
        {
            "job_id": raw_batch.get("job_id"),
            "status": str(raw_batch.get("status") or "idle"),
            "processed_slides": int(raw_batch.get("processed_slides") or 0),
            "completed_slides": int(raw_batch.get("completed_slides") or 0),
            "failed_slides": int(raw_batch.get("failed_slides") or 0),
            "current_slide_index": raw_batch.get("current_slide_index"),
            "started_at": raw_batch.get("started_at"),
            "finished_at": raw_batch.get("finished_at"),
            "last_error": raw_batch.get("last_error"),
            "message": str(raw_batch.get("message") or ""),
        }
    )
    batch["total_slides"] = len(slides)
    if batch["status"] not in {"idle", *BATCH_ACTIVE_STATES, *BATCH_TERMINAL_STATES}:
        batch["status"] = "idle"

    job_id = batch.get("job_id")
    if batch["status"] in BATCH_ACTIVE_STATES and not has_active_batch_job(project["name"], job_id):
        batch["status"] = "interrupted"
        batch["finished_at"] = batch["finished_at"] or now_iso()
        batch["current_slide_index"] = None
        batch["message"] = "Background generation stopped because the Flask server restarted."
        for slide in slides:
            slide_status = normalize_slide_generation_state(slide.get("generation_status"))
            if slide_status in BATCH_ACTIVE_STATES:
                slide["generation_status"] = "interrupted"
                slide["generation_error"] = slide.get("generation_error") or "Background generation stopped."

    return batch


def load_project(project_name: str) -> dict[str, Any]:
    with PROJECT_IO_LOCK:
        path = project_json_path(project_name)
        if not path.exists():
            raise FileNotFoundError(f"Project '{project_name}' does not exist.")

        project = json.loads(path.read_text(encoding="utf-8"))
        slides = project.get("slides", [])
        normalized_slides = []
        for index, slide in enumerate(slides, start=1):
            normalized_slide = default_slide(index)
            normalized_slide["prompt"] = str(slide.get("prompt", ""))
            normalized_slide["fixed_text_prompt_override"] = slide.get("fixed_text_prompt_override")
            normalized_slide["images"] = [str(image) for image in slide.get("images", [])]
            normalized_slide["generated"] = bool(slide.get("generated", False))
            normalized_slide["filename"] = str(slide.get("filename") or f"s{index}.png")
            normalized_slide["generation_status"] = normalize_slide_generation_state(
                slide.get("generation_status")
            )
            normalized_slide["generation_error"] = slide.get("generation_error")
            normalized_slides.append(normalized_slide)

        project["slides"] = normalized_slides
        project["fixed_images"] = [str(image) for image in project.get("fixed_images", [])]
        project["fixed_text_prompt"] = str(project.get("fixed_text_prompt", ""))
        project["aspect_ratio"] = validate_aspect_ratio(project.get("aspect_ratio", DEFAULT_ASPECT_RATIO))
        project["resolution"] = validate_resolution(project.get("resolution", DEFAULT_RESOLUTION))
        project["batch_generation"] = normalize_batch_generation(project)
        return project


def effective_fixed_text(project: dict[str, Any], slide: dict[str, Any]) -> str:
    override = slide.get("fixed_text_prompt_override")
    if override is not None:
        return str(override)
    return str(project.get("fixed_text_prompt", ""))


def sync_slide_count(project: dict[str, Any], page_count: int) -> None:
    current_slides = project.get("slides", [])
    page_count = coerce_page_count(page_count)

    if len(current_slides) < page_count:
        for index in range(len(current_slides) + 1, page_count + 1):
            current_slides.append(default_slide(index))
    else:
        current_slides = current_slides[:page_count]

    for index, slide in enumerate(current_slides, start=1):
        slide["index"] = index
        slide["filename"] = f"s{index}.png"
        slide.setdefault("prompt", "")
        slide.setdefault("images", [])
        slide.setdefault("generated", False)
        slide.setdefault("fixed_text_prompt_override", None)
        slide["generation_status"] = normalize_slide_generation_state(slide.get("generation_status"))
        slide.setdefault("generation_error", None)

    project["slides"] = current_slides
    batch_generation = normalize_batch_generation(project)
    batch_generation["total_slides"] = len(current_slides)
    if batch_generation["status"] == "idle":
        batch_generation["message"] = ""
    project["batch_generation"] = batch_generation


def slide_asset_names(project: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for slide in project.get("slides", []):
        filename = str(slide.get("filename", "") or "").strip()
        if filename:
            names.add(filename)
        for image in slide.get("images", []):
            image_name = str(image or "").strip()
            if image_name:
                names.add(image_name)
    return names


def remove_obsolete_slide_assets(previous_project: dict[str, Any], updated_project: dict[str, Any]) -> None:
    folder = project_dir(updated_project["name"])
    obsolete_assets = slide_asset_names(previous_project) - slide_asset_names(updated_project)
    for filename in obsolete_assets:
        path = folder / filename
        if path.exists():
            path.unlink()


def apply_file_renames(folder: Path, rename_pairs: list[tuple[str, str]]) -> None:
    staged_renames: list[tuple[str, str]] = []
    moved_sources: set[str] = set()

    for old_name, new_name in rename_pairs:
        if not old_name or old_name == new_name or old_name in moved_sources:
            continue
        source = folder / old_name
        if not source.exists():
            continue

        temp_name = f".rename_{uuid4().hex}_{Path(old_name).name}"
        source.rename(folder / temp_name)
        staged_renames.append((temp_name, new_name))
        moved_sources.add(old_name)

    for temp_name, new_name in staged_renames:
        temp_path = folder / temp_name
        destination = folder / new_name
        if destination.exists():
            destination.unlink()
        temp_path.rename(destination)


def resequence_slide_assets(project: dict[str, Any]) -> None:
    folder = project_dir(project["name"])
    rename_pairs: list[tuple[str, str]] = []

    for index, slide in enumerate(project.get("slides", []), start=1):
        current_filename = str(slide.get("filename", "") or "").strip()
        desired_filename = f"s{index}.png"
        if current_filename and current_filename != desired_filename:
            rename_pairs.append((current_filename, desired_filename))
        slide["filename"] = desired_filename

        renamed_images: list[str] = []
        for image_index, image_name in enumerate(slide.get("images", [])):
            current_image_name = str(image_name or "").strip()
            desired_image_name = f"slide_{index}_img_{image_index}.png"
            if current_image_name and current_image_name != desired_image_name:
                rename_pairs.append((current_image_name, desired_image_name))
            renamed_images.append(desired_image_name)

        slide["images"] = renamed_images
        slide["index"] = index

    apply_file_renames(folder, rename_pairs)

    for slide in project.get("slides", []):
        generated_path = folder / slide["filename"]
        slide["generated"] = bool(slide.get("generated")) and generated_path.exists()


def serialize_project(project: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(project)
    payload["page_count"] = len(payload.get("slides", []))
    payload["batch_generation"] = normalize_batch_generation(payload)
    for slide in payload["slides"]:
        slide["effective_fixed_text"] = effective_fixed_text(project, slide)
        slide["preview_url"] = (
            f"/projects/{payload['name']}/{slide['filename']}?t={payload['updated_at']}"
            if slide.get("generated")
            else None
        )
    payload["fixed_image_urls"] = {
        filename: f"/projects/{payload['name']}/{filename}?t={payload['updated_at']}"
        for filename in payload.get("fixed_images", [])
    }
    folder = project_dir(payload["name"])
    has_assets = project_folder_has_assets(folder)
    is_empty = not has_assets and not project_has_meaningful_content(project)
    payload["is_valid"] = True
    payload["project_error"] = None
    payload["has_assets"] = has_assets
    payload["is_empty"] = is_empty
    payload["delete_requires_confirmation"] = not is_empty
    payload["delete_warning"] = project_delete_warning(has_assets) if not is_empty else ""
    return payload


def project_list_entry_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": payload["name"],
        "updated_at": payload["updated_at"],
        "created_at": payload["created_at"],
        "page_count": payload.get("page_count", len(payload.get("slides", []))),
        "is_valid": payload.get("is_valid", True),
        "project_error": payload.get("project_error"),
        "is_empty": payload.get("is_empty", False),
        "has_assets": payload.get("has_assets", False),
        "delete_requires_confirmation": payload.get("delete_requires_confirmation", True),
        "delete_warning": payload.get("delete_warning", ""),
    }


def inspect_project_record(project_name: str) -> dict[str, Any]:
    name = slugify_project_name(project_name)
    folder = project_dir(name)
    project_file = folder / "project.json"
    if not folder.exists():
        raise FileNotFoundError(f"Project '{name}' does not exist.")

    updated_at = datetime.fromtimestamp(project_file.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat() if project_file.exists() else now_iso()
    has_assets = project_folder_has_assets(folder)

    if not project_file.exists():
        return invalid_project_stub(
            name,
            "project.json is missing.",
            has_assets=has_assets,
            updated_at=updated_at,
        )

    try:
        project = load_project(name)
    except (json.JSONDecodeError, ProjectError, ValueError) as exc:
        return invalid_project_stub(
            name,
            str(exc) or "project.json could not be read.",
            has_assets=has_assets,
            updated_at=updated_at,
        )

    return serialize_project(project)


def delete_project_folder(project_name: str) -> None:
    folder = project_dir(project_name)
    if folder.exists():
        shutil.rmtree(folder)


def ensure_batch_not_running(project_name: str) -> None:
    runtime_job = get_runtime_job(project_name)
    if runtime_job and runtime_job["status"] in BATCH_ACTIVE_STATES:
        raise ProjectError("Background generation is already running for this project.")


def set_slide_batch_state(
    project: dict[str, Any],
    slide_index: int,
    state: str,
    error_message: str | None = None,
) -> None:
    slide = get_slide(project, slide_index)
    slide["generation_status"] = normalize_slide_generation_state(state)
    slide["generation_error"] = error_message


def initialize_batch_generation(project: dict[str, Any], job_id: str) -> None:
    for slide in project.get("slides", []):
        slide["generation_status"] = "queued"
        slide["generation_error"] = None

    project["batch_generation"] = {
        "job_id": job_id,
        "status": "queued",
        "total_slides": len(project.get("slides", [])),
        "processed_slides": 0,
        "completed_slides": 0,
        "failed_slides": 0,
        "current_slide_index": None,
        "started_at": now_iso(),
        "finished_at": None,
        "last_error": None,
        "message": "Queued background generation.",
    }


def persist_batch_generation_update(
    project_name: str,
    updater,
) -> dict[str, Any]:
    project = load_project(project_name)
    updater(project)
    save_project(project)
    return project


def start_background_generation(project_name: str) -> dict[str, Any]:
    project_name = slugify_project_name(project_name)
    ensure_batch_not_running(project_name)
    job_id = uuid4().hex
    project = persist_batch_generation_update(
        project_name,
        lambda loaded_project: initialize_batch_generation(loaded_project, job_id),
    )

    cancel_event = threading.Event()
    runtime_job = {
        "job_id": job_id,
        "project_name": project_name,
        "status": "queued",
        "cancel_event": cancel_event,
        "thread": None,
    }
    worker = threading.Thread(
        target=run_background_generation,
        args=(project_name, job_id),
        daemon=True,
        name=f"carousel-batch-{project_name}",
    )
    runtime_job["thread"] = worker

    with JOB_REGISTRY_LOCK:
        ACTIVE_BATCH_JOBS[project_name] = runtime_job

    worker.start()
    return project


def finalize_batch_generation(project_name: str, final_status: str, message: str, last_error: str | None = None) -> dict[str, Any]:
    def apply_final_state(project: dict[str, Any]) -> None:
        batch = normalize_batch_generation(project)
        batch["status"] = final_status
        batch["finished_at"] = now_iso()
        batch["current_slide_index"] = None
        batch["last_error"] = last_error
        batch["message"] = message
        project["batch_generation"] = batch
        if final_status in {"cancelled", "interrupted"}:
            for slide in project.get("slides", []):
                if normalize_slide_generation_state(slide.get("generation_status")) == "queued":
                    slide["generation_status"] = final_status
                    slide["generation_error"] = slide.get("generation_error") or message

    return persist_batch_generation_update(project_name, apply_final_state)


def run_background_generation(project_name: str, job_id: str) -> None:
    project_name = slugify_project_name(project_name)
    runtime_job = get_runtime_job(project_name)
    if not runtime_job or runtime_job["job_id"] != job_id:
        return

    runtime_job["status"] = "running"
    project = load_project(project_name)
    slide_indexes = [slide["index"] for slide in project.get("slides", [])]
    failures = 0
    last_error: str | None = None

    try:
        for position, slide_index in enumerate(slide_indexes, start=1):
            runtime_job = get_runtime_job(project_name)
            if not runtime_job or runtime_job["job_id"] != job_id:
                return
            cancel_event = runtime_job["cancel_event"]
            if cancel_event.is_set():
                finalize_batch_generation(
                    project_name,
                    "cancelled",
                    "Background generation cancelled.",
                    last_error=last_error,
                )
                return

            def mark_running(loaded_project: dict[str, Any]) -> None:
                batch = normalize_batch_generation(loaded_project)
                batch["status"] = "running"
                batch["current_slide_index"] = slide_index
                batch["message"] = f"Generating slide {slide_index} of {len(slide_indexes)}..."
                loaded_project["batch_generation"] = batch
                set_slide_batch_state(loaded_project, slide_index, "running")

            persist_batch_generation_update(project_name, mark_running)

            try:
                generate_slide_image(project_name, slide_index, include_existing_image=False)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                last_error = str(exc)

                def mark_failed(loaded_project: dict[str, Any]) -> None:
                    batch = normalize_batch_generation(loaded_project)
                    batch["processed_slides"] = position
                    batch["failed_slides"] = failures
                    batch["last_error"] = last_error
                    batch["message"] = f"Slide {slide_index} failed. Continuing..."
                    loaded_project["batch_generation"] = batch
                    set_slide_batch_state(loaded_project, slide_index, "failed", last_error)

                persist_batch_generation_update(project_name, mark_failed)
                continue

            def mark_completed(loaded_project: dict[str, Any]) -> None:
                batch = normalize_batch_generation(loaded_project)
                batch["processed_slides"] = position
                batch["completed_slides"] = batch.get("completed_slides", 0) + 1
                batch["message"] = f"Completed slide {slide_index} of {len(slide_indexes)}."
                loaded_project["batch_generation"] = batch
                set_slide_batch_state(loaded_project, slide_index, "completed")

            persist_batch_generation_update(project_name, mark_completed)

        final_status = "failed" if failures else "completed"
        final_message = (
            "Background generation completed with errors."
            if failures
            else "All slides generated in the background."
        )
        finalize_batch_generation(project_name, final_status, final_message, last_error=last_error)
    finally:
        with JOB_REGISTRY_LOCK:
            ACTIVE_BATCH_JOBS.pop(project_name, None)


def get_slide(project: dict[str, Any], slide_index: int) -> dict[str, Any]:
    if slide_index < 1 or slide_index > len(project["slides"]):
        raise ProjectError(f"Slide index {slide_index} is out of range.")
    return project["slides"][slide_index - 1]


def get_previous_generated_slide(project: dict[str, Any], slide: dict[str, Any]) -> dict[str, Any] | None:
    previous_index = int(slide["index"]) - 1
    if previous_index < 1:
        return None

    previous_slide = get_slide(project, previous_index)
    previous_filename = str(previous_slide.get("filename", "") or "").strip()
    if not previous_slide.get("generated") or not previous_filename:
        return None

    previous_path = project_dir(project["name"]) / previous_filename
    if not previous_path.exists():
        return None

    return previous_slide


def next_available_asset_name(
    project: dict[str, Any],
    prefix: str,
) -> str:
    used_names = set(project.get("fixed_images", []))
    for slide in project.get("slides", []):
        used_names.update(slide.get("images", []))

    index = 0
    while True:
        candidate = f"{prefix}{index}.png"
        if candidate not in used_names:
            return candidate
        index += 1


def load_image_from_bytes(image_bytes: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        return image
    except UnidentifiedImageError as exc:
        raise ProjectError("Uploaded data is not a valid image.") from exc


def image_bytes_from_request() -> bytes:
    if "image" in request.files:
        uploaded_file = request.files["image"]
        image_bytes = uploaded_file.read()
        if not image_bytes:
            raise ProjectError("Uploaded image file is empty.")
        return image_bytes

    payload = request.get_json(silent=True) or {}
    image_base64 = payload.get("image_base64")
    if not image_base64:
        raise ProjectError("No image was provided.")

    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]

    try:
        return base64.b64decode(image_base64)
    except ValueError as exc:
        raise ProjectError("Invalid base64 image payload.") from exc


def save_uploaded_asset(project: dict[str, Any], target: str, slide_index: int | None = None) -> dict[str, Any]:
    image = load_image_from_bytes(image_bytes_from_request())
    if target == "fixed":
        filename = next_available_asset_name(project, "fixed_img_")
    elif target == "slide":
        if slide_index is None:
            raise ProjectError("slide_index is required for slide uploads.")
        get_slide(project, slide_index)
        filename = next_available_asset_name(project, f"slide_{slide_index}_img_")
    else:
        raise ProjectError("Target must be either 'fixed' or 'slide'.")

    output_path = project_dir(project["name"]) / filename
    image.save(output_path, format="PNG")

    if target == "fixed":
        project["fixed_images"].append(filename)
    else:
        slide = get_slide(project, slide_index or 1)
        slide["images"].append(filename)

    save_project(project)
    return {"filename": filename, "url": f"/projects/{project['name']}/{filename}?t={project['updated_at']}"}


def delete_asset(project: dict[str, Any], filename: str) -> None:
    removed = False
    if filename in project.get("fixed_images", []):
        project["fixed_images"] = [item for item in project["fixed_images"] if item != filename]
        removed = True

    for slide in project.get("slides", []):
        if filename in slide.get("images", []):
            slide["images"] = [item for item in slide["images"] if item != filename]
            removed = True

    if not removed:
        raise ProjectError("Asset was not found in this project.")

    asset_path = project_dir(project["name"]) / filename
    if asset_path.exists():
        asset_path.unlink()

    save_project(project)


def open_project_image(project_name: str, filename: str) -> Image.Image:
    image_path = project_dir(project_name) / filename
    if not image_path.exists():
        raise FileNotFoundError(f"Image '{filename}' was not found.")
    with Image.open(image_path) as image:
        image.load()
        copied = image.copy()
    return copied


def build_gemini_prompt(
    project: dict[str, Any],
    slide: dict[str, Any],
    previous_slide_attached: bool = False,
) -> str:
    prompt = str(slide.get("prompt", "")).strip()
    if not prompt:
        raise ProjectError(f"Slide {slide['index']} is missing a prompt.")

    fixed_text = effective_fixed_text(project, slide).strip()
    prompt_parts = [prompt]
    if fixed_text:
        prompt_parts.append(fixed_text)
    if previous_slide_attached:
        prompt_parts.append(
            "Attached previous slide for reference, use it to maintain image consistency"
        )
    return "\n\n".join(prompt_parts)


def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ProjectError("GEMINI_API_KEY is missing. Add it to your .env file.")

    try:
        from google import genai
    except ImportError as exc:
        raise ProjectError("google-genai is not installed. Run pip install -r requirements.txt.") from exc

    return genai.Client(api_key=api_key)


def build_generate_config(project: dict[str, Any]):
    try:
        from google.genai import types
    except ImportError as exc:
        raise ProjectError("google-genai is not installed. Run pip install -r requirements.txt.") from exc

    return types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=project["aspect_ratio"],
            image_size=project["resolution"],
        ),
    )


def gemini_contents_for_slide(
    project: dict[str, Any],
    slide: dict[str, Any],
    include_existing_image: bool,
) -> list[Any]:
    contents: list[Any] = []

    if include_existing_image:
        generated_filename = slide["filename"]
        generated_path = project_dir(project["name"]) / generated_filename
        if not generated_path.exists():
            raise ProjectError(f"Slide {slide['index']} has no generated image to update yet.")
        contents.append(open_project_image(project["name"], generated_filename))

    previous_slide = get_previous_generated_slide(project, slide)
    if previous_slide is not None:
        contents.append(open_project_image(project["name"], previous_slide["filename"]))

    for filename in project.get("fixed_images", []):
        contents.append(open_project_image(project["name"], filename))

    for filename in slide.get("images", []):
        contents.append(open_project_image(project["name"], filename))

    contents.append(
        build_gemini_prompt(
            project,
            slide,
            previous_slide_attached=previous_slide is not None,
        )
    )
    return contents


def extract_generated_image(response: Any) -> Image.Image:
    candidate_parts = []
    if hasattr(response, "parts") and response.parts:
        candidate_parts = response.parts
    elif hasattr(response, "candidates") and response.candidates:
        for candidate in response.candidates:
            content = getattr(candidate, "content", None)
            if content and getattr(content, "parts", None):
                candidate_parts.extend(content.parts)

    for part in candidate_parts:
        if getattr(part, "inline_data", None) is not None:
            return part.as_image()

    raise ProjectError("Gemini did not return an image in the response.")


def generate_slide_image(project_name: str, slide_index: int, include_existing_image: bool) -> dict[str, Any]:
    project = load_project(project_name)
    slide = get_slide(project, slide_index)
    client = get_gemini_client()

    response = client.models.generate_content(
        model="gemini-3-pro-image-preview",
        contents=gemini_contents_for_slide(project, slide, include_existing_image=include_existing_image),
        config=build_generate_config(project),
    )

    image = extract_generated_image(response)
    output_path = project_dir(project_name) / slide["filename"]
    image.save(output_path)
    slide["generated"] = True
    slide["generation_status"] = "completed"
    slide["generation_error"] = None
    save_project(project)

    refreshed_project = load_project(project_name)
    refreshed_slide = get_slide(refreshed_project, slide_index)
    return {
        "project": serialize_project(refreshed_project),
        "slide": {
            **refreshed_slide,
            "effective_fixed_text": effective_fixed_text(refreshed_project, refreshed_slide),
            "preview_url": f"/projects/{project_name}/{refreshed_slide['filename']}?t={refreshed_project['updated_at']}",
        },
    }


def apply_project_update(project: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if "aspect_ratio" in payload:
        project["aspect_ratio"] = validate_aspect_ratio(payload["aspect_ratio"])
    if "resolution" in payload:
        project["resolution"] = validate_resolution(payload["resolution"])
    if "fixed_text_prompt" in payload:
        project["fixed_text_prompt"] = str(payload["fixed_text_prompt"] or "")

    page_count = payload.get("page_count", payload.get("pages"))
    if page_count is not None:
        sync_slide_count(project, page_count)

    if "slides" in payload:
        incoming_slides = payload["slides"]
        if not isinstance(incoming_slides, list):
            raise ProjectError("slides must be an array.")

        sync_slide_count(project, len(incoming_slides))
        for index, incoming_slide in enumerate(incoming_slides, start=1):
            slide = get_slide(project, index)
            slide["prompt"] = str(incoming_slide.get("prompt", ""))
            slide["fixed_text_prompt_override"] = incoming_slide.get("fixed_text_prompt_override")
            slide["images"] = [str(image) for image in incoming_slide.get("images", [])]
            slide["generated"] = bool(incoming_slide.get("generated", slide.get("generated", False)))
            slide["filename"] = str(incoming_slide.get("filename") or f"s{index}.png")

    return project


def insert_slide(project: dict[str, Any], insert_before_index: int) -> dict[str, Any]:
    slides = project.get("slides", [])
    max_insert_index = len(slides) + 1
    if insert_before_index < 1 or insert_before_index > max_insert_index:
        raise ProjectError(
            f"Slide index {insert_before_index} is out of range for insertion."
        )

    slides.insert(insert_before_index - 1, default_slide(insert_before_index))
    project["slides"] = slides
    return project


@app.errorhandler(ProjectError)
def handle_project_error(error: ProjectError):
    return jsonify({"error": str(error)}), 400


@app.errorhandler(FileNotFoundError)
def handle_file_not_found(error: FileNotFoundError):
    return jsonify({"error": str(error)}), 404


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/projects")
def list_projects():
    projects = []
    for candidate in PROJECTS_DIR.iterdir():
        if not candidate.is_dir():
            continue
        try:
            project_payload = inspect_project_record(candidate.name)
            projects.append(project_list_entry_from_payload(project_payload))
        except Exception:
            continue

    projects.sort(key=lambda item: item["updated_at"], reverse=True)
    return jsonify({"projects": projects})


@app.post("/api/projects")
def create_project():
    payload = request.get_json(silent=True) or {}
    raw_name = payload.get("name")
    if not raw_name:
        raise ProjectError("Project name is required.")

    project = create_project_payload(
        name=str(raw_name),
        page_count=payload.get("page_count", payload.get("pages", DEFAULT_PAGE_COUNT)),
        aspect_ratio=payload.get("aspect_ratio", DEFAULT_ASPECT_RATIO),
        resolution=payload.get("resolution", DEFAULT_RESOLUTION),
        fixed_text_prompt=payload.get("fixed_text_prompt", ""),
    )

    folder = project_dir(project["name"])
    if folder.exists():
        raise ProjectError(f"Project '{project['name']}' already exists.")

    save_project(project)
    return jsonify({"project": serialize_project(project)}), 201


@app.get("/api/projects/<project_name>")
def get_project(project_name: str):
    project = inspect_project_record(project_name)
    return jsonify({"project": project})


@app.put("/api/projects/<project_name>")
def update_project(project_name: str):
    ensure_batch_not_running(project_name)
    payload = request.get_json(silent=True) or {}
    project = load_project(project_name)
    previous_project = deepcopy(project)
    apply_project_update(project, payload)
    remove_obsolete_slide_assets(previous_project, project)
    resequence_slide_assets(project)
    save_project(project)
    refreshed = load_project(project_name)
    return jsonify({"project": serialize_project(refreshed)})


@app.post("/api/projects/<project_name>/slides/insert")
def insert_project_slide(project_name: str):
    ensure_batch_not_running(project_name)
    payload = request.get_json(silent=True) or {}
    insert_before_index = payload.get("insert_before_index")
    if insert_before_index is None:
        raise ProjectError("insert_before_index is required.")

    project = load_project(project_name)
    insert_slide(project, int(insert_before_index))
    resequence_slide_assets(project)
    save_project(project)
    refreshed = load_project(project_name)
    return jsonify({"project": serialize_project(refreshed)})


@app.post("/api/projects/<project_name>/generate/<int:slide_index>")
def generate_slide(project_name: str, slide_index: int):
    ensure_batch_not_running(project_name)
    return jsonify(generate_slide_image(project_name, slide_index, include_existing_image=False))


@app.post("/api/projects/<project_name>/update/<int:slide_index>")
def update_slide(project_name: str, slide_index: int):
    ensure_batch_not_running(project_name)
    return jsonify(generate_slide_image(project_name, slide_index, include_existing_image=True))


@app.post("/api/projects/<project_name>/generate-all")
def generate_all(project_name: str):
    project = start_background_generation(project_name)
    return jsonify(
        {
            "project": serialize_project(project),
            "accepted": True,
            "message": "Background generation started.",
        }
    ), 202


@app.get("/api/projects/<project_name>/generation-status")
def generation_status(project_name: str):
    project = load_project(project_name)
    runtime_job = get_runtime_job(project_name)
    return jsonify(
        {
            "project": serialize_project(project),
            "active": bool(runtime_job and runtime_job["status"] in BATCH_ACTIVE_STATES),
        }
    )


@app.delete("/api/projects/<project_name>")
def delete_project(project_name: str):
    payload = request.get_json(silent=True) or {}
    confirm_delete = bool(payload.get("confirm"))
    project_payload = inspect_project_record(project_name)

    if project_payload.get("is_valid"):
        ensure_batch_not_running(project_name)

    if project_payload["delete_requires_confirmation"] and not confirm_delete:
        return (
            jsonify(
                {
                    "error": project_payload["delete_warning"],
                    "requires_confirmation": True,
                    "project": project_payload,
                }
            ),
            409,
        )

    delete_project_folder(project_name)
    return jsonify({"deleted": True, "project_name": project_payload["name"]})


@app.post("/api/projects/<project_name>/generate-all/cancel")
def cancel_generate_all(project_name: str):
    runtime_job = get_runtime_job(project_name)
    if not runtime_job or runtime_job["status"] not in BATCH_ACTIVE_STATES:
        raise ProjectError("No background generation is currently running.")

    runtime_job["status"] = "cancelling"
    runtime_job["cancel_event"].set()

    def mark_cancelling(project: dict[str, Any]) -> None:
        batch = normalize_batch_generation(project)
        batch["status"] = "cancelling"
        batch["message"] = "Stopping after the current slide finishes..."
        project["batch_generation"] = batch

    project = persist_batch_generation_update(project_name, mark_cancelling)
    return jsonify(
        {
            "project": serialize_project(project),
            "accepted": True,
            "message": "Background generation will stop after the current slide.",
        }
    )


@app.post("/api/projects/<project_name>/upload-image")
def upload_image(project_name: str):
    ensure_batch_not_running(project_name)
    project = load_project(project_name)
    form_payload = request.form.to_dict()
    json_payload = request.get_json(silent=True) or {}
    target = form_payload.get("target") or json_payload.get("target")
    if not target:
        raise ProjectError("Upload target is required.")

    slide_index_value = form_payload.get("slide_index") or json_payload.get("slide_index")
    slide_index = int(slide_index_value) if slide_index_value is not None else None
    upload_result = save_uploaded_asset(project, target=target, slide_index=slide_index)
    refreshed = load_project(project_name)
    return jsonify({"project": serialize_project(refreshed), "upload": upload_result}), 201


@app.delete("/api/projects/<project_name>/delete-image/<path:filename>")
def delete_image(project_name: str, filename: str):
    if "/" in filename or filename.startswith("."):
        abort(400)

    ensure_batch_not_running(project_name)
    project = load_project(project_name)
    delete_asset(project, filename)
    refreshed = load_project(project_name)
    return jsonify({"project": serialize_project(refreshed)})


@app.get("/projects/<project_name>/<path:filename>")
def serve_project_file(project_name: str, filename: str):
    if filename.startswith("."):
        abort(404)

    folder = project_dir(project_name)
    target = (folder / filename).resolve()
    if target.parent != folder.resolve():
        abort(404)
    if not target.exists():
        abort(404)
    return send_from_directory(folder, filename)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
