"""Compatibility shim: all metadata lives in pyproject.toml.

Kept so that legacy ``python setup.py``-style workflows and very old pip
versions keep working; setuptools reads pyproject.toml for the real values.
"""
from setuptools import setup

setup()
