"""Unified-pipeline request path (docs/architecture/unified-pipeline.md, Step 3).

Each platform switches over through its admin ``PipelineRoute`` row. While a
row is off or missing, the platform stays on its existing path.
"""
