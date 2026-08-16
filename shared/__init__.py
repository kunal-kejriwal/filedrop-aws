"""Shared logic reused across Lambda handlers.

Kept dependency-free (stdlib + pydantic only) so the same code can run
under Lambda without ballooning the deployment package.
"""
