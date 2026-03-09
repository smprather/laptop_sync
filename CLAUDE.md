# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A CLI tool that mirrors files from a Windows source directory (recursive) to a Linux destination directory using `scp` and `ssh`. It polls for changes, only copying files whose contents changed, and deletes destination files removed from the source (true mirror). The first poll iteration always checks for consistency. Assumes passwordless SSH.

## Tech Stack

- Python 3.14, managed with Astral uv
- rich-click for CLI interface
- PyYAML for configuration

## Configuration

All settings live in `config.yaml`. CLI flags override config values.

## Commands

- `uv run main.py` — run with default config.yaml
- `uv run main.py -c path/to/config.yaml` — run with custom config
- `uv sync` — install/sync dependencies
