# Carousel Generator — Spec

## Purpose
Generate and refine image carousels fast using Gemini's native image generation (Nano Banana Pro: `gemini-3-pro-image-preview`).

## Architecture
- **Backend**: Python + Flask (single file `server.py`)
- **Frontend**: Single `index.html` served by Flask (vanilla HTML/CSS/JS)
- **Storage**: Local filesystem — one folder per project under `./projects/`
- **Config**: API key stored in `.env`

No database. Project state saved as `project.json` inside each project folder.

---

## Data Model

### Project (`projects/<project-name>/project.json`)
```json
{
  "name": "my-carousel",
  "created_at": "2026-04-05T10:00:00",
  "updated_at": "2026-04-05T10:30:00",
  "aspect_ratio": "16:9",
  "resolution": "2K",
  "fixed_text_prompt": "Always include @aglio_app watermark in bottom-right. Use consistent teal brand colors and clean modern style.",
  "fixed_images": ["fixed_img_0.png", "fixed_img_1.png"],
  "slides": [
    {
      "index": 1,
      "prompt": "A bold title card saying 'iPhone Storage Full?' with dramatic red gradient background",
      "fixed_text_prompt_override": null,
      "images": ["slide_1_img_0.png", "slide_1_img_1.png"],
      "generated": true,
      "filename": "s1.png"
    },
    {
      "index": 2,
      "prompt": "Before/after comparison of photo library cleanup",
      "fixed_text_prompt_override": "Include @aglio_app watermark. Use split-screen layout with green checkmark on the 'after' side.",
      "images": [],
      "generated": false,
      "filename": "s2.png"
    }
  ]
}
```

### Key fields explained

- **`fixed_text_prompt`** (project-level) — static text block auto-appended to every slide's prompt. Set once in settings, copied to each slide's UI on creation.
- **`fixed_text_prompt_override`** (per-slide) — if not `null`, this replaces the project-level `fixed_text_prompt` for that slide. The UI pre-populates each slide's fixed text field with the project default, but any edit creates an override.
- **`fixed_images`** — image files auto-attached to every slide's Gemini request.
- **`slides[].images`** — per-slide image assets, ordered. Images pasted into the prompt textarea land here too (at the end, or at cursor position — see Paste Behavior).

### Override Logic
```
effective_fixed_text = slide.fixed_text_prompt_override
                       if slide.fixed_text_prompt_override is not None
                       else project.fixed_text_prompt
```

If a slide's fixed text field is edited at all (even to match the project default), it becomes an override. A "Reset to default" button clears the override back to `null`.

### Folder Structure
```
carousel-gen/
├── server.py
├── .env                  # GEMINI_API_KEY=xxx
├── templates/
│   └── index.html
└── projects/
    └── my-carousel/
        ├── project.json
        ├── fixed_img_0.png
        ├── fixed_img_1.png
        ├── slide_1_img_0.png
        ├── slide_1_img_1.png
        ├── s1.png                # Generated slide 1
        ├── s2.png
        └── s3.png
```

---

## UI Layout (Single Page, Two-Column per Slide)

```
┌──────────────────────────────────────────────────────────────────┐
│  CAROUSEL GEN                                  [New Project ▼]  │
│  Project: my-carousel                                           │
├──────────┬───────────────────────────────────────────────────────┤
│ Projects │  Settings Bar                                        │
│          │  Aspect: [16:9 ▼]  Res: [2K ▼]  Pages: [5]          │
│ • proj-1 │                                                      │
│ • proj-2 │  Fixed Text Prompt (default for all slides):         │
│ • proj-3 │  [__________________________________________________]│
│          │  [__________________________________________________]│
│          │  (auto-expands to show full content)                  │
│          │                                                      │
│          │  Fixed Images: [img1 ✕] [img2 ✕] [+ / paste]        │
│          │                                                      │
│          │  [Generate All]  [Save]                               │
│          ├───────────────────────────────────────────────────────┤
│          │                                                      │
│          │  ┌─ Slide 1 ────────────────────────────────────────┐│
│          │  │  LEFT (50%)            │  RIGHT (50%)            ││
│          │  │                        │                         ││
│          │  │  Slide Prompt:         │  ┌───────────────────┐  ││
│          │  │  [________________]    │  │                   │  ││
│          │  │  [________________]    │  │   Generated       │  ││
│          │  │  (auto-expands)        │  │   Image           │  ││
│          │  │                        │  │   Preview         │  ││
│          │  │  Fixed Text (this      │  │                   │  ││
│          │  │  slide): [Reset ↺]     │  └───────────────────┘  ││
│          │  │  [________________]    │                         ││
│          │  │  [________________]    │                         ││
│          │  │  (auto-expands)        │                         ││
│          │  │                        │                         ││
│          │  │  Slide Images:         │                         ││
│          │  │  [img ✕] [img ✕]       │                         ││
│          │  │  [+ / paste]           │                         ││
│          │  │                        │                         ││
│          │  │  [Generate] [Update]   │                         ││
│          │  └────────────────────────┴─────────────────────────┘│
│          │                                                      │
│          │  ┌─ Slide 2 ────────────────────────────────────────┐│
│          │  │  ...                   │  ...                    ││
│          │  └────────────────────────┴─────────────────────────┘│
│          │                                                      │
└──────────┴───────────────────────────────────────────────────────┘
```

### Key Layout Rules
- Each slide is a horizontal card split 50/50: controls left, generated preview right.
- **All textareas auto-expand** to show their full content (no scrolling inside textarea). Use CSS `field-sizing: content` or JS auto-resize on input.
- Fixed text prompt in settings = project default. Each slide gets its own copy (editable). Editing creates an override. [Reset ↺] button clears override back to project default.
- Fixed images area and slide image areas all support paste, drag-drop, and file picker.

---

## API Endpoints

| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/` | Serve the HTML page |
| `GET` | `/api/projects` | List all projects |
| `POST` | `/api/projects` | Create new project |
| `GET` | `/api/projects/<n>` | Get project config |
| `PUT` | `/api/projects/<n>` | Update project config (settings, prompts, overrides) |
| `POST` | `/api/projects/<n>/generate/<slide_index>` | Generate single slide (fresh) |
| `POST` | `/api/projects/<n>/update/<slide_index>` | Update existing slide (send generated image + prompt) |
| `POST` | `/api/projects/<n>/generate-all` | Generate all slides sequentially |
| `GET` | `/projects/<n>/<filename>` | Serve generated/asset image |
| `POST` | `/api/projects/<n>/upload-image` | Upload image asset (file or pasted base64) |
| `DELETE` | `/api/projects/<n>/delete-image/<filename>` | Remove an asset image |

---

## Gemini Integration

### Model
`gemini-3-pro-image-preview` (Nano Banana Pro)

### Generate (fresh — no prior image)

```python
from google import genai
from google.genai import types
from PIL import Image

client = genai.Client(api_key=API_KEY)

# Build multimodal contents list
contents = []

# 1. Add fixed images (project-level, attached to every slide)
for img_path in project["fixed_images"]:
    contents.append(Image.open(f"projects/{name}/{img_path}"))

# 2. Add slide-specific images (in order)
for img_path in slide["images"]:
    contents.append(Image.open(f"projects/{name}/{img_path}"))

# 3. Build text prompt: slide prompt + effective fixed text
effective_fixed = slide.get("fixed_text_prompt_override") or project.get("fixed_text_prompt", "")
full_prompt = slide["prompt"]
if effective_fixed:
    full_prompt += "\n\n" + effective_fixed
contents.append(full_prompt)

# 4. Call Gemini with image_config for aspect ratio and resolution
response = client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents=contents,
    config=types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=project["aspect_ratio"],   # e.g. "16:9"
            image_size=project["resolution"],        # e.g. "2K"
        ),
    ),
)

# 5. Extract and save generated image
for part in response.parts:
    if part.inline_data is not None:
        image = part.as_image()
        image.save(f"projects/{name}/s{index}.png")
```

### Update (edit existing generated image)
Send the previously generated image back to Gemini with the current prompt. Single-turn only — no conversation history.

```python
# Load the existing generated image
existing_img = Image.open(f"projects/{name}/s{index}.png")

contents = []

# 1. The existing generated image (first, so Gemini knows what to edit)
contents.append(existing_img)

# 2. Fixed images (project-level)
for img_path in project["fixed_images"]:
    contents.append(Image.open(f"projects/{name}/{img_path}"))

# 3. Slide-specific images
for img_path in slide["images"]:
    contents.append(Image.open(f"projects/{name}/{img_path}"))

# 4. Edit prompt (slide prompt + effective fixed text)
effective_fixed = slide.get("fixed_text_prompt_override") or project.get("fixed_text_prompt", "")
edit_prompt = slide["prompt"]
if effective_fixed:
    edit_prompt += "\n\n" + effective_fixed
contents.append(edit_prompt)

# 5. Call Gemini
response = client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents=contents,
    config=types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=project["aspect_ratio"],
            image_size=project["resolution"],
        ),
    ),
)

# 6. Overwrite existing
for part in response.parts:
    if part.inline_data is not None:
        image = part.as_image()
        image.save(f"projects/{name}/s{index}.png")
```

### Prompt Assembly (what Gemini actually receives)

**Multimodal contents list (in order):**
1. Fixed images (project-level) — as PIL Image objects
2. Slide images (per-slide) — as PIL Image objects, in the order they appear in the slide's image list
3. Text prompt:
```
{slide.prompt}

{effective_fixed_text_prompt}
```

**Config:**
- `response_modalities`: `["TEXT", "IMAGE"]`
- `image_config.aspect_ratio`: from project settings (e.g. `"16:9"`)
- `image_config.image_size`: from project settings (e.g. `"2K"`)

No slide numbering or other metadata injected into the prompt.

---

## Image Paste Behavior

All image input zones support three methods:
1. **File picker** — click [+] button
2. **Paste** — Ctrl+V / Cmd+V
3. **Drag and drop**

### Where paste is intercepted

| Paste location | What happens |
|---------------|-------------|
| Fixed images area (settings) | Uploaded as fixed image, added to `project.fixed_images` |
| Slide image area | Uploaded as slide image, added to `slide.images` at end |
| **Slide prompt textarea** | Image is **not** inserted as text. Instead, uploaded as slide image asset and appended to `slide.images`. Thumbnail appears in the slide's image list below the prompt. |
| Fixed text prompt textarea | No image handling (text only) |

### Frontend paste handler (prompt textarea)
```javascript
promptTextarea.addEventListener('paste', (e) => {
    const items = e.clipboardData.items;
    for (const item of items) {
        if (item.type.startsWith('image/')) {
            e.preventDefault(); // Don't paste as text
            const blob = item.getAsFile();
            const formData = new FormData();
            formData.append('image', blob);
            formData.append('target', 'slide');
            formData.append('slide_index', slideIndex);
            fetch(`/api/projects/${projectName}/upload-image`, {
                method: 'POST',
                body: formData
            }).then(() => refreshSlideImages(slideIndex));
            return;
        }
    }
    // If no image found, let normal text paste proceed
});
```

This means: **Ctrl+V on the prompt field with an image on clipboard → image becomes a slide asset, shown as a thumbnail below the prompt, and sent to Gemini as a multimodal input alongside the text prompt.**

---

## Auto-Expanding Textareas

All textareas (slide prompt, fixed text prompt in settings, per-slide fixed text override) auto-expand to show their full content. No internal scrollbar.

```css
textarea {
    field-sizing: content;  /* Modern browsers */
    min-height: 60px;
    max-height: none;
    resize: vertical;       /* Allow manual resize too */
}
```

Fallback for older browsers:
```javascript
textarea.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = this.scrollHeight + 'px';
});
```

---

## Workflow

### New Project
1. Click "New Project" → enter name
2. Set aspect ratio, resolution, number of slides
3. Optionally set fixed text prompt and fixed images (project defaults)
4. Slide cards appear. Each slide's fixed text field is pre-populated with the project default.

### Author Slides
1. Write prompt per slide (left column, auto-expanding textarea)
2. Optionally edit the fixed text for specific slides (creates override)
3. Paste/upload per-slide images or paste images directly into prompt field
4. Fixed images from settings auto-apply to all slides

### Generate
- **Per slide**: Click [Generate] → fresh generation → image appears in right column
- **All slides**: Click [Generate All] → generates sequentially, previews fill in

### Update (Edit Existing)
- After a slide is generated, [Update] button appears
- Click [Update] → sends existing generated image + current prompt to Gemini
- Single-turn edit (no multi-turn history)
- Overwrites the generated image
- [Generate] still available to regenerate from scratch

### Reset Fixed Text
- Click [Reset ↺] next to a slide's fixed text field
- Clears `fixed_text_prompt_override` to `null`
- Field repopulates with project default

### Reopen
- On page load, sidebar lists all projects
- Click any project to load config and previews

---

## Buttons per Slide

| State | Buttons Available |
|-------|-------------------|
| Not generated | [Generate] |
| Generated | [Generate] (from scratch) + [Update] (edit existing) |

- **Generate** = fresh generation, no prior image sent
- **Update** = sends existing generated image + prompt as single-turn edit

---

## Tech Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Model | `gemini-3-pro-image-preview` | Nano Banana Pro — best quality, text rendering |
| Image config | `aspect_ratio` + `image_size` via `ImageConfig` | Per Gemini API docs |
| Framework | Flask | One file, no build step |
| Frontend | Vanilla HTML/JS | No bundler needed |
| Storage | Filesystem + JSON | No DB setup |
| Image format | PNG | Gemini returns PNG via `part.as_image()` |
| PIL usage | `Image.open()` passed directly to SDK | SDK accepts PIL Image objects |
| Versioning | None (overwrite) | Simplicity |
| Multi-turn | No | Single-turn only for now |
| Auth | None (local tool) | Runs on localhost |

---

## Out of Scope (for now)
- Multi-turn conversation history with Gemini
- User auth
- Image versioning / history
- Drag-and-drop slide reordering
- Export to PDF / video
- Hosted deployment
- Batch prompt templates
- Google Search grounding (can add later)
