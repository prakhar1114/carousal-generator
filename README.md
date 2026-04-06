# Carousel Gen

Local Flask app for generating and refining image carousels with Gemini image generation.

It follows the product spec in `spec.md`:

- Python backend in `server.py`
- Single-page vanilla UI in `templates/index.html`
- Filesystem-backed projects in `projects/`
- Gemini API key loaded from `.env`

## Features

- Create and reopen local carousel projects
- Set project-wide aspect ratio, resolution, fixed text prompt, and fixed reference images
- Update settings explicitly with the `Update Settings` button
- Edit per-slide prompts and per-slide fixed text overrides
- Add a slide above any existing slide
- Delete a slide
- Append a new empty slide at the bottom
- Upload images by file picker, paste, or drag and drop
- Generate a slide from scratch
- Update an existing generated slide by sending the current output back to Gemini
- Generate all slides sequentially

## Requirements

- Python 3.10+
- A Gemini API key with access to `gemini-3-pro-image-preview`

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file in the project root:

```bash
cp .env.example .env
```

4. Add your Gemini API key:

```env
GEMINI_API_KEY=your_api_key_here
```

## Run

Start the local development server:

```bash
source .venv/bin/activate
python server.py
```

The app runs at:

[`http://127.0.0.1:5000`](http://127.0.0.1:5000)

## How To Use

1. Open the app in your browser.
2. Click `New` and create a project.
3. Set:
   - aspect ratio
   - resolution
   - page count
   - fixed text prompt
   - fixed images
4. Click `Update Settings` to post those setting changes.
5. Fill in each slide prompt.
6. Optionally override fixed text on individual slides.
7. Use `Add Slide Above`, `Delete Slide`, or the bottom `Add Slide` button to manage slide order.
8. Paste or upload slide-specific images.
9. Click `Generate` on a slide, or `Generate All` for the whole project.
10. After a slide is generated, click `Update` to refine it using the current generated image plus the current prompt.

## Project Data

Every project is saved under `projects/<project-name>/`.

Each project folder contains:

- `project.json` for saved state
- `fixed_img_*.png` for project-wide reference images
- `slide_<n>_img_*.png` for slide-specific reference images
- `s<n>.png` for generated slide outputs

## Main Files

- `server.py`: Flask app, API routes, Gemini integration, filesystem persistence
- `templates/index.html`: single-page UI, styling, and client-side logic
- `requirements.txt`: Python dependencies
- `.env.example`: sample environment file
- `spec.md`: original product specification

## Notes

- This is a local tool. There is no authentication and no database.
- New projects default to aspect ratio `1:1` and resolution `1K`.
- Generated images are overwritten in place when you use `Update`.
- `Generate All` runs sequentially and will make one Gemini request per slide.
- If Gemini returns an error, the API response will surface it in the UI status area.

## API Summary

- `GET /` - serve the app
- `GET /api/projects` - list projects
- `POST /api/projects` - create project
- `GET /api/projects/<name>` - fetch project
- `PUT /api/projects/<name>` - save project settings and slides
- `POST /api/projects/<name>/generate/<slide_index>` - generate one slide from scratch
- `POST /api/projects/<name>/update/<slide_index>` - update an existing generated slide
- `POST /api/projects/<name>/generate-all` - generate all slides sequentially
- `POST /api/projects/<name>/upload-image` - upload a fixed or slide image
- `DELETE /api/projects/<name>/delete-image/<filename>` - remove an uploaded asset
- `GET /projects/<name>/<filename>` - serve stored images
