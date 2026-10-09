# 鹈鹕骑自行车 SVG 动画 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a self-contained HTML page with an accessible, responsive SVG animation of a pelican riding a bicycle.

**Architecture:** Keep the experience in one standalone document at `site/pelican-bike.html`. Inline SVG defines the visual layers; CSS handles responsive composition and reduced-motion styling; a small JavaScript controller updates transform attributes from a single animation clock and exposes pause/resume/reset controls.

**Tech Stack:** Native HTML, CSS, SVG, JavaScript, pytest for structural smoke tests, Playwright for browser verification.

**Spec:** `docs/superpowers/specs/2026-10-03-pelican-bicycle-animation-design.md`

## Global Constraints

- Use only native HTML, CSS, SVG, and JavaScript; add no dependencies.
- Keep visual assets inline and offline-capable.
- Respect `prefers-reduced-motion` by showing the static scene without a running animation loop.
- Preserve responsive `viewBox` composition with no horizontal overflow.

---

### Task 1: Add structural smoke test

**Files:**
- Create: `tests/test_pelican_bicycle_page.py`

**Interfaces:**
- Consumes: `site/pelican-bike.html`.
- Produces: deterministic checks for the required SVG layers, controls, and animation hooks.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path


def test_pelican_bicycle_page_contains_animation_contract():
    page = Path("site/pelican-bike.html")
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    for marker in ("<svg", "pelican", "bicycle", "requestAnimationFrame", "pauseButton", "resetButton"):
        assert marker in html
    assert "prefers-reduced-motion" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pelican_bicycle_page.py -q`
Expected: FAIL because `site/pelican-bike.html` does not yet exist.

- [ ] **Step 3: Commit**

```bash
git add tests/test_pelican_bicycle_page.py
git commit -m "test: define pelican animation page contract"
```

### Task 2: Build the standalone animated SVG page

**Files:**
- Create: `site/pelican-bike.html`

**Interfaces:**
- Consumes: the structural contract from Task 1.
- Produces: accessible page controls with IDs `pauseButton` and `resetButton`, SVG groups `pelican`, `bicycle`, and `road`, and a `requestAnimationFrame` loop.

- [ ] **Step 1: Implement page structure and visual layers**

Add semantic heading and control buttons, then draw the scene in an inline SVG with a stable `viewBox="0 0 1200 760"`. Keep the bird and bicycle inside groups named `pelican` and `bicycle`, and create dedicated road/background groups for moving lines.

- [ ] **Step 2: Add CSS composition and responsive behavior**

Use a warm, high-contrast palette, CSS variables, `aspect-ratio`, and `max-width` so the artwork scales down without clipping. Add `@media (prefers-reduced-motion: reduce)` to disable transitions.

- [ ] **Step 3: Add the animation controller**

Use one elapsed-time value to calculate wheel rotation, pedal rotation, body bob, wing tilt, and road offset. Update only SVG transform/style attributes inside `requestAnimationFrame`; pause cancels the loop and reset restores zero time and baseline transforms.

- [ ] **Step 4: Run the structural test**

Run: `pytest tests/test_pelican_bicycle_page.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add site/pelican-bike.html
git commit -m "feat: add animated pelican bicycle SVG page"
```

### Task 3: Verify the page in a browser

**Files:**
- Verify: `site/pelican-bike.html`

**Interfaces:**
- Consumes: the completed standalone page.
- Produces: browser evidence that the page renders, controls respond, and narrow layouts remain contained.

- [ ] **Step 1: Serve the site locally**

Run: `python -m http.server 4173 --directory site`
Expected: a local server listening on `http://127.0.0.1:4173`.

- [ ] **Step 2: Inspect desktop and narrow viewports**

Open the page with Playwright, capture screenshots at 1280x820 and 390x844, and assert the page title, SVG visibility, and zero horizontal overflow.

- [ ] **Step 3: Exercise controls**

Click pause, confirm the button label changes to continue, click continue, then click reset and confirm the button returns to pause.

- [ ] **Step 4: Stop the local server and record verification**

Stop the server after checks finish and report the actual viewport/control results.
