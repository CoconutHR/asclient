"""Compatibility build entry point for older pip/setuptools installations."""
from setuptools import setup


setup(
    name="asclient",
    version="0.6.5",
    description="Dependency-free AScript local iOS device client",
    python_requires=">=3.10",
    packages=["asclient"],
    entry_points={"console_scripts": ["asc=asclient.cli:main"]},
)
