# Random Image Picker

Selects a random image from a public webpage without hardcoding rules for a
specific site. It renders pages in headless Chromium so JavaScript-heavy sites
can populate thumbnails and feed images before extraction. It samples links
from the same website and filters out common UI images such as logos, icons,
badges, sprites, and tiny placeholders.

## Setup

Python 3.10+ is recommended. Install Python dependencies and the Playwright
Chromium browser:

```bash
pip install -r requirements.txt
playwright install chromium
```

On Linux or WSL, Chromium may also need native system packages:

```bash
playwright install-deps chromium
```

## Usage

Run the full-screen terminal interface:

```bash
python random_image.py
```

You can also pass the page URL up front. The interface will still ask how many
images to get and will show overall progress, per-image sub-progress, current
image time, and average time while it works:

```bash
python random_image.py https://example.com
```

Print one random image URL:

```bash
python random_image.py https://example.com --no-ui
```

Print several random image URLs:

```bash
python random_image.py https://example.com --count 5 --no-ui
```

In `--no-ui` mode, image URLs are printed to stdout and per-image timing is
printed to stderr so URL output stays easy to redirect.

Download the selected image:

```bash
python random_image.py https://example.com --download images --no-ui
```

Sample more same-site pages before picking an image:

```bash
python random_image.py https://en.wikipedia.org/wiki/Python_(programming_language) --pages 15
```

Use `--no-validate` if a site blocks image validation requests. It is faster,
but it may return a URL that is not actually an image.

## Notes

This works for normal public HTML pages and many JavaScript-rendered pages.
Sites that require authentication, block automated browsers, or hide media
behind private APIs may still return few or no usable images.

## Use this commands to start the fun

source .venv/bin/activate
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
export LD_LIBRARY_PATH="$PWD/.playwright-libs/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"