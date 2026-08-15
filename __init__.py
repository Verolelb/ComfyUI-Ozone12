try:
    from comfy_api.latest import ComfyExtension  # noqa: F401 -- probe only
except ImportError:
    pass  # Outside ComfyUI runtime (e.g., standalone tests)
else:
    from .nodes import comfy_entrypoint  # noqa: F401

# Frontend widgets (live level meter + A/B player) served by the ComfyUI web app.
WEB_DIRECTORY = "./web"
