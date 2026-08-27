"""Compatibility build entry point for older pip/setuptools installations."""
from setuptools import setup


setup(
    name="asclient",
    version="0.7.5",
    description="AScript local iOS device client",
    python_requires=">=3.10",
    install_requires=["Pillow>=9.0"],
    packages=["asclient"],
    entry_points={"console_scripts": ["asc=asclient.cli:main"]},
)
